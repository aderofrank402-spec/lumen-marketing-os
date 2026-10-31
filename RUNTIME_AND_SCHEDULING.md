# Local runtime and scheduling

## Runtime design

The marketing OS runs on this Windows machine. Codex is the harness for development, agent coordination, review, and operator-directed work; scheduled and persistent execution is performed by local Python processes backed by SQLite. A live Codex chat is never used as a database, clock, or guaranteed background service. Lumen Sieve is reached through an integration adapter when available; hosting the product is outside this OS runtime.

Proposed executable roles are: **scheduler** creates due jobs; **worker** claims and runs bounded jobs; **inbox poller** obtains inbound events; **projection worker** updates Markdown/Sheets views; **monitor** checks lag, limits, and process health; **operator CLI/status view** shows queues, approvals, pause state, and replay options. A single binary/package may host several roles with separate commands. The 35 named agents are logical workflow handlers; they do not require 35 Windows processes or 35 always-open Codex chats.

## Process ownership

| Process | Reads | Writes | Restart rule |
| --- | --- | --- | --- |
| Scheduler (5A) | Approved schedule definitions, pause state, due times | Jobs and schedule-run ledger | Safe to rerun; unique logical job keys prevent duplicate due jobs. |
| Worker | Claimed job, inputs, role policy, adapter capability | Job attempt, domain records, outbox events, audit | Expired leases are inspected before requeue; external effects require reconciliation. |
| Inbox poller (4B/9A) | Mail/webhook/provider cursor | Normalized inbound events, cursor after commit | Re-poll may see duplicates; provider ID uniqueness keeps one event. |
| Projection worker (9C/reporting) | Committed domain/outbox records | Generated Markdown and authorized Sheets rows | At-least-once delivery with stable row/event ID; failed sync remains queued. |
| Monitor (7B/7C/7E) | Heartbeats, job state, quotas, integrity, connector health | Alerts, lane pause, audit | Repeated alerts are coalesced; it never silently performs a duplicate action. |
| Operator control | Status, drafts, approvals, incidents | Authenticated commands and audit | Commands use a stable request ID to avoid double application. |

Windows Task Scheduler (or a Windows service wrapper if later justified) starts the supervisor/periodic entry point at sign-in or machine startup and checks that it remains healthy. A process manager supervises worker restarts with bounded backoff. The precise Windows mechanism is selected during implementation and tested on this machine. When the PC is off, asleep, or disconnected, local OS work stops; on wake, the scheduler applies a defined catch-up policy and never dumps overdue actions into an uncontrolled burst. This is a real operational constraint, not a promise of continuous 24/7 service.

## Scheduling rules

- Store schedules as versioned configuration with schedule ID, owner role, interval or cron expression, active window, time zone, maximum overlap, catch-up policy, and last/next due time.
- Research/feed scans may use periodic schedules; inbox/support polls use shorter intervals or available event callbacks; analytics use daily/weekly windows. Exact frequencies come from measured load, provider limits, and approved objectives rather than fixed numbers copied from examples.
- Outreach uses the contact's verified IANA time zone and approved local window. The example 09:15 time in the drafts is a candidate setting, not a universal fact. When time zone is unknown, choose a configured fallback or require review.
- Use UTC for stored due times and event timestamps. Recompute local delivery windows across daylight-saving transitions. Discard or re-review actions whose approval, source evidence, or offer has expired during downtime.
- Deduplicate each scheduled run by `(schedule_id, scheduled_occurrence_utc)` and each downstream action by its own idempotency key.

## Job lifecycle and leases

The scheduler inserts a durable `pending` job. A worker claims one job transactionally, sets `leased` with owner and expiry, and writes a job attempt. It heartbeats while working. On success it commits domain changes and outbox handoffs, then marks the attempt complete. On retryable failure it records a typed error and `retry_wait` due time. Permanent/ambiguous failures enter `needs_review` or `dead_letter` and alert the operator.

Retries are bounded per operation and use backoff with provider `Retry-After` where supplied. Retrying research, a read, or an uncommitted computation is generally safe; retrying a send/post/book action is safe only after confirming the first attempt did not take effect or when the provider accepts a stable idempotency key. An action with unknown provider outcome stays in `unknown` until reconciled. A lease expiring during a three-minute Lumen lookup is not evidence that its request failed: the lease/heartbeat budget must cover the lookup deadline plus processing margin.

SQLite is local and has a controlled write path. Use foreign keys, WAL where appropriate, short transactions, busy timeout, and explicit transaction boundaries; never hold a write transaction open across a network call. Event handoffs are committed through an outbox in the same transaction as the source state change. The outbox dispatcher may deliver twice after a crash, so every consumer deduplicates by event ID.

## Global pause and lane controls

`PAUSE_ALL.lock` at the workspace root is an immediate local operator signal. The database also records global/lane pause state, reason, actor, and time. All processes check pause before claiming new work; external action handlers recheck before the provider call. A monitor can pause a problem lane (for example email after a provider denial) without stopping unrelated research. If the global file and database disagree, the more restrictive state wins. Resume is an explicit operator command: it revalidates pending actions before any dispatch.

Pausing during an external request cannot recall a request already accepted by the provider. The system records this boundary and reconciles the outcome. Pause does not erase jobs, approvals, or audit history.

## Failure modes and recovery

| Failure | Expected behavior | Operator-visible evidence |
| --- | --- | --- |
| PC restart/power loss | Processes restart; jobs recover from committed state; expired leases inspected. | Last healthy heartbeat, interrupted jobs, recovered run count. |
| Network/provider outage | Affected adapter lane enters retry/backoff; other local lanes continue. | Adapter status, last error class, next retry time. |
| Lumen lookup reaches three-minute bound | Attempt times out according to verified contract; queue remains responsive. | Attempt duration/status and prospect review/retry state. |
| SQLite busy/corruption | Busy writes retry briefly; integrity failure pauses writes/actions and triggers restore procedure. | Integrity result, backup age, incident ID. |
| Sheet projection outage | Local outbox retains changes; retry stable row upserts. | Sync backlog and oldest unsynced item. |
| Model spend/quota exhausted | Stop requests to that provider; affected tasks remain queued/reviewable. | Usage ledger and limit/period. |
| Worker hang or repeated error | Sentinel marks lane unhealthy and alerts; bounded restart or manual replay. | Lease/heartbeat age, error history and recovery action. |
| Unknown email/post outcome | Hold action for reconciliation; do not automatically resend. | Provider request key, attempt timeline and resolution. |

## Observability and local operations

The status view shows global/lane state, next schedules, jobs by state, oldest backlog, last success/error by adapter, approval queue, quota and spend, pending suppressions, projection backlog, and backup age. Logs use correlation/job/action IDs and redact secrets and private message bodies. Health alerts should reach the operator locally even if Sheets is unavailable; duplicate alerts are grouped. `TEST_AND_OPERATIONS_PLAN.md` defines restore drills and activation checks.
