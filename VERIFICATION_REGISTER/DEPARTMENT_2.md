# Department 2 — deferred verification

Department 2's local implementation is complete, but the Lumen Sieve product is not currently live. The qualification code is tested with injected provider responses; those tests do not establish live API compatibility. Baseline on 2026-09-30: `python -m pytest -q` passed 44 tests, the package wheel built, and `lmos check` reported SQLite integrity OK after migration 0006. See [DEPARTMENT_2_RUNBOOK.md](../DEPARTMENT_2_RUNBOOK.md) for the operating procedure. No API credentials are needed to continue building later departments.

Do not paste credentials or unredacted prospect data into this register. Live checks must use authorized accounts and a bounded test budget. Items below are **open** until their stated trigger and acceptance criteria are met; Hunter is optional and is not a prerequisite to call Department 2 implemented.

## D2-01 — Lumen Sieve live lookup contract

- **Status:** open.
- **Trigger:** Lumen Sieve is hosted, its API is reachable, and an authorized test key is available in the local ignored `.env`.
- **Check:** With a deliberately small daily lookup limit, submit a known business domain through Department 2; observe the initial `200` or `202`, same-origin poll path, final profile shape, authentication, and idempotency behavior. Confirm the stored attempt, provider-call reservation, snapshot, and fit assessment correspond to the same request. Compare behavior with the actual deployed API contract, not only the local product source or fixtures.
- **Close when:** At least one real lookup finishes through the local OS, the recorded data and status are correct, a repeat/retry does not create an unintended duplicate paid lookup, and a dated redacted test record is linked here.
- **Evidence/result:** pending.

## D2-02 — Real profile quality and identity gate

- **Status:** open.
- **Trigger:** D2-01 is possible and a small set of consented or publicly listed test organizations with known domains/countries is selected.
- **Check:** Inspect the live profile's name, website/domain, country, confidence, field sources, dates, and generic email fields against independent public sources. Include at least one domain/name mismatch or incomplete profile. Verify that ambiguity remains in review and that missing/low-confidence evidence cannot produce a qualified decision.
- **Close when:** The normalized snapshot accurately represents the source, provenance is retained, and incorrect/ambiguous profiles are blocked in documented examples; any mapping defects are fixed and retested.
- **Evidence/result:** pending.

## D2-03 — Provider failures, limits, and recovery in operation

- **Status:** open.
- **Trigger:** A live test environment and a safe way to exercise or observe provider errors without wasteful calls are available.
- **Check:** Confirm real `401/403`, `404`, `429` with `Retry-After`, transient failure, deadline expiry, process restart during polling, daily budget exhaustion, and explicit retry after reconciliation are classified and persisted as designed. Where a response cannot safely be induced live, use a controlled proxy or test endpoint and label it as such; do not claim it was observed from production.
- **Close when:** Each case has dated test evidence, no silent duplicate charge/action occurs, jobs recover or require review as specified, and `lmos check` still passes.
- **Evidence/result:** pending.

## D2-04 — Research-backed qualification rubric

- **Status:** open.
- **Trigger:** Department 1 has yielded reviewed evidence for a target segment and a concrete buyer problem; this is a research/operator decision, not a request to invent an ICP or select one solely from the existing codebase.
- **Check:** Approve a versioned rubric and specific sourced use case, admit representative prospects with organization-specific pain evidence, then review score reasons and qualified/review decisions. Include poor-fit and ambiguous examples.
- **Close when:** The approved version, reviewer, sample decisions, and source links are recorded, and the evidence supports the segment/problem choice. Outcome calibration is tracked separately in D2-07.
- **Evidence/result:** pending. Current `config/qualification.json` deliberately remains a draft with no approved segments.

## D2-05 — Contact evidence and optional provider compatibility

- **Status:** open.
- **Trigger:** A qualified real prospect and a permitted public source for a named business contact are available. Hunter testing is triggered only if we choose and enable that optional paid provider.
- **Check:** Verify a named contact's current role, business channel, source URL, and provenance manually; confirm generic addresses and unverified candidates cannot pass the contact-verification gate. If Hunter is enabled, separately test its real domain-search response, credit limit, source freshness, error handling, and candidate-only status.
- **Close when:** At least one real named contact is accurately recorded and reviewed without relying on a guessed address, with a dated redacted source record. The Hunter sub-check is marked **not used** or closed with separate evidence; its absence does not block the manual path.
- **Evidence/result:** pending.

## D2-06 — Department 3 handoff

- **Status:** open.
- **Trigger:** Department 3's content intake/review workflow is implemented.
- **Check:** Follow a qualified prospect and verified contact through the durable `contact.verified`/`content.review` handoff. Confirm one logical downstream intake after retries/restarts, correct evidence references, and no draft or message is sent merely because a contact was verified.
- **Close when:** An end-to-end local integration test and a dated operational check show a complete, duplicate-safe handoff with no external send.
- **Evidence/result:** Local integration test now follows Department 2's verified contact into one Department 3 intake and checks replay does not duplicate it (`tests/test_qualification.py` and `tests/test_content_runtime.py`, 2026-09-30). Operational review with a real prospect remains pending, so this item stays open.

## D2-07 — Fit calibration against campaign outcomes

- **Status:** open.
- **Trigger:** Later departments have run an authorized campaign long enough to produce meaningful replies, disqualifications, or conversions.
- **Check:** Compare Department 2 qualified/review decisions with those outcomes, accounting for small sample size and message/channel effects. Identify false positives, false negatives, and evidence gaps; recommend a new rubric version if the pattern warrants it.
- **Close when:** A dated analysis states sample size and limitations, links decision/outcome records without exposing personal data, and documents either a justified rubric update or a justified decision to keep it unchanged.
- **Evidence/result:** pending.
