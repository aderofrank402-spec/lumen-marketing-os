CREATE TABLE system_controls (
    scope TEXT PRIMARY KEY,
    paused INTEGER NOT NULL CHECK(paused IN (0,1)),
    reason TEXT,
    actor TEXT NOT NULL,
    changed_at_utc TEXT NOT NULL
);
CREATE TABLE audit_events (
    id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    event_type TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    correlation_id TEXT,
    details_json TEXT NOT NULL,
    occurred_at_utc TEXT NOT NULL
);
CREATE INDEX ix_audit_entity ON audit_events(entity_type, entity_id, occurred_at_utc);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    workflow TEXT NOT NULL,
    lane TEXT NOT NULL,
    effect TEXT NOT NULL CHECK(effect IN ('internal','external')),
    payload_json TEXT NOT NULL,
    logical_key TEXT NOT NULL UNIQUE,
    priority INTEGER NOT NULL DEFAULT 0,
    due_at_utc TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','leased','retry_wait','needs_review','dead_letter','succeeded')),
    lease_owner TEXT,
    lease_expires_at_utc TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
    last_error_class TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    CHECK((state = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at_utc IS NOT NULL)
       OR (state != 'leased' AND lease_owner IS NULL AND lease_expires_at_utc IS NULL))
);
CREATE INDEX ix_jobs_claim ON jobs(state, due_at_utc, priority DESC, created_at_utc);
CREATE INDEX ix_jobs_lease ON jobs(state, lease_expires_at_utc);
CREATE TABLE job_attempts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
    worker_id TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    result TEXT NOT NULL CHECK(result IN ('running','succeeded','retry','needs_review','dead_letter','lease_expired')),
    error_class TEXT,
    redacted_diagnostic TEXT,
    UNIQUE(job_id, attempt_number)
);

CREATE TABLE outbox_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK(schema_version > 0),
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    deduplication_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN ('pending','leased','retry_wait','needs_review','delivered')),
    due_at_utc TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at_utc TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    last_error_class TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    CHECK((state = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at_utc IS NOT NULL)
       OR (state != 'leased' AND lease_owner IS NULL AND lease_expires_at_utc IS NULL))
);
CREATE INDEX ix_outbox_due ON outbox_events(state, due_at_utc);

CREATE TABLE sync_cursors (
    adapter TEXT NOT NULL,
    stream TEXT NOT NULL,
    cursor_value TEXT,
    checked_at_utc TEXT NOT NULL,
    PRIMARY KEY(adapter, stream)
);
CREATE TABLE schedule_definitions (
    id TEXT PRIMARY KEY,
    workflow TEXT NOT NULL,
    lane TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL CHECK(interval_seconds > 0),
    timezone TEXT NOT NULL DEFAULT 'UTC',
    catch_up_policy TEXT NOT NULL CHECK(catch_up_policy IN ('skip','one','all')),
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    last_due_at_utc TEXT,
    next_due_at_utc TEXT NOT NULL,
    config_version TEXT NOT NULL
);
