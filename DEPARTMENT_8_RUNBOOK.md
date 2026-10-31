# Department 8 — Use-Case Discovery & Prospect Escalation

Four project roles are declared in `.codex/config.toml`: 8A Autonomous Use-Case Scout, 8B Deep-Dive Vertical Researcher, 8C Vertical Prospect Hunter, and 8D Central Brain & Dispatch Arbiter. Migration `0014_discovery_escalation.sql` stores opportunities, source evidence, dossiers, reviewed organizations, cycle tasks, and audit events. These commands do not send or publish.

## Three daily cycles

The active Department 5 schedule `discovery-department8-8h` starts at 08:00 Africa/Nairobi on 2026-10-03 and repeats at 16:00 and 00:00. It allows one outstanding occurrence and one catch-up occurrence. `discovery.tick` creates bounded local tasks from reviewed Department 1 findings, Department 6 observations, and Department 8 state. Per-cycle ceilings: 50 source signals for 8A, 10 opportunities for 8B, 10 validated opportunities for 8C, and 10 accepted candidate groups for 8D. Actual yields may be zero. A running Department 5 supervisor is required; a local task does not invoke Codex or a browser session automatically.

```powershell
.\.venv\Scripts\lmos.exe operations report
.\.venv\Scripts\lmos.exe discovery report
.\.venv\Scripts\lmos.exe discovery cycle --key manual-2026-10-03
```

8A targets 3–8 distinct sourced pain candidates per cycle. `discovery scout` requires a specific vertical, buyer, problem, HTTPS source and excerpt. Brave Search and permitted research sessions can find leads when actually connected. A search hit alone is not validation.

8B targets 2–5 dossiers per cycle. `discovery evidence` records exact support and counter URLs and excerpts. `discovery validate` requires support from two independent domains plus counter evidence, buyer workflow, product fit, limitations, and a named reviewer. The deterministic score ranks review work; it is not market-size evidence or approval.

8C targets 3–10 organization candidates per cycle. `discovery candidate` requires company name, domain, country, source URL, and a prospect-specific pain URL and note. Domain/name/country conflicts fail for manual identity resolution. New organizations remain candidates; `discovery review-candidate` records acceptance or rejection.

8D targets 1–3 dispatch recommendations per cycle. A named approver runs `discovery dispatch OPPORTUNITY_ID --approver NAME` after reviewing the dossier and accepted candidates. This transaction creates an approved use case and Department 2 `needs_review` intakes. Repeated dispatch does not duplicate an intake. Department 2 must still review identity, admit the prospect, link its own pain evidence, and apply its fit rubric. Departments 3, 4, and 7 retain their later controls.

```powershell
.\.venv\Scripts\lmos.exe discovery scout --vertical "Revenue operations" --problem "Sales teams repeatedly correct stale business account records" --buyer "Revenue operations manager" --source-url "https://example.org/discussion" --excerpt "A sourced excerpt describing the actual problem in context."
.\.venv\Scripts\lmos.exe discovery evidence OPPORTUNITY_ID --polarity counter --source-url "https://example.net/report" --excerpt "A sourced excerpt explaining a credible limitation or contrary result."
.\.venv\Scripts\lmos.exe discovery validate OPPORTUNITY_ID --buyer-workflow "The buyer currently cleans incoming account lists every morning." --product-fit "Fresh company records could reduce manual reconciliation work." --limitations "The sources do not establish willingness to pay or purchase authority." --reviewer NAME
.\.venv\Scripts\lmos.exe discovery candidate OPPORTUNITY_ID --company "Example Ltd" --domain example.com --country KE --source-url "https://example.com/about" --pain-url "https://example.com/blog/problem" --pain-note "This organization explicitly describes its account data problem."
.\.venv\Scripts\lmos.exe discovery review-candidate CANDIDATE_ID --decision accept --reviewer NAME
.\.venv\Scripts\lmos.exe discovery dispatch OPPORTUNITY_ID --approver NAME
```

The global pause and `discovery` lane pause stop new mutations. Transactions keep validation and dispatch atomic. Cycle keys, signals, evidence URLs and opportunity/organization pairs are deduplicated. The task queue is a durable briefing ledger; after research, use `discovery finish-task TASK_ID --outcome completed|blocked --reason TEXT --actor NAME`. Browser connections, live Brave usage, agent yields, and supervisor restart continuity require separate verification. Codex heartbeat automation `department-8-research-and-escalation` is active for the research loop; its first unattended run remains to be verified. The Windows task runs only the local cycle.
