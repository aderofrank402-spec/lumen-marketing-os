ALTER TABLE schedule_definitions ADD COLUMN owner_role TEXT NOT NULL DEFAULT 'operations';
ALTER TABLE schedule_definitions ADD COLUMN max_overlap INTEGER NOT NULL DEFAULT 1 CHECK(max_overlap > 0);
ALTER TABLE schedule_definitions ADD COLUMN window_start_local TEXT;
ALTER TABLE schedule_definitions ADD COLUMN window_end_local TEXT;
ALTER TABLE schedule_definitions ADD COLUMN weekdays_json TEXT NOT NULL DEFAULT '[0,1,2,3,4,5,6]';
ALTER TABLE schedule_definitions ADD COLUMN updated_at_utc TEXT;

CREATE TABLE schedule_occurrences (
    schedule_id TEXT NOT NULL REFERENCES schedule_definitions(id),
    due_at_utc TEXT NOT NULL,
    config_version TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('enqueued','catch_up_skipped','outside_window','overlap_skipped')),
    job_id TEXT REFERENCES jobs(id),
    recorded_at_utc TEXT NOT NULL,
    PRIMARY KEY(schedule_id, due_at_utc)
);
CREATE INDEX ix_schedule_occurrences_job ON schedule_occurrences(job_id);

CREATE TABLE operations_heartbeats (
    process_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    heartbeat_at_utc TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE provider_prices (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    currency TEXT NOT NULL,
    prompt_per_million_minor INTEGER NOT NULL CHECK(prompt_per_million_minor >= 0),
    completion_per_million_minor INTEGER NOT NULL CHECK(completion_per_million_minor >= 0),
    request_minor INTEGER NOT NULL CHECK(request_minor >= 0),
    effective_at_utc TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at_utc TEXT NOT NULL,
    UNIQUE(provider, model, currency, effective_at_utc)
);
CREATE INDEX ix_provider_prices_lookup ON provider_prices(provider,model,effective_at_utc);

CREATE TABLE usage_ledger (
    id TEXT PRIMARY KEY,
    usage_key TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    job_id TEXT REFERENCES jobs(id),
    prompt_tokens INTEGER CHECK(prompt_tokens >= 0),
    completion_tokens INTEGER CHECK(completion_tokens >= 0),
    requests INTEGER CHECK(requests >= 0),
    price_id TEXT REFERENCES provider_prices(id),
    estimated_minor INTEGER CHECK(estimated_minor >= 0),
    actual_minor INTEGER CHECK(actual_minor >= 0),
    currency TEXT,
    status TEXT NOT NULL CHECK(status IN ('unknown','estimated','reconciled')),
    occurred_at_utc TEXT NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    reconciled_at_utc TEXT,
    reconciled_by TEXT,
    CHECK(status != 'reconciled' OR (actual_minor IS NOT NULL AND reconciled_by IS NOT NULL))
);
CREATE INDEX ix_usage_rollup ON usage_ledger(provider,model,occurred_at_utc);

CREATE TABLE backup_records (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
    migration_count INTEGER NOT NULL CHECK(migration_count > 0),
    created_at_utc TEXT NOT NULL,
    verified_at_utc TEXT NOT NULL
);
CREATE TABLE restore_drills (
    id TEXT PRIMARY KEY,
    backup_id TEXT NOT NULL REFERENCES backup_records(id),
    result TEXT NOT NULL CHECK(result IN ('passed','failed')),
    diagnostic TEXT,
    performed_at_utc TEXT NOT NULL
);
