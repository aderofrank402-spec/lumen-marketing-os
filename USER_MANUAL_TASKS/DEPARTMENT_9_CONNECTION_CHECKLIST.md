# Department 9 — connection decisions still needed

The local Department 9 build and tests are in place. These are the specific external decisions or proofs needed before its connected flows can be called live:

1. Confirm that `lumensieve@gmail.com` is the support mailbox to monitor and specify the label or bounded query that should count as a support request. Codex can read that connected account; it does not give the local Python daemon an OAuth token. The local Department 4 account and suppression key must be configured before a real Gmail message can be ingested into the ticket ledger.
2. Identify an existing private Google support spreadsheet and tab, or authorize a new private one. Department 9C will write only ticket metadata keyed by ticket ID, never the message body or sender by default.
3. When Lumen Sieve goes live, provide its authenticated product-event/webhook/export contract and signing information; this is not needed for the local ticket path today.

No response will be sent merely because a ticket was classified, drafted, approved for content, or synchronized to a sheet. The separate Department 4 exact-action gate remains in force.
