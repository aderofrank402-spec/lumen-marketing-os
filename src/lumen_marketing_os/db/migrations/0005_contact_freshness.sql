ALTER TABLE contact_provenance ADD COLUMN source_first_seen_on TEXT;
ALTER TABLE contact_provenance ADD COLUMN source_last_seen_on TEXT;
ALTER TABLE contact_provenance ADD COLUMN still_on_page INTEGER CHECK(still_on_page IN (0,1));
