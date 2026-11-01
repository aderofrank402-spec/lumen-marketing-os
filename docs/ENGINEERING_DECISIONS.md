# Engineering decisions

These decisions capture the reasoning behind the public implementation, including the trade-offs an interviewer or maintainer should know.

## ADR-001 · Use SQLite as the first operational store

**Decision:** keep workflow state, policy records, jobs, outbox entries, and audit data in one versioned SQLite database.

**Why:** the system is local-first. SQLite provides transactions, foreign keys, backups, integrity checks, and a small operational footprint without hiding state across services.

**Trade-off:** a single local writer is not the final answer for high-volume distributed execution. Moving to a server database would require preserving the same constraints, idempotency keys, migration discipline, and recovery semantics.

## ADR-002 · Persist handoffs instead of chaining agents in memory

**Decision:** consequential work crosses durable tables and state transitions.

**Why:** in-memory chains lose context on process failure and make retries difficult to reason about. Durable handoffs can be inspected, replayed selectively, paused, and audited.

**Trade-off:** this adds schema and state-machine complexity, but that complexity represents real operational requirements rather than framework overhead.

## ADR-003 · Bind approval to the exact action

**Decision:** approval matches content hash, target, channel, reviewer, expiration, and policy context.

**Why:** approving an early draft must not authorize modified copy or a different recipient. Exact binding makes the permission narrow and testable.

**Trade-off:** legitimate edits trigger re-review. The additional review cost is intentional for externally visible actions.

## ADR-004 · Assume at-least-once execution

**Decision:** queues and outboxes may retry; unique keys and claimed action identities protect external effects.

**Why:** crashes can happen after an effect but before local acknowledgement. Pretending delivery is exactly once would hide that uncertainty.

**Trade-off:** adapters and workflow designers must define stable idempotency keys and route unknown outcomes to review.

## ADR-005 · Fail closed when safety evidence is unavailable

**Decision:** missing suppression data, approval, quota, provider health, or policy evidence blocks action.

**Why:** marketing automation has asymmetric failure costs. A delayed action is recoverable; an unauthorized or duplicated message may not be.

**Trade-off:** availability is lower when a control dependency fails. The system favors correctness and operator visibility over automatic continuation.

## ADR-006 · Separate logical roles from processes

**Decision:** define 35 roles as responsibility and review boundaries without requiring 35 resident workers.

**Why:** clear ownership improves prompts, evidence contracts, and handoffs; process topology can remain appropriate to the local workload.

**Trade-off:** “agent count” is not a throughput claim. Deployment may consolidate or split workers without changing the role model.
