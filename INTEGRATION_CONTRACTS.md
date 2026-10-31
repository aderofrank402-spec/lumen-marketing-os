# Integration contracts

## Boundary and adapter contract

Agents request capabilities through typed adapters; they do not manage credentials or construct arbitrary network calls. Each adapter exposes `probe()` (configuration and reachability), `execute(request, idempotency_key, deadline)`, and a normalized result with `status`, `provider_reference`, `observed_at_utc`, `usage`, `provenance`, and a redacted `error`. For read streams, an adapter also exposes `poll(cursor, limit)` and returns a next cursor only after the OS commits the events. Exact Python method signatures may differ, but these semantics must survive implementation.

Adapters emit one of: `ok`, `not_found`, `ambiguous`, `unauthorized`, `rate_limited`, `transient_error`, `permanent_error`, or `unknown_effect`. `unknown_effect` is mandatory for an action whose request may have reached the provider but whose result was lost. Adapter failures are never interpreted as permission to scrape a different route, fabricate data, or resend an uncertain action.

All connectors support a fixture/dry-run implementation and a capability probe. The live adapter is enabled only after account authorization and its contract tests pass. IDs, cursors, and URLs returned by outside services are treated as untrusted data; logs redact tokens, personal message content, and secret-bearing URLs.

## Connector inventory

| Adapter | Consumers; capability | Normalized input → result | Authentication / validation / outage behavior |
| --- | --- | --- | --- |
| Lumen Sieve lookup | 2A; business enrichment | Evidenced business name plus country and request ID → raw response reference, normalized fields, ambiguity, latency, cost. | Verify real endpoint, auth, payload and response against product code/service; support up to three minutes. Missing/ambiguous identity goes to review; timeout is recorded as an attempt. This adapter is not an OS host. |
| RSS and permitted web pages | 1A/1C/8A/8B; research | Source URL/cursor and query → observation IDs, excerpts, timestamps, canonical links. | Respect source access terms, rate limits and robots where applicable; retain provenance; unavailable source yields stale/retry status. |
| Reddit | 1B/6A; discover and draft response | Approved community/query/cursor → discussion IDs and context; separately approved publication request if supported. | Use authorized account/API capability; store OAuth scopes and expiry outside records. An account/browser session alone does not authorize unattended posting. A denied request pauses its lane. |
| Hacker News | 1B/6B; trend discovery and draft | Query/item IDs/cursor → story/comment metadata with canonical IDs/URLs. | Read via permitted public interface; publication is a separate manual or verified supported action. Duplicates resolved by item ID. |
| Product Hunt and launch directories | 1E/6C; discovery, launch monitoring, preparation | Approved source and query → launches/organizations/events; launch asset/checklist drafts. | Confirm provider API entitlement before live calls; unavailable API produces reviewable research gap rather than unapproved scraping fallback. |
| LinkedIn | 6D/6E; research and draft, possible approved action | Operator-supplied or authorized source context → observation/draft; action only if an allowed, supported interface is confirmed. | A logged-in browser is useful for supervised research but is not a general unattended extraction/posting API. Access denial or challenge stops the lane. |
| Contact provider (for example Hunter) | 2C; business contact research | Verified domain/organization and role → sourced contact candidate and verification status. | Use purchased/authorized scope; distinguish provider confidence from confirmed deliverability; quota/absence does not authorize guessing. |
| Email send | 4A/4C/9D; outbound action | Exact approved recipient, subject/body hash, thread reference, action key → provider message ID or unknown-effect result. | Use authenticated provider method with TLS/OAuth or supported SMTP. Verify sender domain/readiness and bounce/complaint handling. Never retry unknown-effect without reconciliation. |
| Email inbox | 4B/9A; read replies/support | Account/folder/cursor → message ID, thread, sender, received time, body reference, flags and next cursor. | Use authorized account scopes; persist cursor after database commit; duplicate provider ID is ignored. Include out-of-office, bounce, opt-out classifications. |
| Google Sheets/Drive | 2B/7D/9C and reporting; dashboard projection | Stable local record ID/version → target spreadsheet/tab/row upsert result. | Least-privilege access to named files; use stable ID columns, never `clear()` a tab to sync. On outage, retain an outbox record and retry idempotently. |
| Calendar | 4C; availability and optional booking | Prospect time zone plus configured event type → available slots; approved booking request → booking ID. | Verify account permission, time zone, and slot freshness; booking requires exact approval. On outage, draft a response without invented availability. |
| Model providers | 1A/3A/3D/4B/8A/8B/9B and others; reasoning/classification | Versioned task instruction, curated evidence, output schema, spend reservation → parsed typed result and usage. | OpenAI/Gemini or another configured provider are replaceable adapters. Model names, prices and limits are configuration, not facts copied from draft examples; invalid JSON or budget exhaustion fails closed. |
| Product/support events | 9A/7D; feedback, trial, payment verification | Authorized product event/webhook/export → source ID, account ID, event type, evidence time. | Verify event origin/signature where available; trial and payment are distinct; only verified successful payment produces `conversion.verified`. |
| Notifications/operator surface | 7B/7E/8D; status and urgent alerts | Alert severity, job/action IDs, redacted summary → delivery result. | Local notification is primary; dashboards can mirror status. Alert failure is logged and visible in local status. |

## Common request and result shapes

```text
AdapterRequest:
  correlation_id, operation, actor_role, policy_version,
  logical_entity_id, idempotency_key, deadline_utc,
  payload_reference, dry_run

AdapterResult:
  status, provider_reference?, observed_at_utc,
  normalized_payload_reference?, evidence_references[],
  usage_units?, cost_estimate?, retry_after_utc?,
  error_class?, redacted_diagnostic?, unknown_effect=false
```

The local coordinator validates that a role has the requested capability; read adapters have no route to an action method. External action adapters require an `action` row and a current central gate decision. A dry-run adapter records what would be attempted without calling the provider.

## Synchronization ownership

SQLite owns leads, tickets, messages, approvals, and results. Sheets receives projections for `Lumen_Target_Leads`, `Campaign_Analytics`, and `Lumen_Support_Tickets` (canonical names chosen here; a draft used `Lumen_Support_Ticks`). Every projected row includes the local stable ID and version. Human edits to a Sheet do not silently overwrite local state: if a field is intended as operator input, it is ingested as a separately authenticated command with validation and audit. Dashboard access is optional to the basic local workflow, but requested as part of the full OS and must be tested before its workflow is called complete.

## Contract verification before live use

For each connector, record the real account owner, granted scope, endpoint/method, required fields, representative success and failure payloads, observed latency/rate limits, cost, and terms of permitted use. Then test: absent credentials, unauthorized/expired credentials, malformed payload, throttling, timeout, duplicate event, restart after cursor commit, and provider outage. For an action connector, also test unknown outcome, suppression after queueing, approval expiry, and dry-run isolation. These facts are verified during adapter implementation; guessed paths or sample credentials from planning PDFs are never treated as live configuration.
