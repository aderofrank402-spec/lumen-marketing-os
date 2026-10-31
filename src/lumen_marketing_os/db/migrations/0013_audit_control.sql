CREATE TABLE audit_policy_versions (
    id TEXT PRIMARY KEY,
    policy_key TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    rules_json TEXT NOT NULL,
    rules_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('draft','active','retired')),
    created_by TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    activated_by TEXT,
    activated_at_utc TEXT,
    UNIQUE(policy_key, version)
);
CREATE UNIQUE INDEX ux_audit_policy_active ON audit_policy_versions(policy_key)
    WHERE state='active';

CREATE TABLE audit_guard_reviews (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(id),
    content_hash TEXT NOT NULL,
    policy_id TEXT NOT NULL REFERENCES audit_policy_versions(id),
    decision TEXT NOT NULL CHECK(decision IN ('passed','blocked','needs_review')),
    findings_json TEXT NOT NULL,
    checker_state TEXT NOT NULL CHECK(checker_state IN ('available','unavailable')),
    reviewed_by TEXT NOT NULL,
    reviewed_at_utc TEXT NOT NULL,
    UNIQUE(draft_id, content_hash, policy_id)
);
CREATE INDEX ix_guard_review_exact ON audit_guard_reviews(draft_id,content_hash,decision);

CREATE TABLE provider_health_snapshots (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    account_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('healthy','degraded','unhealthy','unknown')),
    metrics_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    expires_at_utc TEXT,
    recorded_by TEXT NOT NULL
);
CREATE INDEX ix_provider_health_latest ON provider_health_snapshots(channel,account_id,observed_at_utc DESC);

CREATE TABLE audit_runs (
    id TEXT PRIMARY KEY,
    role_id TEXT NOT NULL CHECK(role_id IN ('7A','7B','7C','7D','7E','7F')),
    run_kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','passed','warning','blocked','failed')),
    input_json TEXT NOT NULL,
    result_json TEXT,
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    initiated_by TEXT NOT NULL
);
CREATE INDEX ix_audit_runs_role ON audit_runs(role_id,started_at_utc DESC);

CREATE TABLE audit_findings (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES audit_runs(id),
    role_id TEXT NOT NULL CHECK(role_id IN ('7A','7B','7C','7D','7E','7F')),
    finding_key TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('info','warning','blocking','critical')),
    entity_type TEXT,
    entity_id TEXT,
    lane TEXT,
    summary TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('open','acknowledged','resolved')),
    created_at_utc TEXT NOT NULL,
    resolved_at_utc TEXT,
    resolved_by TEXT,
    UNIQUE(run_id, finding_key)
);
CREATE INDEX ix_audit_findings_open ON audit_findings(state,severity,created_at_utc);

CREATE TABLE growth_audit_reports (
    id TEXT PRIMARY KEY,
    window_start_utc TEXT NOT NULL,
    window_end_utc TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    recommendations_json TEXT NOT NULL,
    sample_warning INTEGER NOT NULL CHECK(sample_warning IN (0,1)),
    report_hash TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE compliance_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL CHECK(event_type IN ('suppression_added','suppression_expired','suppression_checked','retention_review')),
    subject_type TEXT,
    suppression_rule_id TEXT REFERENCES suppression_rules(id),
    result TEXT NOT NULL,
    details_json TEXT NOT NULL,
    occurred_at_utc TEXT NOT NULL,
    actor TEXT NOT NULL
);
CREATE INDEX ix_compliance_events_time ON compliance_events(occurred_at_utc DESC);

CREATE TRIGGER immutable_active_audit_policy BEFORE UPDATE OF rules_json,rules_hash,policy_key,version
ON audit_policy_versions WHEN OLD.state='active'
BEGIN SELECT RAISE(ABORT, 'active audit policy is immutable; create a new version'); END;
