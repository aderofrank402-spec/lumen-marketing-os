# Department 1 authenticated Reddit research handoff

Department 6A owns the authenticated browser session; Department 1B owns research review. The session is not a local daemon credential. Each scheduled 6A task must check the visible account against its task's `account_reference` before reading Reddit. The existing daily 6A trigger is the handoff; do not create a second polling loop or use private endpoints. Respect global, community, and `research.1B` pauses. Read only a small, relevant set of discussions and stop on access denial or rate warnings.

## Selection and analysis

Look for first-hand, recent reports of a specific business-information problem: incorrect company/domain matches, stale contacts, unclear company identity, supplier verification, or similar. Read the original post and enough comments to find workarounds, consequences, or a solution already offered. Prefer a concrete business workflow over a keyword mention. Do not collect profiles or treat a username as a verified buyer. Keep a session cover note with date, communities/queries checked, discussions reviewed, imported signals, and no-match reason when applicable.

For each worthy discussion, record a `community observe` item under the live leased 6A task, using the original HTTPS Reddit discussion permalink, visible thread ID, title, a short evidence excerpt, source observation time, and a relevance note. Then create a UTF-8 JSON analysis file (no secrets or personal contact list) with exactly these required keys; use `unknown` or `none` when evidence is absent, not speculation:

```json
{
  "post_date": "date as displayed on Reddit, or unknown",
  "problem": "one or two sentences in your own words",
  "business_workflow": "business activity affected, or unknown",
  "workaround_or_consequence": "observed workaround or consequence, or unknown",
  "buyer_evidence": "only explicitly evidenced role or organization, or unknown",
  "urgency_or_buying_signal": "source-backed signal, or none",
  "fit_rationale": "why this might relate to Lumen Sieve; mark product fit unverified when appropriate",
  "counter_evidence": "why it may not fit or may already be solved",
  "confidence": "low",
  "confidence_reason": "one source-based reason",
  "next_step": "investigate_use_case"
}
```

`confidence` is `high`, `medium`, or `low`; `next_step` is `investigate_use_case` or `verify_organization`. The latter does **not** verify the organization; it requests separate identity review. Do not submit `no_action` discussions. All fields must be nonempty strings of at most 1,500 characters. Do not copy a full post into analysis. Preserve uncertainty and counter-evidence.

## Durable handoff

From the Desktop workspace root, while the 6A task lease is still live:

```powershell
.\.venv\Scripts\lmos.exe research import-reddit-observation TASK_ID OBSERVATION_ID --worker WORKER --analysis-file PATH_TO_JSON
.\.venv\Scripts\lmos.exe research finding FINDING_ID
```

The first command atomically saves a Department 1B source observation, reviewable finding, and `strategy.review` outbox event. It requires the observation to belong to the leased 6A task and the task's account reference to match the registered Reddit account. Repeating the same import returns the existing finding and creates no duplicate handoff. Do not interpret `created: false` as a fresh finding. Review with `lmos research review FINDING_ID --reviewer NAME --decision reviewed|rejected` after checking the source and reasoning. A reviewed signal is still not an outreach target or proof of demand.

If a thread suggests no useful public reply, finish the 6A task as `no_match` and cite the saved research finding ID in the reason. If a helpful reply draft is justified, use the separate community draft/review path; no posting occurs here. If no credible source appears, finish `no_match` and report the bounded queries checked. Never invent a signal to fill a quota.
