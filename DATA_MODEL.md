# Data model

## What the database must do

The AI OS needs one reliable record of what it found, what it inferred, what a person approved, what it actually did, and what happened afterward. The local SQLite database is that record. Agent memory, Markdown reports, and Google Sheets may display or summarize it, but they do not determine whether an action is safe to run. This design is for the marketing OS on the local Windows machine; Lumen Sieve's product database is separate.

All IDs below are stable opaque IDs. All stored timestamps are UTC with a separate IANA time zone where a local delivery window matters. Every record with external evidence keeps a source reference and observation time. Sensitive message/contact content is access controlled and retained only as long as needed. The exact physical schema is created by versioned migrations, not by copying the illustrative SQL from a draft document.

## Entity relationships

```text
source_observation ──► use_case ──► research_dossier
         │                 │
         └──► organization ──► prospect ──► campaign
                  │              │            │
                  ├──► contact   ├──► enrichment_attempt / enrichment_snapshot
                  │              ├──► fit_assessment
                  │              └──► draft ──► review ──► approval ──► action
                  │                                                   │
                  └──► suppression_rule                       delivery_attempt
                                                                │
                                                        message / inbound_event
                                                                │
                                                     conversation / support_ticket
                                                                │
                                                       trial / paid_conversion

job / job_attempt / outbox_event / audit_event track the work across every branch.
quota_ledger / provider_usage track allowed volume and cost.
```

### Identity and evidence

| Record | Required fields and purpose | Key rule |
| --- | --- | --- |
| `source_observation` | `id`, source type, external source ID or URL, captured excerpt/hash, observed time, fetch time, permitted-use marker, related organization/use-case, confidence and notes. | Unique on source type plus external ID or canonical URL plus observed content hash; source text is data, never instructions. |
| `organization` | `id`, display name, normalized name, country code if known, canonical domain if verified, identity status, created/updated times. | No unique `(name,country)` constraint as a substitute for entity resolution; use domain/source IDs and review ambiguous merges. |
| `organization_alias` | Organization ID, alias, evidence ID, validity period. | An alias points to one candidate identity only after review or sufficiently strong evidence. |
| `contact` | `id`, organization ID, role, name if known, business address/channel, verification status, source observation, last verified time. | Guessed patterns are not `verified`; deduplicate by normalized address and organization while retaining evidence. |
| `prospect` | `id`, organization ID, segment, use-case ID, acquisition source, owner, lifecycle state, priority, created/updated times. | One organization may enter multiple campaigns, but a contact/channel cannot receive duplicate outreach for the same campaign intent. |
| `use_case` | `id`, target vertical, problem statement, buyer/workflow hypothesis, state, evidence links, review decision, owner. | Candidate research is not approved strategy until a person accepts it. |
| `research_dossier` | Use-case ID, version, source-backed findings, alternatives, counter-evidence, unknowns, reviewer. | Changes create a version; claims can be traced to observations. |

### Qualification and marketing content

| Record | Required fields and purpose | Key rule |
| --- | --- | --- |
| `enrichment_attempt` | Prospect/organization ID, request ID, adapter version, start/end, outcome, HTTP/provider error class, measured latency and cost. | Every attempt is recorded; retry uses the same logical request ID where the provider supports idempotency. |
| `enrichment_snapshot` | Attempt ID, schema version, raw response reference, normalized fields, field-level source/confidence/freshness, ambiguity status. | A new lookup creates a snapshot; it does not silently overwrite the evidence used in an approved draft. |
| `fit_assessment` | Prospect ID, rubric version, input snapshot IDs, score, reasons, uncertainty, decision, assessment time. | A score is reproducible from its recorded inputs and rule version. |
| `campaign` | `id`, segment, objective, channel, active version, dates, approved quota/budget policy, status. | Objective and active policy changes are versioned and operator approved. |
| `campaign_variant` | Campaign ID, variant key, template/content version, hypothesis, start/end and allocation. | Variant assignment is recorded before exposure and is stable for a prospect. |
| `draft` | `id`, prospect/contact/campaign/channel, body and subject or asset reference, exact content hash, evidence links, author role, version, status. | Editing creates a new version and invalidates approval of the old version. |
| `review_finding` | Draft ID/version, critic/guard role, rule version, finding, severity, disposition. | Blocking findings prevent an approval-ready state. |
| `approval` | Actor, decision, time, policy version, exact draft hash/version, target ID, channel, action type, expiry and optional planned window. | An approval cannot be transferred to a different recipient, channel, or edited content. |

### Actions, responses, and outcomes

| Record | Required fields and purpose | Key rule |
| --- | --- | --- |
| `action` | `id`, kind, target, channel, campaign, draft/approval IDs, due time, state, idempotency key, policy version. | Unique idempotency key; no external call unless the action gate records an allowed decision immediately beforehand. |
| `delivery_attempt` | Action ID, provider request/response IDs, start/end, status (`accepted`, `rejected`, `unknown`), redacted diagnostic. | `unknown` is reconciled before another attempt; an HTTP/network timeout is not proof that nothing was sent. |
| `message` | Stable provider message/thread IDs, direction, channel, contact, action, timestamp, body reference, delivery/bounce status. | Unique provider event ID; inbound duplicates do not create duplicate replies. |
| `conversation` | Contact/channel/thread, last activity, intent/status, owner, next review time. | Links all inbound and outbound messages in one context. |
| `inbound_event` | Provider event ID, raw-event reference, normalized type, classification/confidence, processing status, received time. | Idempotent ingestion and explicit handling for opt-out, complaint, bounce, inquiry, and support. |
| `support_ticket` | Ticket ID, source event, category, severity, status, owner, response draft/action, updated time. | Ticket history is append-only or versioned; Sheets is a projection. |
| `trial` | Product account ID, prospect/organization attribution, observed start time, evidence/source. | A trial is a funnel event, not a paid conversion. |
| `paid_conversion` | Product account ID, verified payment/transaction ID, amount/currency if available, paid time, verification source, attribution confidence. | Unique verified transaction ID; only verified successful payment counts toward the five-paying-user target. |

### Controls and operations

| Record | Required fields and purpose | Key rule |
| --- | --- | --- |
| `suppression_rule` | Subject type (`address`, `domain`, `organization`, `account`), keyed normalized value, scope, reason, source, created time, expiry/retention decision. | A keyed hash/HMAC may protect lookup values; comparison is against the same normalized representation, never raw text against a hash. |
| `quota_policy` / `quota_ledger` | Channel/provider, scope, period, approved limit, reserved/committed use, policy version. | Reserve transactionally before an external action; release or reconcile on failure/unknown outcome. |
| `provider_usage` | Provider/model, job, token/request units, estimated and actual cost, time, billing reconciliation status. | A missing price or usage result cannot silently be counted as zero. |
| `job` / `job_attempt` | Workflow name, stable payload reference, deduplication key, priority, due time, state, lease owner/expiry, attempt count, last error class. | Unique logical job key; leases and heartbeats support recovery without parallel execution. |
| `outbox_event` | Event type/schema version, aggregate ID, payload reference, delivery state, destination, retry time, deduplication key. | Written in the same transaction as source state so handoffs survive crashes. |
| `sync_cursor` | Adapter, stream, last processed provider ID/page/time, checked time. | Advance only after durable event ingestion; replay is safe. |
| `system_control` | Global/lane pause state, reason, actor, time, effective policy version. | Pause is read at job start and immediately before external effects. |
| `audit_event` | Actor/role, event type, entity IDs, before/after state or change summary, policy/content version, time, correlation ID. | Append-only, redacted, and queryable by action/job/lead. |

## State machines

**Prospect:** `discovered` → `enrichment_pending` → `enriched` or `enrichment_failed` or `ambiguous_review` → `qualified` or `rejected` → `drafted` → `review_pending` → `approved` → `action_queued` → `contacted` → `replied` → `trial` → `paid`. `suppressed`, `paused`, and `closed` may interrupt eligible stages; historical facts remain in their own records rather than being squeezed into one status string.

**Action:** `drafted` → `review_pending` → `approved` → `queued` → `reserved` → `dispatching` → `accepted` or `rejected` or `unknown` or `cancelled`. `unknown` requires reconciliation; it cannot be automatically retried as if rejected. Approval expiry, suppression, and pause can return a queued action to `review_pending` or `cancelled`.

**Job:** `pending` → `leased` → `succeeded`, `retry_wait`, `needs_review`, or `dead_letter`. A lease expiry permits recovery only after checking whether the job had an external effect.

## Ownership, transactions, and privacy

- Enable SQLite foreign keys and a suitable busy timeout; use WAL mode for concurrent readers with a controlled write path. Do not copy a live database file as a sync method.
- A workflow commits its state update and next outbox event together. A separate worker delivers the event and records success, allowing retry after restart.
- Identify organizations using strong evidence such as verified domain and provider IDs; conflicting names/locations enter merge review. Keep the source observations even after an identity merge.
- Store secrets outside the database. Store only required personal/contact data; protect message bodies and suppression material, document retention, and support deletion while retaining minimal suppression proof where required.
- Project reports (`CAMPAIGN_TRACKER.md`, `OUTREACH_REGISTRY.md`, `AUDIT_LOGS.md`) and Google Sheets are generated views. Failure to refresh them does not roll back the database or erase queued work.

## Migration and verification requirements

Migrations have ordered versions, explicit up/rollback or restore instructions, and backups before destructive changes. Tests must cover foreign keys, uniqueness, duplicate source ingestion, ambiguous organizations, suppression normalization, approval invalidation, quota race, transactional outbox, action timeout/unknown outcome, paid conversion deduplication, and backup/restore integrity. The first migration will turn this logical model into concrete tables and indexes; that is an implementation step, not something assumed to exist today.
