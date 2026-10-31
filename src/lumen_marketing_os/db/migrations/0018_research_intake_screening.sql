CREATE TABLE research_intake_screenings (
    finding_id TEXT PRIMARY KEY REFERENCES research_findings(id),
    state TEXT NOT NULL CHECK(state IN ('active','restored')),
    rule_version TEXT NOT NULL,
    reason TEXT NOT NULL,
    screened_by TEXT NOT NULL,
    screened_at_utc TEXT NOT NULL,
    restored_by TEXT,
    restored_at_utc TEXT
);
CREATE INDEX ix_research_intake_screenings_state ON research_intake_screenings(state,screened_at_utc);
