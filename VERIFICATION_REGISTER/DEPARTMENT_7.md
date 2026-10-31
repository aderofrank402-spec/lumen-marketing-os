# Department 7 — verification register

## D7-01 — Local implementation and enforcement

- **Status:** closed.
- **Check:** Implement 7A–7F durable records and commands; enforce exact-hash 7A checks, quota/provider-health and suppression at the Department 4 action gate; keep scans and recommendations non-destructive.
- **Evidence/result:** On 2026-10-03, migration `0013_audit_control.sql`, `AuditRuntime`, fourteen `lmos audit` command paths, six Codex role configurations and `DEPARTMENT_7_RUNBOOK.md` were present. The cross-department suite collected 127 tests and passed. Controlled tests cover a clean/blocked/unavailable 7A checker; current unhealthy then healthy 7B gating; a 7C merge cycle; low-sample 7D reporting; overdue/unknown-effect 7E detection without replay; and keyed 7F matching/fail-closed behavior. The pre-migration database backup is `runtime/backups/pre-department7-2026-10-03.db`. Verified post-migration backup `60a06079119245be9e147ce474ae3b9a` passed isolated restore drill `01e97b4fe6a34266bee2374fd76481dd`; the final Department 5 health check was clean.

## D7-02 — Recurring control execution and recovery

- **Status:** open.
- **Check:** Prove the fifteen-minute local schedule continues across an actual Windows/app restart and that a missed occurrence catches up once without duplication.
- **Evidence/result:** The active schedule `audit-department7-15m` created due job `a64e22f702724f7888a06002d5cbb47b` on 2026-10-03; Department 5 executed `audit.tick`, and the job reached `succeeded`. Its 7C run `9860249f778d4cd796f505d8718b6d0b` and 7E run `a10c0c57392a4983bac067c312f289e8` both passed with no findings. This verifies the local scheduler handoff, not restart persistence; the Windows supervisor lifecycle remains a Department 5 dependency.

## D7-03 — Real provider/domain reputation telemetry

- **Status:** open.
- **Check:** When an authorized email provider/domain telemetry source is connected, record actual healthy, degraded and unhealthy observations; verify timestamps/expiry and confirm the gate response in a controlled non-sending canary.
- **Evidence/result:** The durable 7B record and action-gate behavior are tested with controlled fixtures. No real provider-health snapshot or active outreach quota exists in the current database, so no health claim has been fabricated. Missing telemetry cannot increase a quota; a current unhealthy/unknown record blocks, and degraded status halves the configured usable limit.

## D7-04 — Real growth and compliance outcomes

- **Status:** open.
- **Check:** After real reviewed campaigns have enough attributed actions and verified paid conversions, run 7D over an agreed window and review its channel/cost recommendations; separately run 7F retention review under the obligations that apply to the actual markets used.
- **Evidence/result:** 7D calculations and low-sample labeling pass synthetic tests and never change allocations. 7F keyed suppression, normalization and no-automatic-deletion behavior pass. The current database has no qualifying real campaign sample, and market-specific legal conclusions are not represented as verified facts.
