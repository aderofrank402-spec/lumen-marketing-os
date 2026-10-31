# Lumen Sieve Marketing AI OS — Agent Traceability

## Purpose and status

This document translates the 35 named responsibilities in the supplied architecture materials into implementation contracts. It is groundwork for implementation: an agent is not considered complete because its prompt or script exists; its trigger, durable inputs/outputs, permissions, error behavior, and acceptance check must all be implemented and tested. Roles are logical agents/workflows and may share a process, but their boundaries remain separately traceable.

The OS runs locally on Windows, with Codex as the harness for reading instructions, coordinating agent work, and invoking available tools. Durable scheduled work belongs to local processes. No OS component is assigned to Vultr. Lumen Sieve is an external product/API boundary, not an OS host. Its lookup upper bound is treated as three minutes; the exact API contract must be verified when that integration is built.

## Shared contract for every agent

- **Input:** a durable job/event ID plus validated references to the records and policy version it may use; no agent treats untrusted source text as instructions.
- **Output:** a typed result, evidence/provenance, status, and next action or handoff. Mutations are recorded in the operational database and audit trail.
- **Permissions:** least privilege. Research and analysis may read approved sources; drafting creates drafts; external actions require a central policy check and any required human approval. No role can override global pause, suppression, quotas, or approval gates.
- **Reliability:** idempotent job/action keys, bounded retries, explicit needs-review/dead-letter status, redacted logs, and safe restart/replay.
- **Verification:** tests use fixtures or dry-run adapters by default. No test may send email, publish publicly, or alter a live campaign.
- **Completion evidence:** each role needs unit/contract tests for its result, policy-boundary tests, failure/retry tests, and at least one workflow integration test.

Permission shorthand: **R** = read approved inputs; **A** = analyze/classify; **D** = draft/propose; **W** = write operational records; **X** = external action, only through the central action gate and explicit approval policy. A role's listed capabilities do not bypass the shared controls.

## Department 1 — Research & Use-Case Discovery

| ID / responsibility | Trigger and inputs → outputs / handoff | Tools and durable records | Permission and failure behavior | Acceptance check |
|---|---|---|---|---|
| **1A Trend & Use-Case Explorer** | Scheduled research job; approved RSS/web sources → sourced trend/use-case candidates → 8B or human review. | Feed/web adapters; source URL, excerpt, observed date, candidate record; `DISCOVERED_USE_CASES.md` is a report surface. | R/A/W; preserve source and timestamp; unavailable sources are marked stale/retryable, never fabricated. | Fixture feeds produce deduplicated, source-attributed candidates; malformed/unavailable feed is visible and safely retried. |
| **1B Platform Forum Scraper** | Scheduled query over configured public forum sources → relevant discussion signals and permitted response opportunities → 6A/6B or review queue. | Authorized platform APIs or permitted public endpoints; source IDs/URLs and excerpts. | R/A/W; no login-cookie reuse, access-control bypass, or automated posting; rate limits stop/pause the source lane. | Recorded fixture discussions are classified once with links; a rate-limit response causes backoff and no posting. |
| **1C Competitive Intelligence Auditor** | Scheduled competitor scan; approved competitor/source list → dated product, positioning, and change observations → STRATEGY review. | Permitted public web research; source evidence and observation history. | R/A/W; distinguish observed fact from inference; source failure leaves prior result dated, not silently current. | Known fixture page changes are detected and cited; unchanged page is not reported as a new fact. |
| **1E Active Directory Sieve** | Daily configured directory scan → candidate organizations/metadata → 2A enrichment queue. | Authorized directory/product-launch APIs and permitted web sources; source IDs, organization candidates, scan cursor. | R/A/W; respect provider terms/quotas; persist cursor and recover without duplicate ingestion. | A fixture scan resumes from cursor, deduplicates records, and stops cleanly on quota denial. |

## Department 2 — Lead Generation & Qualification

| ID / responsibility | Trigger and inputs → outputs / handoff | Tools and durable records | Permission and failure behavior | Acceptance check |
|---|---|---|---|---|
| **2A Data Enrichment Orchestrator** | New candidate marked eligible → request business lookup → validated enrichment snapshot, ambiguity, or failure → 2B. | Lumen adapter; request/attempt/result IDs, schema version, evidence, latency/cost fields. | R/A/W; lookup deadline supports up to three minutes; bounded retry only when safe; never upgrades ambiguous data to verified. | Mock success, malformed response, timeout at three-minute boundary, and provider outage all yield distinct durable statuses without blocking unrelated jobs. |
| **2B Fit-Score Analytics Gatekeeper** | Enrichment result available → apply approved scoring rubric → score, factor explanations, and qualified/review/rejected decision → 2C or review. | Versioned scoring rules; source evidence and score explanation; lead lifecycle record. | R/A/W; cannot change rubric or campaign rules; missing fields lower confidence or request review. | Fixed fixture cases yield expected score/reasons; missing or conflicting facts trigger the documented review path. |
| **2C Decision-Maker Locator** | Qualified organization → identify relevant public business contact/channel → sourced contact candidates → content/review workflow. | Authorized contact data provider or public business sources; contact/source/provenance records. | R/A/W; do not infer private personal data or mark guessed email as verified; provider errors are recorded. | Fixture organization yields only evidence-backed contact candidates; guessed/unverified fields are explicitly labeled and cannot pass send gate. |

## Department 3 — Creative Content & Personalization

| ID / responsibility | Trigger and inputs → outputs / handoff | Tools and durable records | Permission and failure behavior | Acceptance check |
|---|---|---|---|---|
| **3A Hyper-Personalization Engine** | Qualified, sufficiently evidenced lead plus approved template → personalized content draft → 3D review. | Approved model adapter, template/playbook version, verified enrichment facts; immutable draft/version record. | R/A/D/W; may use only evidence in the job; uncertain claims are omitted or qualified; no direct send. | Fixture facts appear accurately in draft; unsupported claims are rejected/flagged; prompt-injection text in source cannot alter policy. |
| **3B Technical Writer & Case-Study Drafter** | Approved content request with product facts/evidence → technical explanation or case-study draft → 3D and human review. | Markdown/document generation; approved product/API documentation and cited evidence; draft/version record. | R/A/D/W; no invented customer results or case-study metrics; formatting/export errors retain draft and surface diagnostics. | Fixture brief generates a correctly structured draft with citations and no unsupported claims; invalid export fails visibly. |
| **3C A/B Framework Optimizer** | Sufficient campaign observations → compare approved variants and propose next experiment → human approval. | Analytics queries and statistical utilities; variant, exposure, outcome, and experiment records. | R/A/W/D; recommendation only; no self-directed changes to live weights, spend, or campaign policy. | Synthetic data with known winner yields appropriately qualified recommendation; low sample size returns “insufficient evidence.” |
| **3D Content Critic & Proofreader** | Draft created or edited → validate claims, policy, readability, and format → pass, revise, or human escalation. | Rule-based checks plus approved model adapter; content/playbook versions and critique findings. | R/A/W; cannot approve external action; failure to run critic blocks progression to approval. | Tests catch disallowed claim, missing evidence, malformed content, and clean draft; only clean/review-cleared draft advances. |

## Department 4 — Outreach & Inbox Orchestration

| ID / responsibility | Trigger and inputs → outputs / handoff | Tools and durable records | Permission and failure behavior | Acceptance check |
|---|---|---|---|---|
| **4A SMTP/Outreach Dispatcher** | Approved queued action reaches send window → gated email send → provider result/message ID → 4B and analytics. | Authorized email adapter; exact approved content hash, recipient, channel, attempt, delivery result. | R/X/W; central gate rechecks approval, suppression, pause, and quotas immediately before send; ambiguous provider result enters reconciliation, not blind resend. | Dry-run records intended action; blocked approval/opt-out/quota sends nothing; successful mock send is idempotent under replay. |
| **4B Inbound Intent Classifier** | New inbound message/webhook/poll result → deduplicated normalized event and intent classification → 4C or support/opt-out route. | Authorized mailbox/webhook adapter and classifier; message/thread IDs, intent, confidence, event record. | R/A/W; unsubscribe/opt-out is handled conservatively and promptly; parser/classifier uncertainty escalates. | Duplicate delivery yields one event; opt-out enters suppression; ambiguous intent routes to review. |
| **4C Responder & Auto-Scheduler** | Classified warm inquiry/meeting intent → response draft and available scheduling options → human review or policy-approved next step. | Approved model/calendar adapters; message context, draft, proposed slots, approval record. | R/A/D/W; no unapproved customer commitments, pricing claims, or calendar booking; unavailable calendar yields a draft without fabricated slots. | Mock warm inquiry produces relevant draft; booking requires approval and valid slot; unavailable calendar is handled safely. |

## Department 5 — System Operations & Performance Registry

| ID / responsibility | Trigger and inputs → outputs / handoff | Tools and durable records | Permission and failure behavior | Acceptance check |
|---|---|---|---|---|
| **5A Central Cron Execution Manager** | Local schedule/timer or operator command → create and claim due jobs → workflow dispatch and status monitoring. | Windows scheduler/process manager plus database job queue and locks; schedule/job/run records. | R/W; schedules are configuration-controlled; missed runs follow explicit catch-up policy; overlapping jobs are locked. | Restart, delayed schedule, duplicate trigger, and overlapping run tests produce one valid execution and visible status. |
| **5B Data Store & Token Ledger** | Every job, model/provider call, and state change → transactional persistence and usage accounting. | SQLite driver/migrations; token/provider usage, job state, audit event, backup metadata. | R/W; atomic updates, protected data, redacted secrets; database errors halt unsafe writes and alert. | Migration, transaction rollback, concurrent access, cost accounting, backup/restore, and replay tests preserve consistent state. |

## Department 6 — Community Growth & Viral Launch

| ID / responsibility | Trigger and inputs → outputs / handoff | Tools and durable records | Permission and failure behavior | Acceptance check |
|---|---|---|---|---|
| **6A Reddit Inbound Hacker** | Scheduled authorized monitoring or manually supplied thread → relevant discussion/context and helpful response draft → human review. | Supported Reddit API or approved source; thread/source IDs, draft, rationale. | R/A/D/W; no unsolicited or automated public comment absent explicit platform permission and human approval; respect quotas. | Fixture thread yields context-aware draft with source link; API denial stops lane and no comment is posted. |
| **6B Hacker News Content Strategist** | Relevant trend/topic or approved launch request → technical article/comment draft and evidence → editorial review. | Permitted HN data/API and content templates; source/trend and draft records. | R/A/D/W; no fabricated technical claims or automated posting; rate/provider errors are surfaced. | Fixture topic yields sourced draft; publish action remains blocked until separately approved. |
| **6C Product Hunt & Launchpads Orchestrator** | Approved launch window → asset/checklist/comment drafts and schedule reminders → operator launch review. | Authorized launch platform interfaces; asset/version, schedule, checklist, response records. | R/A/D/W; platform publication is explicit human action unless verified supported permission says otherwise; failed API does not imply launch success. | Dry run produces complete checklist; no live launch occurs; mock failure remains visible. |
| **6D LinkedIn Authority Builder** | Approved topic or permitted public signal → insight and response draft → human review/manual publication unless authorized supported action exists. | Approved research source and content templates; provenance and draft records. | R/A/D/W; logged-in browser session alone is not authorization for unattended scraping or posting; access/rate warning pauses lane. | Fixture signal generates a relevant sourced draft; no browser action/publication occurs in dry run. |
| **6E B2B Prospect List Indexer** | Approved source/list becomes available → normalize organization/contact rows → prospect records for qualification. | Authorized directory/provider adapters; source IDs, normalization decisions, import batch/cursor. | R/A/W; deduplication preserves source evidence; invalid fields are quarantined, not silently discarded. | Re-importing same fixture creates no duplicates; invalid row is quarantined with reason. |

## Department 7 — System Auditor & Executive Controller

| ID / responsibility | Trigger and inputs → outputs / handoff | Tools and durable records | Permission and failure behavior | Acceptance check |
|---|---|---|---|---|
| **7A Copywriting & Outreach Guard** | Draft/approved-action request → deterministic policy/content validation → pass, block, or review findings. | Versioned rules and approved model checker; policy version and audit finding. | R/A/W; fail closed on unavailable critical check; cannot edit policy or authorize itself. | Prohibited claim/content is blocked; clean content passes; checker outage blocks action and records reason. |
| **7B Rate-Limit & Domain Reputation Watchdog** | Before dispatch and on monitoring schedule → evaluate channel/domain quota and health → allow, throttle, or pause recommendation/action. | Provider counters/health signals where authorized; quota ledger and alert records. | R/A/W; hard limits are centrally enforced; missing telemetry never causes automatic volume increase. | Boundary tests stop at configured cap; unhealthy/missing signal throttles or pauses; reset occurs only per configured window. |
| **7C Database Integrity & Loop Inspector** | Scheduled integrity scan and before replay-sensitive jobs → duplicate/orphan/cycle findings → quarantine or operator alert. | SQL integrity/dedup checks; entity IDs, job graph, audit findings. | R/A/W; no destructive merge without review; detected loops halt affected lane. | Fixture duplicates/orphan/cycle are detected; unrelated records unchanged; replay remains safe. |
| **7D Cross-Channel Growth Optimization Auditor** | Weekly or configured reporting window → attributed funnel/cost/conversion data → performance report and proposed changes. | Database analytics and visualization/report generation; campaign/channel metrics and report. | R/A/D/W; recommendations only; objectives, quotas, and budgets need operator approval. | Known synthetic funnel metrics calculate correctly; missing/low-sample data is labeled and does not trigger autonomous reallocation. |
| **7E Pipeline Lag Sentinel** | Periodic health tick → queued/running/overdue job state → alert and controlled recovery suggestion. | Job DB, process health/heartbeat; alert, lag, and recovery records. | R/A/W; may mark lane unhealthy and alert; restart/replay follows configured safe policy and cannot duplicate external actions. | Hung job triggers alert within configured interval; healthy job does not; recovery preserves action idempotency. |
| **7F Global Suppression & Compliance Vault** | Opt-out, block, complaint, or approved suppression import → normalized suppression record → global pre-action gate. | Protected suppression table, normalization/key strategy, source and timestamp; generated review view. | R/W; checked before every channel action; errors resolving suppression fail closed; retain only minimum necessary data. | Same identity/channel is blocked across workflows after suppression; normalization variants match; deletion/retention behavior is tested. |

## Department 8 — Use-Case Discovery & Prospect Escalation

| ID / responsibility | Trigger and inputs → outputs / handoff | Tools and durable records | Permission and failure behavior | Acceptance check |
|---|---|---|---|---|
| **8A Autonomous Use-Case Scout** | Continuous/configured research schedule → candidate vertical/use case with evidence → 8B. | Approved web/RSS/funding/product documentation sources; source evidence and candidate backlog. | R/A/W; research only; source uncertainty is retained, and disallowed sources are not accessed. | Fixture sources yield candidate plus citations; unsupported claim is flagged; duplicate candidate is linked. |
| **8B Deep-Dive Vertical Researcher** | New candidate accepted → structured pain/workflow/alternative/buyer analysis → research dossier and recommendation → review. | Permitted web research/model adapter; source citations, claims, counter-evidence, dossier version. | R/A/D/W; distinguish facts/inferences; weak evidence is explicitly marked; no automatic strategy change. | Dossier includes citations and counter-evidence; unsupported fields remain unknown; reviewer can trace claims to source. |
| **8C Vertical Prospect Hunter** | Approved vertical and ICP → find candidate organizations and public business evidence → prospect ingestion queue. | Authorized indexes/directories and permitted research tools; source, organization, and import batch records. | R/A/W; no private-data inference or prohibited scraping; provider limit halts work safely. | Fixture vertical produces sourced candidates; duplicate organizations reconcile; rate limit pauses without data loss. |
| **8D Central Brain & Dispatch Arbiter** | New eligible workflow events and approved operating objectives → priority decision and durable job dispatch → department queue. | Scheduler/queue, policy config, database locks; dispatch decision and explanation. | R/A/W; cannot mutate approved objectives, bypass gates, or dispatch disallowed action; conflict becomes review state. | Competing fixture jobs follow deterministic priority; pause/limit blocks dispatch; retry does not duplicate job/action. |

## Department 9 — Product Feedback, Triage & Support

| ID / responsibility | Trigger and inputs → outputs / handoff | Tools and durable records | Permission and failure behavior | Acceptance check |
|---|---|---|---|---|
| **9A Multi-Channel Feedback Listener** | Configured polling/webhook or support contact → deduplicated inbound feedback/support event → 9B. | Authorized email/form/webhook adapters; event source, ticket ID, timestamp, raw-message reference. | R/W; protect personal data; duplicates collapse; connector outage is visible and does not drop cursor state. | Duplicate webhook/poll returns one ticket event; outage recovers from durable cursor. |
| **9B Bug & Triage Classifier** | New feedback event → classify bug, feature request, sales, or other intent with confidence → 9C/9D or human queue. | Approved classifier/model adapter and category schema; classification/version/confidence record. | R/A/W; uncertain/high-impact reports escalate; never modifies product code or customer account. | Labeled fixtures hit expected categories; ambiguous issue routes to review; classifier outage preserves unclassified ticket. |
| **9C Google Workspace Ticket Synchronizer** | Ticket state created/changed → idempotent sync to authorized support dashboard → sync result. | Least-privilege Google Sheets API/connector; stable ticket ID, sync cursor, error/outbox state. | R/W; Sheets is a projection, not source of truth; no credential logging; outages buffer and retry. | Repeat sync updates same row; outage buffers then reconciles; sheet edits cannot overwrite authoritative ticket fields silently. |
| **9D Automated Customer Responder** | Classified ticket with response policy → reply draft referencing ticket and known status → human approval/send gate. | Approved email adapter/model; ticket, draft version, approval, outbound message record. | R/A/D/W; send only under explicit response policy and approval; no unsupported promises or disclosure of internal logs/secrets. | Fixture ticket yields accurate draft; missing status is not invented; no message sends without required approval. |

## Cross-agent workflow handoffs

| Workflow | Ordered handoff | Shared state / invariant |
|---|---|---|
| Use-case discovery | 1A/1B/1C/1E and 8A → 8B → human review → approved target/ICP → 8C/2C | Findings retain source evidence; research alone does not change strategy or launch outreach. |
| Prospect acquisition | 1E/8C/6E → 2A → 2B → 2C → 3A | Every lead has stable identity, source provenance, enrichment status, and explainable qualification. |
| Campaign content | 3A/3B → 3D → 7A → human approval → immutable approved version | A change after approval invalidates the approval; drafts never become external actions implicitly. |
| Outreach and response | 8D/5A schedule → 7B/7F/action gate → 4A or approved community adapter → 4B → 4C/9B | Pause, suppression, approval, quota, and idempotency checked at point of action. |
| Product feedback | 9A → 9B → 9C sync and/or 9D draft → human-approved response | Database ticket is authoritative; dashboard sync is recoverable; opt-out is also sent to 7F. |
| Measurement and adjustment | 5B event records → 7D report → operator decision → approved objective/config update | Verified conversions only; the five-paying-user goal is measured, not guaranteed; agents cannot self-approve policy changes. |

## Shared operational records

All agents use stable IDs and append relevant audit events. At minimum, the data model must support: `jobs`/attempts; sources/evidence; organizations/leads; contacts; enrichment requests/results; use cases/research dossiers; scoring decisions; campaigns/variants/drafts; approvals; outbound/inbound messages; suppression entries; tickets; provider usage/quotas/spend; sync outbox/cursors; audit events; and verified conversion/payment events. Exact table names and migrations are implementation decisions, but no workflow may rely on an unrecorded in-memory handoff for critical state.

## Build completion gate

An agent responsibility is ready for use only when:

1. Its trigger and input/output schema are implemented and documented.
2. Its tool access and permission are explicit and least-privileged.
3. Its database writes are transactional/idempotent and provenance is retained.
4. Pause, suppression, approval, quota, and action controls are enforced where relevant.
5. Success, malformed input, timeout/provider outage, duplicate delivery, restart, retry exhaustion, and safe replay have tests appropriate to that role.
6. Its workflow handoff and operator-visible state are verified end to end in dry-run mode.

Only after the complete dry-run suite and recovery checks pass may the operator consider enabling live external actions; doing so requires explicit approval.
