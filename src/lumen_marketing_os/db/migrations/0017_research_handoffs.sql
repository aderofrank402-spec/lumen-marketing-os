CREATE TABLE research_handoff_receipts (
    id TEXT PRIMARY KEY,
    outbox_event_id TEXT NOT NULL UNIQUE REFERENCES outbox_events(id),
    finding_id TEXT NOT NULL REFERENCES research_findings(id),
    destination TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK(disposition IN ('routed','manual_review','rejected','quarantined')),
    target_type TEXT,
    target_id TEXT,
    reason TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);
CREATE INDEX ix_research_handoff_receipts_finding ON research_handoff_receipts(finding_id,destination);

CREATE TABLE research_handoff_intakes (
    id TEXT PRIMARY KEY,
    finding_id TEXT NOT NULL REFERENCES research_findings(id),
    destination TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('needs_review','routed','promoted','dismissed')),
    reason TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    reviewer TEXT,
    reviewed_at_utc TEXT,
    created_at_utc TEXT NOT NULL,
    UNIQUE(finding_id,destination)
);
CREATE INDEX ix_research_handoff_intakes_state ON research_handoff_intakes(state,destination,created_at_utc);
