# Department 4 — Outreach & Inbox Orchestration

Department 4 comprises 4A Outreach Dispatcher, 4B Inbound Intent Classifier, and 4C Responder/Auto-Scheduler. The implementation is a local, durable action gate and inbox/reply/booking workflow. **No live Gmail or Calendar provider is configured yet.** The user's Codex connections cannot be reused as credentials by the separate local Python worker; Google OAuth, adapter wiring, and live checks remain explicitly open in [D4-01](VERIFICATION_REGISTER/DEPARTMENT_4.md). Development, staging and live configuration currently have `external_actions_enabled=false`, so nothing sends or books.

## Operating model

1. A reviewed Department 3 email draft and a verified Department 2 contact are inputs to `prepare-send`. A reply instead starts with a deduplicated inbound message, classification, a 4C response proposal, and human review of that proposal. A response proposal is not an outbound action.
2. `prepare-send` snapshots the exact sender account, recipient, subject, body, content hash, thread and source message as an immutable binding. The corresponding action starts in `review_pending`; it is never sent merely because a draft passed review.
3. Department 7A must first record a passing guard review for the exact email draft hash. The operator then inspects `outreach action ACTION_ID` and explicitly approves the exact action with an expiry and reason. Approval binds target, draft, hash, channel, action kind, and policy version. Any change of recipient or content requires a new action and approval. Since Department 7 is not implemented yet, real email approval remains blocked.
4. The dispatcher checks global/lane pause, account authorization, exact approval, draft review, current identity/contact, suppression, approved recipient-local window, and quota before reserving capacity. Cold outreach requires an explicit IANA recipient time zone and whether it is verified or an approved account fallback. It checks pause and suppression again just before the provider call. Network work never runs within a SQLite transaction.
5. Accepted results persist the provider ID and delivery attempt; rejected results release quota; unknown results hold quota and require read-only provider reconciliation. A stale `dispatching` action is moved to `unknown`, never silently retried. `reconcile` cannot send or book.
6. 4B commits inbound message, body, classification, and cursor in one transaction. Repeated provider message IDs are deduplicated. Clear opt-outs and complaints create keyed address suppression and cancel unsent actions. Ambiguous or non-obvious cases require 4B/operator review. Support-like messages emit an internal handoff; no support reply is sent by classification alone.
7. 4C can propose reply text and verified live free/busy slots. Without an authorized calendar adapter it must propose no slots. Booking is a separate action with attendee, calendar, exact time interval and operator approval; availability is checked again just before booking.

## Local setup and inspection

Apply migration `0008_outreach.sql` with `lmos init` after backing up any operational database. Supply a stable, private 32-byte-or-longer suppression HMAC key as `LMOS_SUPPRESSION_HMAC_KEY_HEX` in the ignored `.env`; back it up securely because changing it prevents matching older suppression entries. Do not put the key in Markdown, Git, logs, or chat. The CLI refuses Department 4 commands without it.

Representative commands (replace IDs, policy and addresses with reviewed values):

```powershell
.\.venv\Scripts\lmos.exe outreach register-account --email lumen@example.com --policy-version p1 --calendar-id primary --actor operator
.\.venv\Scripts\lmos.exe outreach set-send-window ACCOUNT_ID --timezone UTC --start 09:00 --end 17:00 --weekdays 0,1,2,3,4 --actor operator
.\.venv\Scripts\lmos.exe outreach set-quota ACCOUNT_ID --max-units 10 --period-seconds 86400 --policy-version p1 --actor operator
.\.venv\Scripts\lmos.exe outreach report
.\.venv\Scripts\lmos.exe outreach action ACTION_ID
.\.venv\Scripts\lmos.exe outreach inbound EVENT_ID
```

`enable-account --confirmed` is only a record of an independently verified Google connection; it does not itself create OAuth credentials. Do not use it until the live integration check succeeds. `ingest-file ACCOUNT_ID --file PATH` is for trusted fixture testing and manually reviewed import, not a substitute for a verified live inbox adapter. Such a file is likely to contain private message bodies and must remain outside Git and be removed according to the retention policy. `dispatch-once` cannot call a live provider until the gateway is wired and the environment is deliberately activated.

After the operator reviews an inbound event, `review-classification` records a corrected label and rationale. `propose-response` saves a draft; `review-response` records the human review; `prepare-send ... --source-message-id ID` binds the exact reply. `prepare-booking` binds an exact future attendee/slot. Both actions still need `approve ACTION_ID --expires-at ... --reason ... --actor ...`. Use `revoke` for unsent actions and `recover-dispatch` followed by `reconcile` for uncertain provider outcomes. Never use a provider UI or Codex Gmail/Calendar tool to work around a local gate denial.

## Activation and acceptance

Before any live send or booking, complete D4-01: obtain dedicated Google OAuth authorization for the local process, verify account identity and requested scopes, wire and test Gmail read/send and Calendar free/busy/book adapters, confirm token storage/revocation, configure sender DNS and complaint/bounce handling, and approve campaign send limits/windows and exact-action rules. Email activation additionally requires Department 7A to produce a passing exact-hash guard review; fixture tests insert a simulated 7A result solely to exercise Department 4's gate. The operator must approve a controlled live canary separately. A Codex app permission of “Allow low-risk actions” is not this worker authorization.

Fixture tests exercise approval binding, duplicate preparation, quota reservation, pause, suppression, inbound deduplication/cursor commit, response review, unknown-effect reconciliation, stale-dispatch recovery and booking freshness. They do not prove Gmail delivery, deliverability, calendar correctness, legal compliance, or real response quality. Those remain open until dated live evidence is recorded.
