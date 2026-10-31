CREATE TABLE community_trigger_definitions (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL CHECK(agent_id IN ('6A','6B','6C','6D','6E')),
    trigger_kind TEXT NOT NULL CHECK(trigger_kind IN ('interval','event','manual')),
    interval_seconds INTEGER CHECK(interval_seconds > 0),
    next_due_at_utc TEXT,
    active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1)),
    input_json TEXT NOT NULL DEFAULT '{}',
    config_version TEXT NOT NULL,
    owner TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    CHECK((trigger_kind='interval' AND interval_seconds IS NOT NULL AND next_due_at_utc IS NOT NULL)
       OR (trigger_kind!='interval' AND interval_seconds IS NULL AND next_due_at_utc IS NULL))
);
CREATE INDEX ix_community_triggers_due ON community_trigger_definitions(active, next_due_at_utc);

CREATE TABLE community_tasks (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL CHECK(agent_id IN ('6A','6B','6C','6D','6E')),
    trigger_id TEXT REFERENCES community_trigger_definitions(id),
    trigger_key TEXT NOT NULL UNIQUE,
    input_json TEXT NOT NULL,
    instructions_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','leased','needs_review','completed','blocked')),
    due_at_utc TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at_utc TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    last_error_class TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    CHECK((state='leased' AND lease_owner IS NOT NULL AND lease_expires_at_utc IS NOT NULL)
       OR (state!='leased' AND lease_owner IS NULL AND lease_expires_at_utc IS NULL))
);
CREATE INDEX ix_community_tasks_claim ON community_tasks(agent_id,state,due_at_utc);

CREATE TABLE community_observations (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL CHECK(platform IN ('reddit','hacker_news','product_hunt','launchpad','linkedin','directory')),
    external_id TEXT,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    captured_at_utc TEXT NOT NULL,
    captured_by TEXT NOT NULL,
    UNIQUE(platform,canonical_url)
);
CREATE UNIQUE INDEX ux_community_observation_external ON community_observations(platform,external_id)
    WHERE external_id IS NOT NULL;
CREATE TABLE community_observation_versions (
    id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES community_observations(id),
    content_sha256 TEXT NOT NULL,
    title TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    captured_at_utc TEXT NOT NULL,
    UNIQUE(observation_id,content_sha256)
);
CREATE TABLE community_task_observations (
    task_id TEXT NOT NULL REFERENCES community_tasks(id),
    observation_id TEXT NOT NULL REFERENCES community_observations(id),
    relevance_note TEXT NOT NULL,
    PRIMARY KEY(task_id,observation_id)
);

CREATE TABLE community_drafts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES community_tasks(id),
    observation_id TEXT REFERENCES community_observations(id),
    artifact_type TEXT NOT NULL CHECK(artifact_type IN ('helpful_reply','technical_article','technical_comment','launch_asset','launch_comment','linkedin_insight','linkedin_response')),
    body TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('needs_review','approved','rejected')),
    author TEXT NOT NULL,
    reviewer TEXT,
    review_note TEXT,
    created_at_utc TEXT NOT NULL,
    reviewed_at_utc TEXT,
    UNIQUE(task_id,artifact_type,body_sha256)
);
CREATE INDEX ix_community_drafts_review ON community_drafts(state,created_at_utc);

CREATE TABLE community_launch_items (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES community_tasks(id),
    item_key TEXT NOT NULL,
    description TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('open','ready','reviewed')),
    evidence_ref TEXT,
    reviewed_by TEXT,
    updated_at_utc TEXT NOT NULL,
    UNIQUE(task_id,item_key)
);

CREATE TABLE community_import_batches (
    id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_version TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE(source_name,source_version,input_sha256)
);
CREATE TABLE community_import_rows (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES community_import_batches(id),
    row_key TEXT NOT NULL,
    source_url TEXT,
    company_name TEXT,
    domain TEXT,
    country_code TEXT,
    state TEXT NOT NULL CHECK(state IN ('candidate','quarantined','sent_to_qualification')),
    reason TEXT,
    organization_id TEXT REFERENCES organizations(id),
    intake_id TEXT REFERENCES qualification_intakes(id),
    created_at_utc TEXT NOT NULL,
    UNIQUE(batch_id,row_key)
);
CREATE INDEX ix_community_import_rows_state ON community_import_rows(state,batch_id);

CREATE TABLE community_connection_registry (
    agent_id TEXT NOT NULL CHECK(agent_id IN ('6A','6B','6C','6D','6E')),
    platform TEXT NOT NULL,
    account_reference TEXT,
    state TEXT NOT NULL CHECK(state IN ('not_connected','ready','blocked')),
    last_verified_at_utc TEXT,
    failure_class TEXT,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY(agent_id,platform)
);
