# Department 1 — Manual Reddit Research Handoff

**Superseded for routine research:** Department 6A now uses the authenticated Reddit session and the durable Department 1 intake in `DEPARTMENT_1_REDDIT_HANDOFF.md`. This document remains a fallback checklist if the operator deliberately supplies a discussion; it is no longer the required routine handoff.

## Purpose

Your role is to find **real, specific business-information problems** in Reddit discussions and hand the evidence to Department 1 for analysis. This is a human research task, not an automated Reddit collection or outreach task. A discussion is a **pain signal**, not automatically a prospect or proof that Lumen Sieve solves the problem.

The first-month objective is to learn which problems and buyer groups lead to conversations and paying users. Quality matters more than the number of links collected. Reddit is one source; if it yields little, use the same handoff for relevant discussions found elsewhere and label the source accurately.

## What you do

1. Browse Reddit yourself using your normal account or public pages. Search relevant communities and terms such as *incorrect company data*, *outdated contact details*, *business enrichment*, *verifying suppliers*, or *researching a company*. Follow each community's rules. Do not use scripts, rotating identities, proxies, or repeated requests after access is limited.
2. Open promising discussions and read enough context to understand the actual workflow. Prefer a first-hand problem, a recent example, a stated consequence, and a plausible business user over a vague complaint or a post merely mentioning a keyword.
3. Record the original permalink and date, then summarize the problem **in your own words**. Capture only a short excerpt when necessary to preserve evidence; do not copy entire posts or collect unnecessary personal information.
4. Note the current workaround, urgency, and any sign of willingness to pay. Record counter-evidence too: for example, the issue was already solved, the author needs a different product, or the thread is too old to act on.
5. If the author names a business, distinguish a **self-described affiliation** from a **verified organization**. Do not treat a Reddit username as a company, infer an email address, or call someone a lead without evidence of a real business identity and fit.
6. Send your completed entries to Codex in this workspace for Department 1 review. Do not reply, post, message, or pitch on Reddit as part of this task. Any later engagement is a separate decision subject to community rules and the OS approval process.

Start with a short, focused research session; submit a few well-evidenced signals rather than forcing a quota. If no credible signal appears, report **none found** and the queries/communities you checked—do not manufacture a prospect.

## Expected output: one entry per discussion

Copy this template for each distinct discussion. Use `unknown` where evidence is missing; keep observations separate from your interpretation.

```text
Researcher:
Reviewed at (UTC):
Source / community:
Original discussion URL:
Post date (as displayed; include timezone if known):
Title:
Problem stated (1–2 sentences, in your own words):
Evidence excerpt (optional, short):
Business workflow affected:
Current workaround or consequence:
Possible buyer or organization (only if explicitly evidenced):
Verified organization domain and evidence URL (otherwise "unknown"):
Why this may relate to Lumen Sieve:
Why it may NOT relate / competing explanation:
Urgency or buying signal (quote/paraphrase the evidence, or "none"):
Suggested next step: investigate use case / verify organization / no action
Confidence: high / medium / low, with one reason
Contact or public reply made: no
```

For a session, add a brief cover note with the date, communities/queries checked, number of discussions reviewed, number submitted, and any recurring pattern. Do not include passwords, session cookies, private messages, or scraped contact lists.

## What Department 1 does with it

Codex/Agent 1B checks the link, relevance, freshness, and interpretation; groups duplicate discussions; challenges weak inferences; and classifies the result as a research signal, a possible use-case hypothesis, or not relevant. A named company is handed toward identity review only after its identity and domain are independently supported. No signal becomes an outreach target automatically, and no public reply or direct message is authorized by this handoff.

**Current handoff:** A Reddit discussion is saved first as a leased 6A `community observe` item, then imported with `lmos research import-reddit-observation` as described in `DEPARTMENT_1_REDDIT_HANDOFF.md`. Pasting a report into chat alone does not save it; the CLI must return a finding ID.
