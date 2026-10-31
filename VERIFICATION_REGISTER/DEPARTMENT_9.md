# Department 9 — verification register

## D9-01 — Local ticket path

- **Status:** verified locally on 2026-10-03.
- **Evidence:** Migration `0015_support.sql` extends the existing authoritative `support_tickets` table. Controlled tests cover transactional cursor+ticket commit, duplicate provider IDs, rollback on invalid page, pause, deterministic triage, security/billing escalation, versioned human review, independent response approval, stale draft retirement, sheet version/hash acknowledgement, and Gmail handoff to a Department 4 proposal without send. CLI intake and scheduled local tick are covered. The complete repository suite passed 137 tests on 2026-10-03. The pre-migration backup is `runtime/backups/pre-department9-2026-10-03.db`; `lmos init` applied the migration with integrity and foreign-key checks passing. Verified post-migration backup `92303594f9394fd0b0fbc16ddd9261d1` passed isolated restore drill `596b4f53e93441f0b8139e0465017f61`.
- **Remaining:** Exercise recovery after an actual interrupted connector page and high-volume support load.

## D9-02 — Gmail source and authenticated continuity

- **Status:** open.
- **Evidence:** The connected Codex Gmail profile returned `Lumen Sieve <lumensieve@gmail.com>` on 2026-10-03. A bounded read-only search addressed to that mailbox for support, bug or error subjects returned zero messages. The local database has zero registered `outreach_accounts`, and the local Department 4 CLI currently reports that a suppression key of at least 32 bytes is not configured. Codex connector access does not give the local Python daemon OAuth credentials. No live Gmail message was ingested and no cursor was advanced.
- **Close when:** The owner-approved Lumen Sieve account is registered and scoped, the existing local suppression key is safely configured, a bounded read-only Gmail page is ingested through Department 4 and 9A, account identity and cursor continuity are rechecked, and disconnect/retry behavior is observed. Do not infer that connecting Google Workspace alone completes this path.

## D9-03 — Google Sheets ticket projection

- **Status:** open.
- **Evidence:** Local tests verify stable ticket-ID row keys, version/hash projections, replay-safe acknowledgement, stale-version rejection, and pending state after ticket changes. No Google support spreadsheet was created or selected, and no external row was written. Ticket bodies and sender addresses are excluded from the default projection.
- **Close when:** The owner names or approves a private support spreadsheet and tab; 9C upserts one controlled ticket metadata row, reads it back, acknowledges the exact version/hash, retries the same row without duplication, and demonstrates outage recovery without allowing a sheet edit to overwrite SQLite.

## D9-04 — Responses and product events

- **Status:** open for live integrations; local approval gate verified.
- **Evidence:** A current approved Gmail response can create a Department 4 review-pending proposal, but no send action or delivery is created. After the Department 9 implementation and scheduled smoke run on 2026-10-03, the live local database had zero outreach actions and zero delivery attempts. Product-event ingestion is intentionally not connected because Lumen Sieve is offline.
- **Close when:** A real support inquiry passes source verification, classification, factual response review and the separate Department 4 exact-action gate; then independently verify provider outcome. Separately verify signed product webhook/export origin, trial-versus-payment distinctions and duplicate handling after Lumen Sieve goes live. Never use a fabricated product event as live proof.

## D9-05 — Local schedule

- **Status:** local dispatch verified on 2026-10-03; unattended continuity open.
- **Evidence:** `support-triage-15m` was activated under Department 5, due at 14:35 Africa/Nairobi. `operations tick` enqueued job `440ca5756315400989f5833fc06c94d2`; `operations run` completed it as `succeeded` with zero tickets to triage, and the next due instant advanced to 14:50 Africa/Nairobi. A second immediate tick returned `[]` (no duplicate); `operations health` was clean. This schedule neither polls Gmail nor sends replies.
- **Remaining:** Observe a truly unattended run with an actual local ticket, restart/catch-up, and pause behavior. Gmail connector wake-up and Sheet sync need separate Codex automation only after D9-02 and D9-03 are verified.
