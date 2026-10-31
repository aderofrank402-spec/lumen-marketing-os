CREATE TABLE research_review_results (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    finding_id TEXT NOT NULL UNIQUE REFERENCES research_findings(id),
    agent_id TEXT NOT NULL CHECK(agent_id IN ('1A','1B','1C','1E')),
    role_name TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('reviewed','rejected')),
    assessment_json TEXT NOT NULL,
    source_checked_at_utc TEXT NOT NULL,
    reviewed_by TEXT NOT NULL,
    agent_run_ref TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);
CREATE INDEX ix_research_review_results_agent ON research_review_results(agent_id,created_at_utc);
