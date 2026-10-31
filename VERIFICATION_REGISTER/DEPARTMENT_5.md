# Department 5 — operational verification

## D5-01 — Windows supervisor lifecycle

- **Status:** open.
- **Trigger:** Operator chooses to install the provided Scheduled Task on this Windows machine.
- **Check:** Preview and inspect `scripts/register_department5_task.ps1`; install for the intended user; verify one instance at logon, restart after process failure, pause/resume, sleep/wake catch-up, workstation sign-out and stale-heartbeat alerting. Inspect Task Scheduler history and SQLite occurrences/jobs.
- **Close when:** Dated host evidence confirms no duplicate jobs, correct catch-up and recovery across reboot and sleep, and the operator confirms the run policy.
- **Evidence/result:** Unit tests cover duplicate ticks, restart-style re-instantiation and pause. No Windows task was installed by Department 5 implementation.

## D5-02 — Real provider/model usage and bill reconciliation

- **Status:** open.
- **Trigger:** A supported provider usage export or invoice is available and real Codex/provider usage is authorized for collection.
- **Check:** Map provider event IDs, input/output tokens, request counts, price-effective time and currency into the ledger; compare sum of actual cost to the provider bill, track missing/late records and review any discrepancy. Validate that no unknown usage is represented as zero spend.
- **Close when:** A dated source-to-ledger reconciliation has documented coverage and discrepancy thresholds; the ingestion connector, if needed, is tested against the authorized account.
- **Evidence/result:** Local pricing, idempotency, unknown-status and reconciliation tests pass. No live billing API or Codex usage telemetry has been connected.

## D5-03 — Protected backup and disaster restore

- **Status:** open.
- **Trigger:** Operator selects an access-controlled backup location and retention policy.
- **Check:** Run daily scheduled backup and weekly isolated restore drill; verify encrypted/protected storage, retention, off-machine disaster copy, age alerts and a full recovery exercise without overwriting the live DB. Audit exposure of personal/inbox records.
- **Close when:** Dated backup/drill logs and a separate-host restoration prove recoverability and the operator approves retention and access controls.
- **Evidence/result:** On 2026-10-01, 96 local tests passed. Migration 0010 was applied after `runtime/backups/pre-department5-2026-10-01.db` was verified. A post-migration verified backup at `runtime/backups/post-department5-2026-10-01.db` (backup ID `b93fcff9b21c48beb7f1513cb9df5cf8`) passed an isolated restore drill (ID `712ecf72523f466fb372d916b5e70956`). Off-machine recovery, storage controls and scheduled cadence remain unverified.
