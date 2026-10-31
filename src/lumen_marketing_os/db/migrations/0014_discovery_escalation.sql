CREATE TABLE vertical_opportunities (
    id TEXT PRIMARY KEY,
    signal_key TEXT NOT NULL UNIQUE,
    vertical TEXT NOT NULL,
    problem TEXT NOT NULL,
    buyer TEXT NOT NULL,
    source_observation_id TEXT REFERENCES source_observations(id),
    state TEXT NOT NULL CHECK(state IN ('candidate','validated','rejected','dispatched')),
    score INTEGER NOT NULL DEFAULT 0,
    use_case_id TEXT UNIQUE REFERENCES use_cases(id),
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);
CREATE TABLE vertical_evidence (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES vertical_opportunities(id),
    polarity TEXT NOT NULL CHECK(polarity IN ('support','counter')),
    source_url TEXT NOT NULL,
    source_domain TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    UNIQUE(opportunity_id,polarity,source_url)
);
CREATE TABLE vertical_dossiers (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL UNIQUE REFERENCES vertical_opportunities(id),
    buyer_workflow TEXT NOT NULL,
    product_fit TEXT NOT NULL,
    limitations TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);
CREATE TABLE vertical_candidates (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES vertical_opportunities(id),
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    source_observation_id TEXT NOT NULL REFERENCES source_observations(id),
    pain_url TEXT NOT NULL,
    pain_note TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('needs_review','accepted','rejected','dispatched')),
    reviewer TEXT,
    intake_id TEXT UNIQUE REFERENCES qualification_intakes(id),
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE(opportunity_id,organization_id)
);
CREATE TABLE vertical_cycles (
    id TEXT PRIMARY KEY,
    cycle_key TEXT NOT NULL UNIQUE,
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT NOT NULL,
    signals_seen INTEGER NOT NULL,
    signals_new INTEGER NOT NULL,
    dispatched INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('complete','partial'))
);
CREATE TABLE vertical_tasks (
    id TEXT PRIMARY KEY,
    task_key TEXT NOT NULL UNIQUE,
    cycle_id TEXT NOT NULL REFERENCES vertical_cycles(id),
    agent_id TEXT NOT NULL CHECK(agent_id IN ('8A','8B','8C','8D')),
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','completed','blocked')),
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);
CREATE INDEX ix_vertical_opportunities ON vertical_opportunities(state,score,created_at_utc);
CREATE INDEX ix_vertical_candidates ON vertical_candidates(state,opportunity_id);
CREATE INDEX ix_vertical_tasks ON vertical_tasks(agent_id,state,created_at_utc);
