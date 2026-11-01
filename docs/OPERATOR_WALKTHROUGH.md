# Operator walkthrough

This walkthrough uses the default development profile. It creates local state only and does not perform live outreach.

## 1. Install and initialize

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
Copy-Item .env.example .env
.\.venv\Scripts\lmos.exe init
```

Initialization applies the ordered migrations and creates the local SQLite runtime under `runtime/`.

## 2. Inspect health and safety state

```powershell
.\.venv\Scripts\lmos.exe status
.\.venv\Scripts\lmos.exe check
```

`status` reports the active environment, dry-run state, database path, migration state, and pause controls. `check` runs SQLite integrity and foreign-key validation.

## 3. Create durable internal work

```powershell
.\.venv\Scripts\lmos.exe enqueue portfolio-demo `
  --lane research `
  --key portfolio-demo-001 `
  --payload '{"purpose":"demonstrate durable work"}' `
  --effect internal

.\.venv\Scripts\lmos.exe jobs --state pending
```

The unique key makes repeated enqueue attempts idempotent. The job remains in durable storage until a worker leases it.

## 4. Demonstrate an operational pause

```powershell
.\.venv\Scripts\lmos.exe pause `
  --lane research `
  --actor operator@example.com `
  --reason "portfolio safety demonstration"

.\.venv\Scripts\lmos.exe status --lane research
```

The lane pause blocks new work in that lane without deleting queued records. Resume requires an equally explicit operator action:

```powershell
.\.venv\Scripts\lmos.exe resume `
  --lane research `
  --actor operator@example.com `
  --reason "demonstration complete"
```

## 5. Verify the implementation

```powershell
python scripts/validate_public_repo.py
python -m pytest -q
```

The first command checks the public artifact itself; the second exercises policy controls, persistence, migrations, runtime behavior, and department workflows.

## What to inspect next

- [`src/lumen_marketing_os/engine/queue.py`](../src/lumen_marketing_os/engine/queue.py) for lease and retry behavior
- [`src/lumen_marketing_os/engine/outbox.py`](../src/lumen_marketing_os/engine/outbox.py) for durable effect delivery
- [`src/lumen_marketing_os/policy/`](../src/lumen_marketing_os/policy/) for approval, quota, pause, and suppression gates
- [`src/lumen_marketing_os/workflows/`](../src/lumen_marketing_os/workflows/) for department runtimes
- [`tests/`](../tests/) for executable evidence
