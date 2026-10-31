# Test and operations plan

## Definition of complete

The OS is complete when all 35 named responsibilities have working handlers or an explicitly designed, verified manual handoff for an unsupported external capability; their data and event contracts work together; the operator can see and control the system; and the full dry-run, failure, restart, and restore scenarios pass. A folder of scripts or successful individual API calls is not enough. Five paying users in one month is the business target to measure, not a test that code alone can guarantee.

## Test layers

| Layer | What it proves | Required examples |
| --- | --- | --- |
| Pure role/policy tests | Deterministic decisions from inputs and rule versions. | Fit-score explanation, claim guard, suppression normalization, quota boundary, priority, time-zone handling. |
| Schema and migration tests | Durable model integrity across versions. | Foreign keys, uniqueness, up/down or restore path, preservation of old records, corrupt/missing input handling. |
| Adapter contract tests | Every connector obeys the normalized result/error contract. | Success, no result, ambiguity, malformed data, unauthorized, rate limit, timeout, outage, usage/cost reporting, redacted errors. |
| Workflow integration tests | Handoffs are committed and replayable. | Research → prospect → enrichment → score → draft → review; inbound → suppression/ticket; report projection; duplicate events. |
| Action-gate tests | No disallowed external action occurs. | Missing/expired/edited approval, changed target, global/lane pause, suppression, quota/spend exhaustion, unverified contact, adapter unavailable. |
| Recovery tests | Restart and uncertainty do not duplicate or lose actions. | Crash before/after outbox commit, expired lease, unknown send result, inbox cursor replay, Sheet outage, restore from backup. |
| Operator acceptance | A person can understand and control the OS. | Inspect status, approve exact content, pause/resume, review ambiguous lead, resolve unknown action, replay safe job, restore backup. |

Every role entry in `TRACEABILITY.md` must identify a corresponding test case and evidence. Test data is invented and labeled; examples of leads, conversions, credentials, or campaign results in the source PDFs never become production history. Dry-run adapters use recorded fixtures and never send messages, publish content, book meetings, or mutate a live campaign.

## End-to-end scenarios

1. **Clean path:** A sourced company and problem are ingested, deduplicated, enriched, scored, matched to a verified contact, drafted, critiqued, approved, queued, dry-run dispatched, replied to, and represented in reports with one consistent correlation ID.
2. **Ambiguous company:** Lumen returns multiple/uncertain matches; the prospect enters review, no personalized claim or outreach proceeds, and the operator can resolve it with evidence.
3. **Opt-out before send:** A contact opts out after action queueing; the global vault records it and the final action gate blocks email and public follow-up across relevant channels.
4. **Approval changes:** The operator approves a particular draft, then content/recipient changes; the old approval fails and a fresh decision is required.
5. **Duplicate source/inbound:** A scanner or inbox poller sees the same provider ID twice; only one source event, prospect, reply route, and support ticket are created.
6. **Unknown delivery result:** The provider accepts or may have accepted a send while the network response is lost; the action remains `unknown`, no automatic resend occurs, and the operator has a reconciliation path.
7. **Outages:** Lumen, email, model, Google Sheets, or one research source fails independently; affected work queues/retries while unrelated lanes continue and errors remain visible.
8. **Power loss and restore:** Stop local workers mid-job, restart, then restore from a protected backup in a separate test location; state, suppression, approvals, outbox, and action IDs remain consistent.
9. **Verified payment:** A real or fixture payment event with stable transaction ID counts once as paid conversion; a trial/free signup and duplicate payment event do not increase the paid count.
10. **Pause/resume:** `PAUSE_ALL.lock` prevents external action, including an already queued one; resume triggers revalidation and does not burst-send overdue actions.

## Operational dashboard and alerts

The local status command/view must show: process health and last heartbeat; schedules/next run; queued/leased/retry/review/dead-letter jobs; oldest backlog; adapter connectivity and last error; pending drafts/approvals; active pause state; suppression events; quota and provider spend; pending/unknown actions; Sheets/report sync lag; last backup and restore-drill date; and funnel metrics through verified paid users. Alerts identify the exact affected lane, action/job ID, severity, last error class, and suggested next safe step. Personal data and credentials are redacted.

## Daily and weekly operating routines

| Cadence | Operator/system task | Evidence |
| --- | --- | --- |
| At startup | Validate configuration, migration level, secret references, pause state, last unfinished jobs, and adapter probes. | Startup health report with pass/fail per enabled lane. |
| During operation | Monitor heartbeats, action denials, bounce/complaint/opt-out events, spend, quotas, and queue lag; review approvals and unknown outcomes. | Alerts and audit IDs; no silent error swallowing. |
| Daily | Back up database using SQLite's safe backup API or a consistent snapshot, export config versions, verify backup readability; summarize funnel and spend. | Backup ID, checksum, age, summary. |
| Weekly | Run integrity checks and a restore drill in an isolated directory; review conversion evidence, source quality, account health, and proposed strategy changes. | Drill result and operator decision record. |
| After a change | Run focused tests, dry-run affected workflow, record config/migration version, then activate under the existing approved policy. | Test report and release record. |

Backup retention and storage location are configured for this machine; copies must be protected because they contain prospects and messages. Never back up a live SQLite database by casually copying the main file while writes continue. A restore drill must prove that migrations, data, suppression checks, and queued/unknown actions can be interpreted correctly. Restore does not automatically resume external sends.

## Incident and recovery procedure

1. **Contain:** pause the affected lane or all external actions when scope is uncertain; capture incident time, current config version, and job/action IDs.
2. **Diagnose:** inspect redacted errors, provider IDs, outbox/jobs, approval/suppression state, and database integrity; distinguish no effect, confirmed effect, and unknown effect.
3. **Recover:** repair configuration or connector, restore from backup if necessary, reconcile unknown actions, and replay only idempotent jobs with a recorded operator command.
4. **Verify:** run relevant contract/workflow tests, confirm queue drains safely, review results and quota accounting, then explicitly resume the lane.
5. **Learn:** log cause, impact, affected contacts, fix, and preventive test. If users were contacted incorrectly, identify and handle that separately before new outreach.

## Build and live activation gates

| Gate | Exit evidence |
| --- | --- |
| Foundation | Config validates; migrations, queue/outbox, pause, approval, suppression, quota, audit, and backup/restore work locally. |
| Adapter | Each enabled connector has verified actual contract, permissions, fixtures, redacted diagnostics, and outage tests. |
| Agent | All 35 responsibilities have implemented logic or verified manual handoff, role-level tests, and workflow coverage. |
| System dry-run | Ten end-to-end scenarios above pass; dashboards/status match the database; no external action is performed. |
| Live channel | Operator approves account, action type, limits, sender/privacy rules, exact content policy, and recovery readiness; a small controlled run is observed before any increase. |

The first implementation action after these design files is to create the package structure and shared foundation described in `PROJECT_STRUCTURE.md`, then build the actual agent handlers in dependency order. Additional planning documents are not a prerequisite.
