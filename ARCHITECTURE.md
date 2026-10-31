# Lumen Sieve Marketing AI OS — Architecture

## Purpose

This document describes the intended system and the relationships between its parts, based on the supplied System Configuration Specification, Practical Runtime & Environment Execution Blueprint, and Master Architecture Traceability Matrix. It is the working architecture for building the complete marketing OS. Items explicitly marked **verify** are implementation facts to confirm when we connect the relevant service; they do not block describing the system.

## Operating boundary

- The marketing OS runs on the user's local Windows machine. Its project files, persistent operational data, and scheduled local workers belong here.
- Codex is the development and coordination harness: it reads project instructions, coordinates bounded agent work, invokes available tools, and helps the operator review decisions. A Codex conversation is not assumed to remain alive as a production scheduler.
- Local Windows processes provide durable storage and scheduled/background execution. They must be restartable and recover work after interruption.
- Lumen Sieve is the product the OS is intended to market. Its API is an external service boundary to verify when integration work begins; the OS does not host Lumen Sieve.
- This architecture does not place any part of the marketing OS on Vultr.

## System at a glance

```text
Operator
  ├─ approves strategy, permissions, campaign settings, and external actions
  └─ reviews dashboards, drafts, alerts, and results
          │
          ▼
Codex harness ── reads project Markdown / coordinates agent work / invokes tools
          │
          ▼
Local Windows OS runtime
  ├─ scheduler and job coordinator
  ├─ nine departmental workflow groups (35 named responsibilities)
  ├─ policy, approval, pause, quota, and suppression gates
  ├─ integration adapters (web research, Lumen API, email, community, Workspace, etc.)
  └─ authoritative operational database, audit trail, and recovery facilities
          │
          ├─ external services: Lumen Sieve, authorized research/community sources,
          │  email, Google Workspace, calendar, model, and enrichment providers
          └─ human-readable views: Markdown reports and approved Workspace dashboards
```

The arrows represent proposed responsibility boundaries, not proof that an account, API, credential, or endpoint is already connected.

## Main components and their connections

### 1. Operator and policy surface

The operator owns product truth, target market, campaign intent, credentials, account authorization, and approval of consequential external actions. The system may research, qualify, draft, classify, and recommend within its configured scope. Sending messages, publishing public content, changing campaign objectives or limits, and replying to customers are gated by the approved permission policy. A kill/pause control can stop all external actions.

### 2. Codex harness

Codex reads the project guidance and coordinates work across the named responsibilities. During development, it can delegate independent implementation or review tasks while a coordinating agent maintains shared contracts and integrates results. At runtime, durable Windows services perform scheduled work; Codex is not treated as an always-on daemon or an implicit source of credentials. Any runtime invocation of a model or agent must use a defined, available integration and configured permissions.

### 3. Local runtime and workflow coordinator

The Windows runtime owns job scheduling, locks, retries, time-zone-aware queues, idempotency, state transitions, health checks, and restart/replay behavior. It hands work between workflow groups using durable records/events rather than relying on an in-memory conversation. Long-running Lumen lookups must accommodate the user-confirmed upper bound of three minutes; exact endpoint, authentication, payload, and response behavior remain to be verified against the real service.

### 4. Department workflow groups

The 35 roles below are functional responsibilities. They do not require 35 separate processes: compatible roles can share a maintained workflow service if their permissions, state, and failure boundaries remain explicit.

| Department | Named responsibilities |
| --- | --- |
| 1. Research & Use-Case Discovery | 1A Trend & Use-Case Explorer; 1B Platform Forum Scraper; 1C Competitive Intelligence Auditor; 1E Active Directory Sieve |
| 2. Lead Generation & Qualification | 2A Data Enrichment Orchestrator; 2B Fit-Score Analytics Gatekeeper; 2C Decision-Maker Locator |
| 3. Creative Content & Personalization | 3A Hyper-Personalization Engine; 3B Technical Writer & Case-Study Drafter; 3C A/B Framework Optimizer; 3D Content Critic & Proofreader |
| 4. Outreach & Inbox Orchestration | 4A SMTP/Outreach Dispatcher; 4B Inbound Intent Classifier; 4C Autonomous Responder & Auto-Scheduler |
| 5. System Operations & Performance Registry | 5A Central Cron Execution Manager; 5B Data Store & Token Ledger |
| 6. Community Growth & Viral Launch | 6A Reddit Inbound Hacker; 6B Hacker News Content Strategist; 6C Product Hunt & Launchpads Orchestrator; 6D LinkedIn Authority Builder; 6E B2B Prospect List Indexer |
| 7. System Auditor & Executive Controller | 7A Copywriting & Outreach Guard; 7B Rate-Limit & Domain Reputation Watchdog; 7C Database Integrity & Loop Inspector; 7D Cross-Channel Growth Optimization Auditor; 7E Pipeline Lag Sentinel; 7F Global Suppression & Compliance Vault |
| 8. Use-Case Discovery & Prospect Escalation | 8A Autonomous Use-Case Scout; 8B Deep-Dive Vertical Researcher; 8C Vertical Prospect Hunter; 8D Central Brain & Dispatch Arbiter |
| 9. Product Feedback, Triage & Support | 9A Multi-Channel Feedback Listener; 9B Bug & Triage Classifier; 9C Google Workspace Ticket Synchronizer; 9D Automated Customer Responder |

The source matrix contains no 1D role. This architecture preserves the named scope and does not invent one.

### 5. Policy and action-control layer

Every external action passes through centralized checks immediately before execution:

1. Global pause/stop state.
2. Recipient/channel suppression and opt-out state.
3. Required human approval bound to the exact recipient, channel, content version, and action.
4. Per-channel and provider quota/spend limits.
5. Current authorization, credentials, and adapter health.
6. Idempotency/replay check to prevent duplicate actions.

Edits to approved content invalidate that approval. Failures are recorded, retried only within configured bounds, then surfaced for operator review. Platform access uses authorized, supported interfaces; browser sessions do not imply permission for unattended extraction or posting.

### 6. Data and recordkeeping

The operational database is the proposed single source of truth for mutable system state. It stores stable IDs and source evidence, prospects and contacts, enrichment attempts/results, use cases, fit-score explanations, campaign variants and drafts, approvals, outbound/inbound communications, support tickets, suppression records, jobs/retries, quotas/spend, audit events, and payment-verified conversions.

SQLite is the intended local relational store from the documents, subject to validating concurrency and backup needs for the actual workload. Database migrations, transactional job/outbox handling, safe replay, backup/restore, retention, and deletion behavior are part of the architecture. Company name plus country alone is not assumed to be a reliable identity key.

Markdown files hold human-authored instructions/policy or generated readable reports; Google Sheets is a dashboard and collaboration surface. Neither should silently compete with the database as an authoritative writer for transactional state. Syncs must be record-level, idempotent, and recoverable.

### 7. Project Markdown files

| File | Role in the system |
| --- | --- |
| `STRATEGY.md` | Product truth, approved claims, positioning, and target segments; human-owned. |
| `AGENTS.md` | Responsibilities, tools, permission boundaries, escalation, and handoff instructions. |
| `DAILY_OBJECTIVES.md` | Current approved goals, segments, and operating parameters; policy changes require approval. |
| `CAMPAIGN_TRACKER.md` | Readable campaign/funnel report generated from operational records. |
| `OUTREACH_REGISTRY.md` | Readable interaction history generated from communication records. |
| `CONTENT_PLAYBOOK.md` | Approved templates, evidence standards, and writing guidance; human-approved. |
| `AUDIT_LOGS.md` | Readable operational, compliance, and performance summaries. |
| `DISCOVERED_USE_CASES.md` | Research backlog, sourced findings, and review decisions. |

The system configuration source names `DISCOVERED_USE_CASES.md`; if another source uses `USE_CASES.md`, choose one canonical name and document any compatibility mapping before implementation.

### 8. Integration adapters

Adapters isolate service-specific authentication, request/response formats, quotas, retries, and errors from the workflows. Intended groups are:

- Lumen Sieve enrichment API (contract and actual access to verify; up-to-three-minute lookup handling).
- Permitted web, RSS, forum, Reddit, Hacker News, Product Hunt, and launch-directory research sources.
- Email send and inbox/reply intake, including threading, bounce, and unsubscribe handling.
- Google Workspace Sheets/Drive sync for named lead, campaign analytics, and support-ticket surfaces.
- Contact enrichment, calendars, model providers, and support/contact webhooks.

Each adapter is independently testable and supports dry-run mode. Missing credentials or unsupported access must fail safely and produce a reviewable handoff rather than bypass controls.

## Core end-to-end workflow

1. **Discover:** research workflows collect a possible use case or prospect and store source evidence.
2. **Qualify:** enrichment obtains available business facts; the system records attempts, freshness, provenance, ambiguity, and errors. The fit workflow explains its score rather than silently treating uncertain data as fact.
3. **Prepare:** decision-maker research identifies an appropriate contact; content workflows produce evidence-bound drafts and variants.
4. **Review:** policy checks the draft; the operator approves, rejects, or requests revision. Approval is tied to the precise target and content version.
5. **Queue and dispatch:** the scheduler respects the approved window and target time zone. Immediately before sending/posting, the action-control layer rechecks pause, suppression, approval, quotas, and idempotency.
6. **Listen and support:** inbox/community/support adapters capture inbound events once; classification routes intent, opt-outs, product feedback, and support issues to the appropriate queue. Customer replies follow the configured approval policy.
7. **Measure and improve:** the system attributes observed responses and verified paid conversions to campaign/channel, reports results, and proposes changes. Policy, quota, or objective changes require approval.
8. **Recover:** failures remain visible as durable jobs with bounded retry, dead-letter/review state, and safe operator replay; monitoring detects lag, hangs, and integrity problems.

## Data flow and ownership rules

```text
Sources / integrations
        │ source evidence + normalized events
        ▼
Local workflow coordinator ──► authoritative operational database
        │                              │
        ├─► review/approval queues     ├─► audit trail and metrics
        │                              ├─► Markdown reports
        ▼                              └─► idempotent Workspace dashboard sync
Central action gates
        │ approved, allowed, in-quota action only
        ▼
External send/post/API action ──► recorded result/reply/conversion event
```

The database is authoritative for mutable operational records; human-authored Markdown remains authoritative only for its defined policy/instruction content. Generated Markdown and Sheets views are projections. A failed projection sync must not lose or rewrite the underlying record.

## Runtime and failure model

- Run scheduled/background workflows as managed local Windows processes with startup/restart behavior and visible health state.
- Persist jobs before work begins; use unique job/action keys, database transactions, and locks to prevent duplicate or overlapping processing.
- Use bounded backoff for transient failures; distinguish retryable errors, needs-review cases, and terminal failures.
- A process restart resumes from durable state. It must not resend an action merely because its prior result was not immediately observed; ambiguous dispatch outcomes require reconciliation.
- Provide operator-visible status, current queue, last success/error, retry state, pause reason, quota/spend, and recovery/replay controls.
- Backups and restore drills cover both operational data and the configuration needed to interpret it. Secrets and browser profiles are protected and excluded from source control and logs.

## Build and activation boundary

Build and verify shared contracts, storage, workflow reliability, action controls, adapters, and departmental workflows locally. Exercise integrations and complete end-to-end flows in dry-run mode first. Live external actions remain disabled until authorization, credentials, limits, approval rules, recovery behavior, and the operator's explicit go-ahead are in place. The five-paying-user/one-month goal is a measurement target, not a guaranteed result.

## Traceability expectation

Every named role and workflow must ultimately have a traceable owner, trigger, inputs, outputs, data records, required tools, permission level, failure/retry path, and acceptance test. This architecture establishes the system boundaries and flow; detailed role contracts and test cases are elaborated as the implementation proceeds.
