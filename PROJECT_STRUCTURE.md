# Project structure and implementation order

## Design rule

The project remains one local Windows workspace. Shared contracts, storage, action controls, and adapter boundaries live in common code; each named agent is a separately identifiable workflow handler with its own tests. The folders below are the intended implementation layout, not a claim that those files already exist. `ARCHITECTURE.md` and `TRACEABILITY.md` are already present; this document describes where subsequent code and controller files will go.

```text
Lumen-Marketing-OS/
├── ARCHITECTURE.md
├── TRACEABILITY.md
├── DATA_MODEL.md
├── WORKFLOW_SPEC.md
├── INTEGRATION_CONTRACTS.md
├── PERMISSIONS_AND_APPROVALS.md
├── RUNTIME_AND_SCHEDULING.md
├── PROJECT_STRUCTURE.md
├── CONFIG_AND_SECRETS.md
├── TEST_AND_OPERATIONS_PLAN.md
├── AGENTS.md                     # Codex/agent operating instructions
├── STRATEGY.md                   # human-approved product facts and ICP
├── DAILY_OBJECTIVES.md          # human-approved current goals
├── CONTENT_PLAYBOOK.md          # approved claims/templates/evidence rules
├── DISCOVERED_USE_CASES.md       # reviewed research view
├── CAMPAIGN_TRACKER.md          # generated funnel view
├── OUTREACH_REGISTRY.md         # generated interaction view
├── AUDIT_LOGS.md                # generated operational summary
├── pyproject.toml
├── .env.example                  # names and safe non-secret examples only
├── .gitignore
├── config/
│   ├── base.json                # versioned, non-secret defaults
│   ├── development.json         # dry-run development settings
│   ├── staging.json             # realistic test settings; dispatch off
│   ├── live.json                # starts with dispatch disabled
│   └── schemas/                 # validated config and event schemas
├── src/lumen_marketing_os/
│   ├── cli/                     # status, review, pause, replay, backup commands
│   ├── core/                    # IDs, events, clock, errors, logging, validation
│   ├── db/                      # connection, repositories, backup
│   │   └── migrations/          # packaged, versioned SQL migrations
│   ├── engine/                  # scheduler, queue, leases, outbox, supervisor
│   ├── policy/                  # permission matrix, approvals, action gate,
│   │                           # suppression, quotas and spend
│   ├── adapters/                # Lumen, research, mail, social, Workspace,
│   │                           # calendar, model, product events, notifications
│   ├── workflows/
│   │   ├── research/            # roles 1A, 1B, 1C, 1E
│   │   ├── qualification/       # roles 2A, 2B, 2C
│   │   ├── content/             # roles 3A, 3B, 3C, 3D
│   │   ├── outreach/            # roles 4A, 4B, 4C
│   │   ├── operations/          # roles 5A, 5B
│   │   ├── community/           # roles 6A, 6B, 6C, 6D, 6E
│   │   ├── audit/               # roles 7A, 7B, 7C, 7D, 7E, 7F
│   │   ├── discovery/           # roles 8A, 8B, 8C, 8D
│   │   └── support/             # roles 9A, 9B, 9C, 9D
│   └── reports/                 # Markdown and Sheets projections
├── scripts/                    # Windows installation/start/backup helpers
├── tests/
│   ├── fixtures/               # invented, clearly labeled test data
│   ├── unit/                   # role logic and pure policy checks
│   ├── contract/               # adapters and event schemas
│   ├── integration/            # DB, queue, handoffs and projections
│   └── end_to_end/             # whole dry-run and recovery scenarios
└── runtime/                    # ignored generated data; created on first run
    ├── lumen_growth.db
    ├── logs/
    ├── backups/
    ├── exports/
    └── locks/
```

`PAUSE_ALL.lock` lives at the workspace root so the operator can find and create it quickly. The generated `runtime/` directory, secret files, and browser profiles are ignored by version control and backed up through the appropriate protected process. The eight controller/report Markdown files are separate from the eight design documents requested for groundwork.

## Code ownership and module boundaries

- `core` owns shared types, clocks, validation, logging, and errors; no provider-specific requests.
- `db` owns schema, transactions, repositories, and migrations; workflows do not issue arbitrary SQL.
- `engine` owns job lifecycle and event handoff; workflows do not self-schedule infinite loops.
- `policy` owns permission checks and the sole external action gate; adapters do not decide approvals.
- `adapters` own provider protocols, credentials, normalization, and error mapping; they have no campaign strategy.
- `workflows/<department>` own domain decisions for the named roles and emit typed results/events. Role IDs remain visible in registration and audit even when handlers share infrastructure.
- `reports` projects committed records into Markdown and Sheets. It cannot rewrite strategy or change a prospect's source-of-truth state from a display row.
- `cli` is the operator entry point for local status, approval, pause, retry and backup operations; a graphical UI can be added if it materially helps operation, using the same service contracts.

## Source control and environments

Initialize Git when implementation begins. Commit code, migrations, non-secret configuration, design docs, and sanitized fixtures. Ignore `.env`, credential JSON, tokens, browser sessions, local database and WAL files, backups, logs, and exported personal data. Keep development, staging, and live profiles separate. Staging can use real read-only connections with external actions disabled; test fixtures are labeled and never imported as production performance.

## Build order from this structure

1. Build package skeleton, validated configuration, logging, IDs, schema/migrations, and operator status/pause commands.
2. Build durable scheduler/queue/outbox and central suppression, approval, quota/spend, and action gate; prove restart and no-duplicate behavior with tests.
3. Build dry-run adapters and then verified live connector implementations one by one; isolate credentials and account permissions.
4. Implement research and qualification roles, then content/review, outreach/community/support, and monitoring/optimization. Connect each role's acceptance tests from `TRACEABILITY.md`.
5. Run integrated dry-run, outage, restart, backup/restore, and operator drills before considering live actions.

This sequence starts implementation after the groundwork files; it does not require another round of high-level documents before writing agent code.
