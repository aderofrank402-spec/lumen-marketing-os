CREATE TABLE research_findings (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL CHECK(agent_id IN ('1A','1B','1C','1E')),
    source_observation_id TEXT NOT NULL REFERENCES source_observations(id),
    finding_type TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    subject_name TEXT,
    subject_domain TEXT,
    evidence_url TEXT NOT NULL,
    evidence_excerpt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'needs_codex_review'
        CHECK(status IN ('needs_codex_review','reviewed','rejected')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL,
    reviewed_at_utc TEXT,
    reviewer TEXT,
    UNIQUE(agent_id, source_observation_id, finding_type)
);
CREATE INDEX ix_research_findings_review ON research_findings(status, agent_id, created_at_utc);

CREATE TABLE research_source_health (
    source_key TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    last_checked_at_utc TEXT NOT NULL,
    last_success_at_utc TEXT,
    status TEXT NOT NULL CHECK(status IN ('ok','degraded','blocked')),
    error_class TEXT,
    item_count INTEGER NOT NULL DEFAULT 0 CHECK(item_count >= 0)
);

CREATE TABLE research_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    agent_id TEXT NOT NULL CHECK(agent_id IN ('1A','1B','1C','1E')),
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    sources_checked INTEGER NOT NULL DEFAULT 0,
    sources_failed INTEGER NOT NULL DEFAULT 0,
    observations_new INTEGER NOT NULL DEFAULT 0,
    findings_new INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('running','succeeded','partial','failed'))
);
