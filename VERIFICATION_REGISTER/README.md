# Verification register

This folder tracks checks that cannot yet be completed against real operating conditions. It is not a list of missing implementation work or permission to silently skip a check. Keep one department file per department, give each item a stable ID, and record the evidence and date when closing it. Do not put API keys, personal data, or raw provider responses in this folder.

| Department | Register | Current state |
| --- | --- | --- |
| 1 — Research & Use-Case Discovery | [DEPARTMENT_1.md](DEPARTMENT_1.md) | Authenticated Reddit handoff and Brave-only search implemented and live-tested; other Department 1 gaps remain separate. |
| 2 — Lead Generation & Qualification | [DEPARTMENT_2.md](DEPARTMENT_2.md) | Implemented and locally tested; listed live/operational checks remain open. |
| 3 — Creative Content & Personalization | [DEPARTMENT_3.md](DEPARTMENT_3.md) | Implemented locally; real claims, role operation, campaign results and downstream gates remain open. |
| 4 — Outreach & Inbox Orchestration | [DEPARTMENT_4.md](DEPARTMENT_4.md) | Local workflows tested; live Google authorization, Department 7A guard, and operational canaries remain open. |
| 5 — System Operations & Performance | [DEPARTMENT_5.md](DEPARTMENT_5.md) | Local scheduler, ledger and recovery tests pass; live Windows lifecycle, provider billing and disaster-restore checks remain open. |
| 6 — Community Growth & Viral Launch | [DEPARTMENT_6.md](DEPARTMENT_6.md) | Local roles, task handoffs, drafts, launch checklist and prospect importer built; platform connections and Codex automations remain open. |
| 7 — System Auditor & Executive Controller | [DEPARTMENT_7.md](DEPARTMENT_7.md) | Six roles implemented and locally tested; real provider reputation signals and real-campaign outcome validation remain operational checks. |

Status vocabulary: **open** = dependency not yet available; **ready** = dependency available and check can be run; **failed** = check ran and needs a fix; **closed** = acceptance criteria met with dated evidence. An item is never closed merely because a unit test passes when its criterion requires a real integration or campaign result.

When a dependency becomes available, update the item to ready, run the specified check, record the result and evidence location, and only then close it. If a check fails, keep it visible, link the defect, fix it, and rerun the same acceptance check. Department 3 and later should add their own files here as they are implemented.
