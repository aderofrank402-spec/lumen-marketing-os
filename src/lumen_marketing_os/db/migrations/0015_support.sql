CREATE TABLE support_sources (
    id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN ('gmail','form','product_event','manual')),
    account_id TEXT REFERENCES outreach_accounts(id),
    external_reference TEXT NOT NULL,
    cursor_value TEXT,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    verified_by TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);
ALTER TABLE support_tickets ADD COLUMN source_id TEXT REFERENCES support_sources(id);
ALTER TABLE support_tickets ADD COLUMN provider_event_id TEXT;
ALTER TABLE support_tickets ADD COLUMN inbound_event_id TEXT REFERENCES inbound_events(id);
ALTER TABLE support_tickets ADD COLUMN subject TEXT;
ALTER TABLE support_tickets ADD COLUMN body TEXT;
ALTER TABLE support_tickets ADD COLUMN sender TEXT;
ALTER TABLE support_tickets ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal'
    CHECK(priority IN ('low','normal','high','urgent'));
ALTER TABLE support_tickets ADD COLUMN state TEXT NOT NULL DEFAULT 'new'
    CHECK(state IN ('new','needs_review','open','waiting_customer','resolved','closed'));
ALTER TABLE support_tickets ADD COLUMN version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0);
ALTER TABLE support_tickets ADD COLUMN received_at_utc TEXT;
CREATE UNIQUE INDEX ux_support_source_event ON support_tickets(source_id,provider_event_id)
    WHERE source_id IS NOT NULL AND provider_event_id IS NOT NULL;
CREATE UNIQUE INDEX ux_support_inbound_event ON support_tickets(inbound_event_id)
    WHERE inbound_event_id IS NOT NULL;
CREATE INDEX ix_support_triage ON support_tickets(state,priority,received_at_utc);
CREATE TABLE support_ticket_events (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES support_tickets(id),
    event_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    occurred_at_utc TEXT NOT NULL
);
CREATE TABLE support_classifications (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES support_tickets(id),
    ticket_version INTEGER NOT NULL,
    category TEXT NOT NULL,
    priority TEXT NOT NULL,
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    rationale TEXT NOT NULL,
    classifier_version TEXT NOT NULL,
    reviewed_by TEXT,
    created_at_utc TEXT NOT NULL,
    UNIQUE(ticket_id,ticket_version,classifier_version)
);
CREATE TABLE support_response_drafts (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES support_tickets(id),
    ticket_version INTEGER NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('needs_review','approved','retired')),
    author TEXT NOT NULL,
    approved_by TEXT,
    approved_at_utc TEXT,
    created_at_utc TEXT NOT NULL,
    UNIQUE(ticket_id,ticket_version,body_sha256)
);
CREATE TABLE support_sheet_sync (
    ticket_id TEXT PRIMARY KEY REFERENCES support_tickets(id),
    spreadsheet_id TEXT NOT NULL,
    row_key TEXT NOT NULL UNIQUE,
    target_version INTEGER NOT NULL,
    target_hash TEXT NOT NULL,
    synced_version INTEGER NOT NULL DEFAULT 0,
    synced_hash TEXT,
    state TEXT NOT NULL CHECK(state IN ('pending','synced','failed')),
    last_error TEXT,
    updated_at_utc TEXT NOT NULL
);
CREATE TABLE support_response_handoffs (
    support_draft_id TEXT PRIMARY KEY REFERENCES support_response_drafts(id),
    outreach_proposal_id TEXT NOT NULL UNIQUE REFERENCES response_proposals(id),
    created_at_utc TEXT NOT NULL
);
