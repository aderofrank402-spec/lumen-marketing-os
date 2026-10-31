CREATE TABLE source_observations (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    external_id TEXT,
    canonical_url TEXT,
    content_hash TEXT NOT NULL,
    excerpt TEXT,
    permitted_use TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    fetched_at_utc TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_source_external ON source_observations(source_type, external_id) WHERE external_id IS NOT NULL;
CREATE UNIQUE INDEX ux_source_url_content ON source_observations(source_type, canonical_url, content_hash) WHERE canonical_url IS NOT NULL;

CREATE TABLE organizations (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    country_code TEXT,
    canonical_domain TEXT,
    identity_status TEXT NOT NULL CHECK(identity_status IN ('candidate','verified','ambiguous','merged')),
    merged_into_id TEXT REFERENCES organizations(id),
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_organization_verified_domain ON organizations(canonical_domain) WHERE canonical_domain IS NOT NULL AND identity_status='verified';
CREATE INDEX ix_organization_name_country ON organizations(normalized_name, country_code);

CREATE TABLE organization_aliases (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    source_observation_id TEXT REFERENCES source_observations(id),
    created_at_utc TEXT NOT NULL,
    UNIQUE(organization_id, normalized_alias)
);

CREATE TABLE contacts (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    display_name TEXT,
    role_title TEXT,
    channel TEXT NOT NULL,
    address TEXT,
    normalized_address TEXT,
    verification_status TEXT NOT NULL CHECK(verification_status IN ('candidate','verified','invalid','unknown')),
    source_observation_id TEXT REFERENCES source_observations(id),
    verified_at_utc TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_contact_address_org ON contacts(organization_id, channel, normalized_address) WHERE normalized_address IS NOT NULL;

CREATE TABLE use_cases (
    id TEXT PRIMARY KEY,
    vertical TEXT NOT NULL,
    problem_statement TEXT NOT NULL,
    buyer_hypothesis TEXT,
    status TEXT NOT NULL CHECK(status IN ('candidate','review_ready','approved','rejected','retired')),
    owner TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);
CREATE TABLE research_dossiers (
    id TEXT PRIMARY KEY,
    use_case_id TEXT NOT NULL REFERENCES use_cases(id),
    version INTEGER NOT NULL CHECK(version > 0),
    findings_json TEXT NOT NULL,
    counter_evidence_json TEXT NOT NULL,
    reviewer TEXT,
    created_at_utc TEXT NOT NULL,
    UNIQUE(use_case_id, version)
);
CREATE TABLE observation_links (
    source_observation_id TEXT NOT NULL REFERENCES source_observations(id),
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    PRIMARY KEY(source_observation_id, entity_type, entity_id, relation)
);

CREATE TABLE prospects (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    use_case_id TEXT REFERENCES use_cases(id),
    segment TEXT NOT NULL,
    acquisition_source_id TEXT REFERENCES source_observations(id),
    lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN ('discovered','enrichment_pending','enriched','enrichment_failed','ambiguous_review','qualified','rejected','drafted','review_pending','approved','action_queued','contacted','replied','trial','paid','suppressed','paused','closed')),
    priority INTEGER NOT NULL DEFAULT 0,
    owner TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE(organization_id, segment, use_case_id)
);
CREATE INDEX ix_prospect_lifecycle ON prospects(lifecycle_state, priority);

CREATE TABLE enrichment_attempts (
    id TEXT PRIMARY KEY,
    prospect_id TEXT NOT NULL REFERENCES prospects(id),
    request_key TEXT NOT NULL UNIQUE,
    adapter_version TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('started','success','not_found','ambiguous','timeout','transient_error','permanent_error')),
    error_class TEXT,
    latency_ms INTEGER,
    cost_amount REAL,
    CHECK(latency_ms IS NULL OR latency_ms >= 0)
);
CREATE TABLE enrichment_snapshots (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES enrichment_attempts(id),
    schema_version TEXT NOT NULL,
    raw_response_json TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    field_evidence_json TEXT NOT NULL,
    ambiguous INTEGER NOT NULL DEFAULT 0 CHECK(ambiguous IN (0,1)),
    created_at_utc TEXT NOT NULL
);
CREATE TABLE fit_assessments (
    id TEXT PRIMARY KEY,
    prospect_id TEXT NOT NULL REFERENCES prospects(id),
    snapshot_id TEXT REFERENCES enrichment_snapshots(id),
    rubric_version TEXT NOT NULL,
    score REAL,
    reasons_json TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('qualified','review','rejected')),
    assessed_at_utc TEXT NOT NULL
);

CREATE TABLE campaigns (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    segment TEXT NOT NULL,
    channel TEXT NOT NULL,
    objective TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','approved','active','paused','completed','cancelled')),
    starts_at_utc TEXT,
    ends_at_utc TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);
CREATE TABLE campaign_variants (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    variant_key TEXT NOT NULL,
    template_version TEXT NOT NULL,
    hypothesis TEXT,
    allocation_weight REAL NOT NULL CHECK(allocation_weight >= 0 AND allocation_weight <= 1),
    created_at_utc TEXT NOT NULL,
    UNIQUE(campaign_id, variant_key)
);
CREATE TABLE drafts (
    id TEXT PRIMARY KEY,
    prospect_id TEXT REFERENCES prospects(id),
    contact_id TEXT REFERENCES contacts(id),
    campaign_id TEXT REFERENCES campaigns(id),
    channel TEXT NOT NULL,
    subject TEXT,
    body TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    evidence_json TEXT NOT NULL,
    author_role TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','review_pending','review_passed','blocked','approved','retired')),
    created_at_utc TEXT NOT NULL,
    UNIQUE(id, content_hash)
);
CREATE TABLE review_findings (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(id),
    reviewer_role TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('info','warning','blocking')),
    finding TEXT NOT NULL,
    disposition TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);
CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(id),
    content_hash TEXT NOT NULL,
    target_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    action_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected','revoked')),
    decided_at_utc TEXT NOT NULL,
    expires_at_utc TEXT,
    reason TEXT
);
CREATE INDEX ix_approval_target ON approvals(target_id, channel, action_type, decision);

CREATE TABLE actions (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    campaign_id TEXT REFERENCES campaigns(id),
    draft_id TEXT REFERENCES drafts(id),
    approval_id TEXT REFERENCES approvals(id),
    due_at_utc TEXT,
    state TEXT NOT NULL CHECK(state IN ('drafted','review_pending','approved','queued','reserved','dispatching','accepted','rejected','unknown','cancelled')),
    idempotency_key TEXT NOT NULL UNIQUE,
    policy_version TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);
CREATE INDEX ix_action_due ON actions(state, due_at_utc);
CREATE TABLE delivery_attempts (
    id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL REFERENCES actions(id),
    provider_request_id TEXT,
    provider_response_id TEXT,
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    status TEXT NOT NULL CHECK(status IN ('started','accepted','rejected','unknown')),
    redacted_diagnostic TEXT
);

CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    contact_id TEXT REFERENCES contacts(id),
    channel TEXT NOT NULL,
    provider_thread_id TEXT,
    intent_status TEXT NOT NULL DEFAULT 'unclassified',
    owner TEXT,
    last_activity_at_utc TEXT NOT NULL,
    UNIQUE(channel, provider_thread_id)
);
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT REFERENCES conversations(id),
    action_id TEXT REFERENCES actions(id),
    provider_message_id TEXT,
    direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound')),
    channel TEXT NOT NULL,
    body_reference TEXT,
    delivery_status TEXT,
    occurred_at_utc TEXT NOT NULL,
    UNIQUE(channel, provider_message_id)
);
CREATE TABLE inbound_events (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    conversation_id TEXT REFERENCES conversations(id),
    event_type TEXT NOT NULL,
    raw_reference TEXT,
    classification TEXT,
    confidence REAL,
    processing_status TEXT NOT NULL DEFAULT 'pending',
    received_at_utc TEXT NOT NULL,
    UNIQUE(provider, provider_event_id)
);
CREATE TABLE support_tickets (
    id TEXT PRIMARY KEY,
    source_event_id TEXT UNIQUE REFERENCES inbound_events(id),
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    owner TEXT,
    response_draft_id TEXT REFERENCES drafts(id),
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE trials (
    id TEXT PRIMARY KEY,
    product_account_id TEXT NOT NULL UNIQUE,
    prospect_id TEXT REFERENCES prospects(id),
    source_reference TEXT NOT NULL,
    started_at_utc TEXT NOT NULL
);
CREATE TABLE paid_conversions (
    id TEXT PRIMARY KEY,
    product_account_id TEXT NOT NULL,
    prospect_id TEXT REFERENCES prospects(id),
    provider TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    amount_minor INTEGER,
    currency TEXT,
    verification_reference TEXT NOT NULL,
    paid_at_utc TEXT NOT NULL,
    verified_at_utc TEXT NOT NULL,
    UNIQUE(provider, transaction_id)
);

CREATE TABLE suppression_rules (
    id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK(subject_type IN ('address','domain','organization','account')),
    keyed_value TEXT NOT NULL,
    scope TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_event_id TEXT REFERENCES inbound_events(id),
    created_at_utc TEXT NOT NULL,
    expires_at_utc TEXT,
    UNIQUE(subject_type, keyed_value, scope)
);
CREATE TABLE quota_policies (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    account_id TEXT NOT NULL,
    period_seconds INTEGER NOT NULL CHECK(period_seconds > 0),
    max_units INTEGER NOT NULL CHECK(max_units >= 0),
    policy_version TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at_utc TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1))
);
CREATE TABLE quota_ledger (
    id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL REFERENCES quota_policies(id),
    action_id TEXT UNIQUE REFERENCES actions(id),
    period_start_utc TEXT NOT NULL,
    units INTEGER NOT NULL CHECK(units > 0),
    state TEXT NOT NULL CHECK(state IN ('reserved','committed','released','unknown')),
    created_at_utc TEXT NOT NULL
);
CREATE INDEX ix_quota_period ON quota_ledger(policy_id, period_start_utc, state);
CREATE TABLE provider_usage (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT,
    job_id TEXT,
    units INTEGER,
    cost_amount REAL,
    currency TEXT,
    reconciliation_status TEXT NOT NULL,
    occurred_at_utc TEXT NOT NULL
);
