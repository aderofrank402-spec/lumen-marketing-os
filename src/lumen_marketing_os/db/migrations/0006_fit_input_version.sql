ALTER TABLE fit_assessments ADD COLUMN input_hash TEXT;
DROP INDEX ux_fit_assessment_version;
CREATE UNIQUE INDEX ux_fit_assessment_input ON fit_assessments(
    prospect_id, COALESCE(snapshot_id, ''), rubric_version,
    COALESCE(config_hash, ''), COALESCE(input_hash, '')
);
