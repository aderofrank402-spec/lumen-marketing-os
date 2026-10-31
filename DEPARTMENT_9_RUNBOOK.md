# Department 9 — Product Feedback, Triage & Support

Department 9 uses the pre-existing `support_tickets` table as the authoritative ticket store and extends it with source identity, idempotent event keys, versioned classification, reviewable responses, and Google Sheets projection state. It does not require the Lumen Sieve product to be online. No product-event source is active until a real signed source has been verified. The connected Codex Google Workspace account is **not** a Python OAuth credential: the local CLI produces exact records, and an authorized Codex run handles connector reads/writes.

Four roles are defined in `.codex/config.toml` and `DEPARTMENT_9_AGENT_GUIDES/`. A role must read its guide before work. No timer or source registration alone launches a Codex subagent. A local support lane pause blocks mutations; Gmail intake also honors the existing inbox lane. Never store credentials in task files or ticket text.

## 9A — intake

1. Verify the intended Lumen Sieve Gmail identity, currently `lumensieve@gmail.com`, and the existing Department 4 outreach account. The source must be bound to that account and enabled only after access is checked. Register with `lmos support register-source --key lumen-gmail-support --kind gmail --reference lumensieve@gmail.com --account-id ACCOUNT_ID --actor owner`, then `support enable-source SOURCE_ID --verified --actor owner`.
2. Read a bounded provider page with the authorized Codex connector. Normalize it into Department 4 `IncomingMail` shape and ingest it through `lmos outreach ingest-file ACCOUNT_ID --file PATH --cursor CURSOR`. This is a local fixture/import path, not a direct provider call. For each new support-worthy `inbound_event`, place `{provider_event_id,inbound_event_id,received_at_utc}` in a JSON array and call `lmos support ingest-file SOURCE_ID --file PATH --next-cursor CURSOR --actor 9A`. The batch commits tickets and cursor atomically. Duplicate provider IDs return the existing ticket. Do not fabricate messages or import unrelated inbox content.
3. For a verified form/webhook later, register a separate source; keep the product-event source disabled until Lumen Sieve is live. Non-Gmail events use subject/body/sender and an exact source event ID. Never use an email address alone as an idempotency key.

## 9B — triage

Run `lmos support triage TICKET_ID --actor 9B`. The rule classifier records version, confidence and rationale. Low-confidence, billing and possible security incidents go to `needs_review`. Human reviewers use `lmos support review TICKET_ID --category CATEGORY --priority PRIORITY --state STATE --rationale TEXT --actor NAME`. Changing a ticket increments its version and retires prior approved replies. The classifier does not change code or customer accounts.

## 9C — Google Sheets projection

The owner should identify one private support spreadsheet and exact tab. `lmos support sheet-projection TICKET_ID --spreadsheet-id ID --actor 9C` yields a stable row key, version, hash and minimal metadata fields (ticket ID, subject, category, priority, state, received and updated times). The body and sender stay local. Upsert by ticket ID, read the row back, then call `lmos support sheet-ack TICKET_ID --spreadsheet-id ID --version N --hash HASH --actor 9C`. A changed ticket invalidates the old acknowledgement. Failed connector access leaves a pending projection; manual Sheet edits do not flow backward into SQLite. No spreadsheet ID has been assumed or created.

## 9D — response

Use `lmos support ticket TICKET_ID` and verify current product status before drafting. Create an internal draft with `support draft-response TICKET_ID --subject TEXT --body-file PATH --author 9D`. An independent reviewer compares the exact body and evidence, then calls `support approve-response DRAFT_ID --hash HASH --reviewer NAME`. For Gmail-linked tickets only, `support handoff-email DRAFT_ID --actor 9D` creates or reuses a Department 4 response proposal. It does **not** review the Department 4 proposal, prepare a send action, approve a send, or dispatch an email. Those are separate existing gates. Form or product-event replies remain manual until a verified reply transport exists.

## Checks and recovery

Use `lmos support report`, `lmos support ticket ID`, `lmos check`, and the support test suite. Connector outages leave local tickets and pending projections intact. Re-read provider IDs/cursors before retrying; never advance a cursor based only on a browser page. During recovery, repeated ingestion, draft and sheet acknowledgement are idempotent for the same inputs. `VERIFICATION_REGISTER/DEPARTMENT_9.md` tracks live checks still needed.
