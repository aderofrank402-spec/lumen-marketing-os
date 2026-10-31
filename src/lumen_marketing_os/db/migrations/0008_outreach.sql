CREATE TABLE outreach_accounts (
    id TEXT PRIMARY KEY,
    email_address TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL CHECK(provider IN ('gmail')),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    calendar_id TEXT,
    send_timezone TEXT,
    send_start_local TEXT,
    send_end_local TEXT,
    send_weekdays_json TEXT,
    policy_version TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE outreach_action_bindings (
    action_id TEXT PRIMARY KEY REFERENCES actions(id),
    account_id TEXT NOT NULL REFERENCES outreach_accounts(id),
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    thread_id TEXT,
    source_message_id TEXT,
    created_at_utc TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_outreach_source_reply ON outreach_action_bindings(source_message_id)
    WHERE source_message_id IS NOT NULL;

CREATE TABLE inbox_cursors (
    account_id TEXT PRIMARY KEY REFERENCES outreach_accounts(id),
    cursor TEXT,
    checked_at_utc TEXT NOT NULL
);
CREATE TABLE inbound_message_content (
    message_id TEXT PRIMARY KEY REFERENCES messages(id),
    subject TEXT,
    body TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT,
    stored_at_utc TEXT NOT NULL
);
CREATE TABLE inbound_classifications (
    event_id TEXT PRIMARY KEY REFERENCES inbound_events(id),
    label TEXT NOT NULL CHECK(label IN ('warm','meeting','question','objection','support','opt_out','bounce','complaint','out_of_office','unrelated','needs_review')),
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    rationale TEXT NOT NULL,
    classifier_version TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at_utc TEXT
);
CREATE TABLE response_proposals (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL UNIQUE REFERENCES drafts(id),
    event_id TEXT NOT NULL UNIQUE REFERENCES inbound_events(id),
    reply_to_message_id TEXT NOT NULL REFERENCES messages(id),
    account_id TEXT NOT NULL REFERENCES outreach_accounts(id),
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    proposed_slots_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('draft','reviewed','retired')),
    author TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);
CREATE TABLE calendar_booking_bindings (
    action_id TEXT PRIMARY KEY REFERENCES actions(id),
    account_id TEXT NOT NULL REFERENCES outreach_accounts(id),
    calendar_id TEXT NOT NULL,
    attendee TEXT NOT NULL,
    title TEXT NOT NULL,
    start_at_utc TEXT NOT NULL,
    end_at_utc TEXT NOT NULL,
    source_event_id TEXT REFERENCES inbound_events(id),
    created_at_utc TEXT NOT NULL
);
CREATE INDEX ix_booking_source ON calendar_booking_bindings(source_event_id)
    WHERE source_event_id IS NOT NULL;

CREATE INDEX ix_inbound_class_label ON inbound_classifications(label, reviewed_at_utc);
CREATE INDEX ix_outreach_action_account ON outreach_action_bindings(account_id);
