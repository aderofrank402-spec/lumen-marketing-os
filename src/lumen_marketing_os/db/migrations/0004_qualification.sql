CREATE TABLE qualification_intakes (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    source_observation_id TEXT REFERENCES source_observations(id),
    finding_id TEXT REFERENCES research_findings(id),
    state TEXT NOT NULL CHECK(state IN ('needs_review','admitted','rejected')),
    reviewer TEXT,
    reason TEXT,
    created_at_utc TEXT NOT NULL,
    reviewed_at_utc TEXT,
    UNIQUE(organization_id, finding_id)
);
CREATE INDEX ix_qualification_intakes_state ON qualification_intakes(state, created_at_utc);

CREATE TABLE qualification_rubrics (
    version TEXT PRIMARY KEY,
    config_hash TEXT NOT NULL,
    config_json TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at_utc TEXT NOT NULL
);

CREATE UNIQUE INDEX ux_prospect_identity_segment ON prospects(
    organization_id, segment, COALESCE(use_case_id, '')
);
ALTER TABLE prospects ADD COLUMN location_hint TEXT;
ALTER TABLE prospects ADD COLUMN pain_evidence_url TEXT;
ALTER TABLE prospects ADD COLUMN pain_evidence_note TEXT;

ALTER TABLE enrichment_attempts ADD COLUMN request_json TEXT;
ALTER TABLE enrichment_attempts ADD COLUMN provider_business_id TEXT;
ALTER TABLE enrichment_attempts ADD COLUMN poll_path TEXT;
ALTER TABLE enrichment_attempts ADD COLUMN poll_after_utc TEXT;
ALTER TABLE enrichment_attempts ADD COLUMN poll_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE enrichment_attempts ADD COLUMN provider_profile_id TEXT;
ALTER TABLE enrichment_attempts ADD COLUMN last_http_status INTEGER;
CREATE INDEX ix_enrichment_poll_due ON enrichment_attempts(outcome, poll_after_utc);

ALTER TABLE fit_assessments ADD COLUMN config_hash TEXT;
CREATE UNIQUE INDEX ux_fit_assessment_version ON fit_assessments(
    prospect_id, COALESCE(snapshot_id, ''), rubric_version, COALESCE(config_hash, '')
);

CREATE TABLE contact_research (
    prospect_id TEXT PRIMARY KEY REFERENCES prospects(id),
    state TEXT NOT NULL CHECK(state IN ('pending','in_progress','complete','blocked','needs_review')),
    provider TEXT NOT NULL,
    error_class TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_checked_at_utc TEXT,
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE contact_provenance (
    id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL REFERENCES contacts(id),
    provider TEXT NOT NULL,
    source_url TEXT NOT NULL,
    provider_confidence REAL,
    provider_verification TEXT,
    observed_at_utc TEXT NOT NULL,
    UNIQUE(contact_id, provider, source_url)
);
CREATE INDEX ix_contact_provenance_contact ON contact_provenance(contact_id);

CREATE TABLE qualification_provider_calls (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    prospect_id TEXT NOT NULL REFERENCES prospects(id),
    request_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN ('reserved','complete','unknown')),
    reserved_at_utc TEXT NOT NULL,
    finished_at_utc TEXT
);
CREATE INDEX ix_qualification_provider_calls_day ON qualification_provider_calls(provider, reserved_at_utc);
