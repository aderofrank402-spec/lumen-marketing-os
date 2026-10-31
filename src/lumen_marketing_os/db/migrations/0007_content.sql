CREATE TABLE content_claims (
    id TEXT PRIMARY KEY,
    claim_key TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    claim_type TEXT NOT NULL CHECK(claim_type IN ('product','customer')),
    claim_text TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    permission_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(permission_confirmed IN (0,1)),
    status TEXT NOT NULL CHECK(status IN ('draft','approved','retired')),
    approved_by TEXT,
    approved_at_utc TEXT,
    created_at_utc TEXT NOT NULL,
    UNIQUE(claim_key, version)
);
CREATE INDEX ix_content_claim_status ON content_claims(status, claim_type);

CREATE TABLE content_templates (
    id TEXT PRIMARY KEY,
    template_key TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    kind TEXT NOT NULL CHECK(kind IN ('personalized','technical','case_study')),
    channel TEXT NOT NULL,
    subject_template TEXT,
    body_template TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','approved','retired')),
    approved_by TEXT,
    approved_at_utc TEXT,
    created_at_utc TEXT NOT NULL,
    UNIQUE(template_key, version)
);

CREATE TABLE content_intakes (
    id TEXT PRIMARY KEY,
    prospect_id TEXT NOT NULL REFERENCES prospects(id),
    contact_id TEXT NOT NULL REFERENCES contacts(id),
    source_event_id TEXT NOT NULL UNIQUE REFERENCES outbox_events(id),
    state TEXT NOT NULL CHECK(state IN ('needs_review','ready','rejected')),
    reviewer TEXT,
    reason TEXT,
    created_at_utc TEXT NOT NULL,
    reviewed_at_utc TEXT
);

CREATE TABLE content_requests (
    id TEXT PRIMARY KEY,
    intake_id TEXT REFERENCES content_intakes(id),
    prospect_id TEXT REFERENCES prospects(id),
    contact_id TEXT REFERENCES contacts(id),
    kind TEXT NOT NULL CHECK(kind IN ('personalized','technical','case_study')),
    template_id TEXT NOT NULL REFERENCES content_templates(id),
    status TEXT NOT NULL CHECK(status IN ('pending','drafted','blocked','cancelled')),
    logical_key TEXT NOT NULL UNIQUE,
    requested_by TEXT NOT NULL,
    requested_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);
CREATE TABLE content_request_claims (
    request_id TEXT NOT NULL REFERENCES content_requests(id),
    claim_id TEXT NOT NULL REFERENCES content_claims(id),
    PRIMARY KEY(request_id, claim_id)
);

ALTER TABLE drafts ADD COLUMN request_id TEXT REFERENCES content_requests(id);
ALTER TABLE drafts ADD COLUMN parent_draft_id TEXT REFERENCES drafts(id);
ALTER TABLE drafts ADD COLUMN input_hash TEXT;
ALTER TABLE drafts ADD COLUMN template_id TEXT REFERENCES content_templates(id);
CREATE UNIQUE INDEX ux_draft_request_version ON drafts(request_id, version) WHERE request_id IS NOT NULL;
CREATE TRIGGER immutable_draft_content BEFORE UPDATE OF subject,body,content_hash,evidence_json,version,request_id,input_hash,template_id ON drafts
BEGIN SELECT RAISE(ABORT, 'draft content is immutable; create a new version'); END;
CREATE TRIGGER immutable_approved_claim BEFORE UPDATE OF claim_text,evidence_ref,claim_type,permission_confirmed ON content_claims
WHEN OLD.status='approved'
BEGIN SELECT RAISE(ABORT, 'approved claim is immutable; create a new version'); END;
CREATE TRIGGER immutable_approved_template BEFORE UPDATE OF subject_template,body_template,kind,channel ON content_templates
WHEN OLD.status='approved'
BEGIN SELECT RAISE(ABORT, 'approved template is immutable; create a new version'); END;

CREATE TABLE content_reviews (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(id),
    content_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    review_round INTEGER NOT NULL CHECK(review_round > 0),
    claim_audit_id TEXT,
    decision TEXT NOT NULL CHECK(decision IN ('passed','blocked')),
    reviewer_role TEXT NOT NULL,
    reviewed_at_utc TEXT NOT NULL,
    UNIQUE(draft_id, review_round)
);

CREATE TABLE content_claim_audits (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(id),
    content_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    rationale TEXT NOT NULL,
    reviewed_at_utc TEXT NOT NULL,
    UNIQUE(draft_id, content_hash, input_hash)
);
ALTER TABLE review_findings ADD COLUMN content_review_id TEXT REFERENCES content_reviews(id);

CREATE TABLE content_experiments (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    metric_key TEXT NOT NULL,
    window_start_at_utc TEXT NOT NULL,
    window_end_at_utc TEXT NOT NULL,
    minimum_exposures INTEGER NOT NULL CHECK(minimum_exposures >= 1),
    minimum_conversions INTEGER NOT NULL CHECK(minimum_conversions >= 1),
    status TEXT NOT NULL CHECK(status IN ('draft','active','closed')),
    created_by TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE(campaign_id, metric_key)
);
CREATE TABLE content_exposures (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES content_experiments(id),
    variant_id TEXT NOT NULL REFERENCES campaign_variants(id),
    subject_key TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    UNIQUE(experiment_id, subject_key)
);
CREATE TABLE content_outcomes (
    id TEXT PRIMARY KEY,
    exposure_id TEXT NOT NULL REFERENCES content_exposures(id),
    metric_key TEXT NOT NULL,
    outcome_key TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    UNIQUE(exposure_id, metric_key),
    UNIQUE(metric_key, outcome_key)
);
CREATE TABLE content_recommendations (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES content_experiments(id),
    analysis_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('insufficient_evidence','review_required')),
    created_at_utc TEXT NOT NULL,
    UNIQUE(experiment_id, analysis_hash)
);
