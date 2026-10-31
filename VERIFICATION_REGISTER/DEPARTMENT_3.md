# Department 3 — deferred verification

The local implementation has 3A/3B evidence-bound drafting, immutable versions, 3D critique with a human-audited path for free-form Codex wording, and 3C recommendation-only experiments. Fixture tests do not establish real message quality, product-claim truth, or campaign lift. Keep all real credentials and personal data out of this file. See [DEPARTMENT_3_RUNBOOK.md](../DEPARTMENT_3_RUNBOOK.md).

## D3-01 — Approve current product truth and usable templates

- **Status:** open.
- **Trigger:** The current Lumen Sieve product behavior and market positioning have been checked against its code, product documentation, and later hosted behavior.
- **Check:** Register scoped product claims with evidence references; review their accuracy and limitations; approve versions and channel-specific templates. Do not convert a roadmap feature or planning PDF into a current-capability claim. Any actual customer-result claim also needs evidence and confirmed publication permission.
- **Close when:** The claim/template IDs, versions, approver, sources and date are recorded, and a second review confirms the rendered copy says no more than the evidence supports.
- **Evidence/result:** pending. No production claims/templates have been approved in the local database.

## D3-02 — Operate the four Codex roles on a real bounded brief

- **Status:** open.
- **Trigger:** D3-01 is satisfied and a qualified, sourced prospect/contact or an approved technical brief is available.
- **Check:** Give 3A or 3B the `content brief` packet, save the exact proposal as a revision, conduct a human claim audit, run 3D on that same hash, and inspect 3C's recommendation-only output on a controlled dataset. Confirm prompt-like source text cannot redirect the roles or local gates.
- **Close when:** A dated role run has stored draft/version, evidence, audit, critique and no-send checks; any unsafe/unsupported wording was blocked or revised.
- **Evidence/result:** pending. Role declarations and fixture tests are not a live Codex-role exercise.

## D3-03 — Real prospect-message quality and response

- **Status:** open.
- **Trigger:** Approved drafts have been sent by a later authorized outreach workflow and replies or qualified conversations are recorded.
- **Check:** Review whether personalization was accurate and relevant, compare variants with a baseline, and examine opt-outs, complaints and disqualifications as well as positive replies. Separate content effects from targeting, channel and deliverability effects.
- **Close when:** A dated, sample-size-aware analysis is linked, including negative outcomes and a documented copy/targeting decision. No reply-rate or sales uplift is assumed now.
- **Evidence/result:** pending.

## D3-04 — Real A/B observation and recommendation audit

- **Status:** open.
- **Trigger:** A later department can record authorized, timestamped variant exposures and outcomes within a declared campaign window.
- **Check:** Reconcile exposure/outcome IDs with source systems, inspect sample balance and missing/delayed outcomes, and compare 3C's conservative recommendation or insufficient-evidence result with independent calculations. Confirm weights/spend did not change automatically.
- **Close when:** A dated real-campaign analysis with counts, window, metric, limitations and operator decision is recorded; sparse data explicitly remains insufficient.
- **Evidence/result:** pending. Synthetic-statistical tests are complete, but no real campaign has run.

## D3-05 — Downstream policy and exact-action approval

- **Status:** open.
- **Trigger:** Department 7A/operator approval and Department 4 action gate are implemented.
- **Check:** Follow a `draft.review_passed` event to policy review. Verify the exact draft hash, recipient, channel, action, evidence freshness and approval expiry; edit/revise the draft and confirm the old approval cannot authorize an action. A 3D pass alone must never send.
- **Close when:** An end-to-end integration test and an operational check prove review-to-approval binding, invalidation and no implicit send.
- **Evidence/result:** pending. Department 3 only emits an internal handoff.

## D3-06 — Critic quality on a representative draft corpus

- **Status:** open.
- **Trigger:** Real approved claims/templates and a corpus of human-written and Codex-proposed drafts exist.
- **Check:** Seed the corpus with unsupported statistics, invented customer outcomes, stale facts, prompt-injection text, unreadable copy, and clean examples; measure which are blocked and which need human escalation. Tighten the rules and role instructions when misses are found. Deterministic checks and a human claim audit are controls, not a claim of perfect semantic detection.
- **Close when:** The dated corpus, expected labels, results, residual limitations, and any remediation/retest are recorded.
- **Evidence/result:** pending.
