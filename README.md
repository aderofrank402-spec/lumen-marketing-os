# Lumen Marketing OS

A local-first, evidence-bound operating system for coordinating AI-assisted research, qualification, content, outreach, community, compliance, growth, and support workflows.

Python · SQLite · durable queues · policy gates · audit trails · human approval

## Why it exists

Marketing automation becomes dangerous when drafts silently become actions, evidence is lost, or retries send duplicates. Lumen Marketing OS treats every workflow as a durable, inspectable state machine: research and drafts can move quickly, while external actions remain gated by evidence, policy, quotas, suppression rules, and explicit approval.

## Nine departments

1. Market and community research
2. Enrichment, qualification, and decision-maker review
3. Evidence-backed content and experiments
4. Guarded outreach, inbound intent, and booking proposals
5. Scheduling, usage accounting, backups, and recovery
6. Community research and launch preparation
7. Integrity, reputation, growth, and compliance controls
8. Opportunity discovery, validation, and escalation
9. Feedback intake, ticket triage, and reviewed support replies

## Safety model

- External effects are disabled in the default development profile.
- Drafting, approval, and dispatch are separate states.
- Exact content hashes prevent approved copy from changing before action.
- Durable leases, idempotency keys, and an outbox protect retries.
- Global and per-lane pause controls stop new work safely.
- Quotas, suppression checks, and provider-health gates fail closed.
- Evidence, counter-evidence, reviewer decisions, and state transitions remain auditable.

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
Copy-Item .env.example .env
.\.venv\Scripts\lmos.exe init
.\.venv\Scripts\lmos.exe check
.\.venv\Scripts\python.exe -m pytest -q
```

The local SQLite runtime is generated under `runtime/` and is not committed.

## Documentation

- [`ARCHITECTURE.md`](ARCHITECTURE.md)
- [`PROJECT_STRUCTURE.md`](PROJECT_STRUCTURE.md)
- [`PERMISSIONS_AND_APPROVALS.md`](PERMISSIONS_AND_APPROVALS.md)
- [`CONFIG_AND_SECRETS.md`](CONFIG_AND_SECRETS.md)
- [`TEST_AND_OPERATIONS_PLAN.md`](TEST_AND_OPERATIONS_PLAN.md)
- Department runbooks and verification registers in the repository root

## Scope

This repository contains the orchestration and control system, not the private LUMEN Sieve product source. Live provider connections require the operator's own accounts, explicit configuration, and approval.

## License

MIT. See [`LICENSE`](LICENSE).
