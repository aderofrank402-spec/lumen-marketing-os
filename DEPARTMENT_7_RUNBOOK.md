# Department 7 — System Auditor & Executive Controller

Department 7 is a local, durable control plane. It creates audit evidence, blocks unsafe actions, and proposes improvements; it does not send, publish, change campaign allocation, or grant its own approvals. The six logical roles are declared in `.codex/config.toml` and share `lmos audit` plus migration `0013_audit_control.sql`.

## Operating sequence

1. Run `lmos audit init-policy` once after migration. A policy is versioned and immutable after activation; changed rules require a new version.
2. **7A** runs `lmos audit guard DRAFT_ID` on the exact immutable draft hash. Passing results create the `reviewer_role='7A'` content review required by Department 4; warnings stay in review and blocking findings cannot authorize an action.
3. **7B** reads `lmos audit rate-report` and records verified health via `record-health`. Existing hard quotas remain authoritative. A current unhealthy/unknown status blocks dispatch; degraded status reduces the usable quota to half, never below one. Absence of a health record does not fabricate health or increase quota.
4. **7C** runs `integrity-scan`. It checks SQLite and foreign keys, orphaned running attempts, duplicate provider-message identities, and organization merge cycles. It writes findings only and never repairs destructively.
5. **7D** runs `growth-audit --starts-at ... --ends-at ...`. It reports durable actions, accepted/rejected/unknown outcomes, trials, verified paid conversions, revenue and recorded provider cost. A low sample is labeled; every recommendation is non-executing.
6. **7E** runs `pipeline-scan`. It detects overdue jobs, expired leases, stale process heartbeats and unknown external actions. It never auto-replays uncertain effects.
7. **7F** uses `suppression-check` at review/diagnostic time while Department 4 enforces the same HMAC-normalized vault immediately before dispatch. `retention-review` identifies expired records without deleting them automatically.

`lmos audit full-scan` executes the read-only 7B report plus 7C, 7E and 7F retention checks. `audit.tick` is an allowlisted Department 5 schedule workflow for recurring local scans. Every run, finding, policy, guard review, health snapshot, growth report and compliance event is stored in SQLite and represented in the general audit log.

## Safety and recovery

- Global/lane pause, exact approval, suppression, quota, current account policy, content hashes and idempotency remain enforced at the point of action.
- Provider-health records must be backed by actual observed telemetry; do not invent a status to unblock dispatch.
- Resolve a finding only after its cause is addressed, using `lmos audit resolve-finding FINDING_ID --resolution ... --actor ...`.
- A failed scan or checker creates evidence; it is not treated as a pass. Database repair, suppression deletion, campaign reallocation, limit increases and external actions are outside automatic Department 7 authority.
