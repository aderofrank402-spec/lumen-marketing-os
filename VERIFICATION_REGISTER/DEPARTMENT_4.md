# Department 4 — integration dependency

## D4-01 — Authorize local Gmail and Calendar workers

- **Status:** open.
- **Trigger:** Department 4's local Gmail and Calendar adapters are ready for live integration.
- **Dependency:** The operator has connected Gmail and Calendar to Codex, but the Codex app permission setting (currently “Allow low-risk actions”) does not itself authorize independent local Windows processes to use Google APIs.
- **Action:** Guide the operator through a Google Cloud desktop-app OAuth setup for the intended Lumen account, enable only the required Gmail and Calendar APIs/scopes, and complete Google consent in the browser. Keep credentials and refresh tokens out of the repository and this register.
- **Check:** Verify the authorized account and permitted inbox read, availability read, and any separately approved send/booking capabilities against the live adapters. Confirm the local worker can resume after restart without exposing tokens; test revocation and failure behavior.
- **Close when:** A dated live integration check records the account identity, scopes, adapter results, token storage location by reference (not value), and operator approval of sender, send limits, and booking rules. No live message or meeting is created just to test a connection without explicit approval.
- **Evidence/result:** pending.

## D4-02 — Live send, reply, bounce and deliverability canary

- **Status:** open.
- **Trigger:** D4-01 is complete; Department 7A's exact-hash guard is implemented; sender identity, DNS, approved claims, recipients, rate/window policy and legal review are approved.
- **Check:** With an explicitly approved controlled recipient, verify exact-action approval, Gmail acceptance and provider ID, reply threading, inbox ingestion, bounce/complaint handling, suppression, quotas, provider throttling and an unknown-effect reconciliation drill. Inspect actual inbox placement and sender health; do not treat API acceptance as delivery.
- **Close when:** Dated provider and local audit evidence verifies the canary and all failures or ambiguities are resolved without duplicate mail.
- **Evidence/result:** pending. No live message has been sent by Department 4.

## D4-03 — Live calendar availability and booking

- **Status:** open.
- **Trigger:** D4-01 is complete and the operator approves the calendar, event rules and a controlled booking test.
- **Check:** Verify time-zone conversion, daylight-saving boundary, live free/busy, slot expiry, exact booking approval, attendee/calendar identity, provider event ID, cancellation/revocation behavior and uncertain-result reconciliation.
- **Close when:** A dated controlled booking and audit record demonstrate no duplicate or unapproved event.
- **Evidence/result:** pending. No calendar event has been created by Department 4.

## D4-04 — Inbound classification and no-contact audit

- **Status:** open.
- **Trigger:** Authorized mailbox access and representative real inbound traffic are available.
- **Check:** Human-review a sample containing interest, questions, meeting intent, support, bounce, out-of-office, explicit opt-out and complaint. Measure misclassifications, confirm cursor/dedup recovery, and verify no-contact signals cancel pending actions across relevant channels before another send.
- **Close when:** Dated results, residual errors and corrective rules are recorded; no opt-out/complaint is left dispatchable.
- **Evidence/result:** pending. Fixture classification tests do not establish real-world accuracy.

## D4-05 — Market-specific outreach and data-handling policy

- **Status:** open.
- **Trigger:** The operator chooses target markets and live campaigns.
- **Check:** Confirm applicable outreach, unsubscribe, privacy, data-retention and recordkeeping obligations with qualified guidance as needed; encode approved limits and notices in policy. Review local inbox-body storage, backup protection and deletion schedule.
- **Close when:** The operator records the chosen markets, dated policy decision, configured controls, and a test showing prohibited actions fail closed.
- **Evidence/result:** pending. This register does not substitute for legal advice or market approval.
