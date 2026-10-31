# Workflow specification

## Purpose

This file defines how the 35 agent responsibilities cooperate to find real demand, reach suitable prospects, learn from replies, and measure paid customers. `TRACEABILITY.md` owns the role-by-role contracts; `DATA_MODEL.md` owns durable records. A workflow advances through recorded events, not through an agent remembering that another agent finished.

## Common event envelope

Every handoff has `event_id`, `event_type`, `schema_version`, `entity_type`, `entity_id`, `correlation_id`, `caused_by`, `occurred_at_utc`, `source_role`, `policy_version`, and a payload/reference. Event IDs and logical job keys are unique. Consumers validate the schema, log the decision, and acknowledge only after durable processing. Unknown versions or missing required fields go to review rather than being guessed.

| Event | Producer → consumer | Minimum payload/reference |
| --- | --- | --- |
| `signal.observed` | 1A/1B/1C/1E/8A/6E → 8B/8C/2A | Source observation ID, signal type, organization/use-case hypothesis. |
| `use_case.review_ready` | 8B → operator | Dossier version, evidence, counter-evidence, proposed segment. |
| `prospect.discovered` | 1E/6E/8C → 2A | Organization ID, source IDs, country if evidenced. |
| `enrichment.finished` | 2A → 2B | Attempt and snapshot IDs, result/ambiguity status. |
| `fit.decided` | 2B → 2C/3A or review | Rubric version, score, reasons, decision. |
| `contact.verified` | 2C → 3A | Contact ID, channel, evidence, verification status. |
| `draft.ready` | 3A/3B/6A/6B/6C/6D/9D → 3D/7A | Draft ID/version/hash, claims and evidence references. |
| `review.passed` / `review.blocked` | 3D/7A → operator or revision queue | Finding IDs and exact draft version. |
| `approval.decided` | Operator → scheduler/action gate | Exact draft hash, target, channel, action, decision, expiry. |
| `action.due` | 5A/8D → 4A or channel adapter | Action ID, scheduled window, idempotency key. |
| `action.result` | Adapter → analytics/inbox | Provider ID, accepted/rejected/unknown result, cost/usage. |
| `inbound.received` | 4B/9A → 4C/9B/7F | Provider event ID, thread, type, raw content reference. |
| `ticket.changed` | 9B/9D → 9C | Ticket ID/version and changed fields. |
| `conversion.verified` | Product/payment evidence → 7D | Product account and unique verified payment ID. |
| `control.changed` | Operator/7B/7E/7F → all lanes | Pause/suppression/quota policy and effective time. |

## Workflow A: discover a viable problem and prospect

1. **Observe a signal.** Agents 1A, 1B, 1C, 1E, 8A, and 6E use configured sources. They capture the source URL/ID, text excerpt, date, and why it may indicate a pain point or buyer. A forum username is not a company; the OS must resolve a real organization before creating a company prospect.
2. **Research the use case.** Agent 8B develops the problem, current workaround, potential buyer, competing options, evidence against the hypothesis, and whether Lumen Sieve could credibly help. A human accepts or rejects a new strategic segment.
3. **Find organizations.** Agents 8C, 1E, and 6E gather candidate companies from authorized sources. The identity resolver links aliases/domain/provider IDs and flags uncertain matches for review. The source observation stays attached.
4. **Create a prospect.** One durable prospect record represents the organization in a particular segment/campaign context. Agent 8D can prioritize the resulting work, but cannot approve a new market, claim, or external action.

**Failure path:** source outage or access denial pauses only that source lane; an unverifiable company remains a research signal. Duplicate ingestion links to the existing record. Acceptance: a sourced signal can be traced to a reviewed use case and a distinct prospect without creating false company identities.

## Workflow B: enrich and qualify

1. Agent 2A receives a prospect with a supported business name and country; it creates an attempt before calling the Lumen adapter. The adapter waits for a possible three-minute lookup and records the measured outcome. Endpoint, authentication, and response fields are determined from the real API when connected.
2. The normalization layer preserves the raw response and records per-field provenance, age, and confidence. Multiple matching businesses, conflicting facts, missing identity, malformed output, and lookup failure lead to separate statuses.
3. Agent 2B applies the approved, versioned fit rubric and records score plus explanations. Low confidence or missing critical evidence routes to human review. Agent 2C seeks a relevant decision-maker through authorized data sources; unverified contact patterns are never treated as confirmed addresses.
4. A qualified, sufficiently evidenced prospect becomes eligible for a draft; a rejected or ambiguous one stays out of outreach until reviewed.

**Failure path:** retry safe transient failures with a bounded policy; do not turn a timeout into a low score or assert that a field is current merely because it came from one lookup. Acceptance: every qualification decision is reproducible from evidence and rubric version.

## Workflow C: draft, critique, approve

1. Agent 3A creates personalized text using approved product claims, target facts, and a content template; 3B prepares deeper technical material when useful. Community roles 6A/6B/6C/6D can create channel-specific drafts. Agent 3C proposes A/B variants based on observed data.
2. Agent 3D checks accuracy, structure, and clarity; 7A enforces claim, format, and objective rules. Blocking findings return the content for revision.
3. The operator sees the exact recipient or public destination, channel, complete content, evidence, planned action, and expiry; an approval is recorded against the content hash and policy version.
4. Any content edit, recipient change, channel change, or materially changed supporting claim creates a new review version. Approved content is immutable.

**Failure path:** unavailable critic, missing evidence, or an unapproved claim blocks advancement. Acceptance: an edited draft cannot use an earlier approval; an injected instruction in source text cannot change approval policy.

## Workflow D: schedule and execute an action

1. Agent 8D chooses a priority from approved objectives; 5A schedules a due action using a verified recipient time zone when available. If time zone is unknown, choose an approved default window or request review; never invent the time zone.
2. Immediately before execution, the central gate checks the local `PAUSE_ALL.lock` signal and database pause state, target suppression, exact valid approval, quota/spend reservation, contact validity, current adapter authorization, and action idempotency.
3. Agent 4A sends approved email; another authorized adapter may carry an approved public action where the platform and account support it. Draft-only/manual channels produce a reviewable handoff instead of pretending an API action happened.
4. Record provider ID and result. A known rejection may be retried according to policy; an unknown send outcome requires provider-side reconciliation or manual review before any resend.

**Failure path:** any gate denial records a reason and keeps the action unsent; no sleep/random spacing can substitute for a quota or provider rule. Acceptance: replay, double scheduler trigger, and network timeout do not cause a duplicate message or post.

## Workflow E: listen, respond, and suppress

1. Agents 4B and 9A ingest authorized mail/form/community events by provider ID and durable cursor. Duplicates are ignored after the first committed processing.
2. An opt-out, complaint, or explicit stop request immediately creates a suppression rule through 7F and cancels pending actions for the matching subject/scope. This path takes priority over automated reply composition.
3. An inquiry is classified with confidence. Agent 4C drafts a contextual response and live calendar option if available. A bug, feature request, or support request goes to 9B; 9D drafts a status-aware reply, and 9C projects ticket state to Sheets.
4. Customer-facing responses pass the configured approval gate. High-priority or uncertain intent alerts the operator. Bounces and out-of-office messages affect conversation state but are not counted as interested replies.

**Failure path:** inbox outage retains cursor, Sheets outage buffers projection, model outage leaves event unclassified for review. Acceptance: one provider message creates one conversation event and one appropriate response path; suppression blocks all later dispatch.

## Workflow F: measure and revise

1. Agent 5B records provider and model usage; 7B watches channel limits and health; 7C checks duplicates/integrity; 7E detects lag, hangs, and stalled queues.
2. Agent 7D calculates source → qualified → action → reply → trial → verified paid conversion by channel and cohort, including costs and confidence of attribution. The goal is five verified paying users in the first month; free signups, opens, and impressions do not satisfy it.
3. Agents 3C and 7D produce evidence-based experiment/segment recommendations. The operator decides whether to change objectives, limits, copy, or target market; the new version is logged before jobs use it.

**Failure path:** missing denominator, incomplete attribution, or small samples are reported as uncertainty. Acceptance: verified transactions are counted once; historical example numbers from planning documents do not enter live metrics.

## Control and recovery workflow

The operator can pause all work by creating `PAUSE_ALL.lock` in the workspace or using the operator control backed by the database. Workers check the signal before claiming new jobs and again immediately before external effects. In-flight read-only work may finish and record its result; new external actions stop. Clearing a pause requires an operator action and does not automatically dispatch all overdue actions: the scheduler reevaluates expiry, approval, suppression, and time windows. Failed jobs keep their state, diagnostics, and replay options. See `RUNTIME_AND_SCHEDULING.md` for leases, retries, and restart rules.

## Department coverage

Workflow A covers departments 1 and 8 plus 6E; B covers 2; C covers 3, 6A–6D, and 7A; D covers 4A, 5A, 7B, 7F, and 8D; E covers 4B/4C and 9A–9D; F and control cover 3C, 5B, and 7B–7E. Every named role appears with a detailed contract in `TRACEABILITY.md`; no role is inferred for missing ID 1D.
