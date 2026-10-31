# Department 5 — local operations and accounting

Department 5A owns local timer-to-job orchestration. Department 5B owns price versions, usage records, verified backups, and isolated restore drills. Codex roles in `.codex/config.toml` inspect and guide; the Python process and SQLite database supply persistence. The Windows task is optional and is **not** installed by adding this code.

## Schedule lifecycle

Inspect `lmos status`, `lmos operations report`, and `lmos jobs` first. A schedule is created inactive by default. Every schedule needs a named owner, an allowlisted internal operation, a first due timestamp with timezone, positive interval, timezone, catch-up policy, maximum outstanding jobs and, optionally, local active hours/weekdays. UTC occurrences are persisted with a unique `(schedule_id,due_at_utc)` key. A due occurrence and its queue job commit together. The timer checks global and operations-lane pause before creating work; the worker uses the existing queue lease and pause gate.

```powershell
.\.venv\Scripts\lmos.exe operations configure-schedule research-hourly research.tick --interval-seconds 3600 --next-due-at 2026-10-02T09:00:00+03:00 --timezone Africa/Nairobi --catch-up one --owner operator --max-overlap 1
.\.venv\Scripts\lmos.exe operations report
.\.venv\Scripts\lmos.exe operations set-schedule-active research-hourly --active --actor operator
.\.venv\Scripts\lmos.exe operations run
```

For reconfiguration, pass `--expected-version` from the current report. `--catch-up skip` discards a multi-interval backlog; `one` runs the latest missed occurrence; `all` drains no more than 24 occurrences per tick by default and resumes the backlog on later ticks. An outstanding queued/leased/retry job consumes a `max_overlap` slot; excess occurrences are recorded as skipped, not silently retried. Local active windows use the selected IANA timezone and therefore follow daylight-saving transitions; UTC due instants remain unambiguous. No arbitrary shell command or direct external send can be scheduled. Allowlisted operations are Department 1/2/3 ticks plus backup, restore drill and DB integrity check. Department 4's approved action gate remains separate.

`lmos operations tick` queues due work; `work` handles one operations job; `run` ticks and drains; `daemon` repeats locally. For logon startup, inspect the exact script and preview `powershell -File scripts\register_department5_task.ps1 -WhatIf`; only install with an operator decision using `powershell -File scripts\register_department5_task.ps1`. It registers a task for the current interactive Windows user, restarts on failure and ignores duplicate process launches. This does not imply 24/7 availability while the computer is off, asleep or the user is signed out. Check Task Scheduler history and `lmos operations report` heartbeat after logon/resume. Pause with `lmos pause --actor ... --reason ...` or `PAUSE_ALL.lock`; do not kill a process mid-provider action as a substitute for the gate.

## Prices and usage

Prices are immutable, approved versions in integer **minor currency units** per million input/output tokens plus per request. Actual provider bills can be reconciled later without overwriting estimates. Unknown usage, unknown price or missing token count remains `unknown`, never a fabricated zero. Use provider event IDs as `usage_key` so re-imports are idempotent. Do not put raw prompts, credentials, personal data or provider response bodies in the ledger.

```powershell
.\.venv\Scripts\lmos.exe operations approve-price vendor model USD --prompt-per-million-minor 100 --completion-per-million-minor 300 --request-minor 0 --effective-at 2026-10-01T00:00:00Z --actor operator
.\.venv\Scripts\lmos.exe operations record-usage provider-event-001 vendor model --prompt-tokens 1500 --completion-tokens 400 --requests 1 --occurred-at 2026-10-01T12:00:00Z
.\.venv\Scripts\lmos.exe operations reconcile-usage provider-event-001 --actual-minor 2 --currency USD --actor finance
.\.venv\Scripts\lmos.exe operations ledger-report
```

The OS cannot observe Codex account token usage or a provider invoice automatically without a supported telemetry/billing connection; that live source validation is tracked in the verification register. The local database still records provider-call reservations in prior departments; do not confuse those counts with complete token or cost reconciliation.

## Backup and recovery

`lmos operations verified-backup DESTINATION` uses SQLite's consistent backup API, checks integrity, hashes the file and records metadata. `lmos operations restore-drill BACKUP_ID` validates the checksum and copies into a temporary isolated database for integrity/migration checks; it never overwrites the live DB. `operations.backup` and `operations.restore_drill` can be scheduled (for example daily and weekly). Keep backup files in a protected local location; they may contain prospect and inbox data. Test recovery on an isolated computer and do not restore over a live database until an incident plan and operator approval exist. Monitor the newest backup and drill in `operations report` or `ledger-report`; a successful backup command alone is not a completed recovery test.

Run `scripts\verify.ps1` and `lmos check` after migrations or repair. `lmos operations health` returns a nonzero status when the scheduler heartbeat is stale (if schedules are active), the verified backup is older than 36 hours, the last passing drill is older than eight days, jobs need review, or usage remains unknown. It is an inspection signal, not an automatic repair mechanism. Any migration failure, unknown price, failed restore drill, stale heartbeat, dead-letter job or duplicate task instance warrants operator review before increasing automation.
