ALTER TABLE community_import_batches
    ADD COLUMN submitted_rows INTEGER NOT NULL DEFAULT 0;

CREATE TABLE community_task_import_links (
    task_id TEXT NOT NULL REFERENCES community_tasks(id),
    batch_id TEXT NOT NULL REFERENCES community_import_batches(id),
    linked_at_utc TEXT NOT NULL,
    PRIMARY KEY (task_id, batch_id)
);

CREATE INDEX ix_community_task_import_links_batch
    ON community_task_import_links(batch_id);
