from __future__ import annotations

import pytest
from pathlib import Path

from lumen_marketing_os.core.errors import PausedError
from lumen_marketing_os.workflows.community.indexer import ProspectListIndexer
from lumen_marketing_os.workflows.community.runtime import CommunityRuntime


SOURCE = "https://directory.example.org/export/2026"


def row(**changes):
    result = {"source_url": "https://directory.example.org/listing/acme",
              "company_name": "Acme Analytics", "domain": "acme.example",
              "country": "KE"}
    result.update(changes)
    return result


def make_import(indexer, rows, version="v1"):
    return indexer.import_rows(source_name="Example Directory", source_url=SOURCE,
                               source_version=version, approved_by="operator", rows=rows)


def test_import_creates_sourced_candidate_and_review_only_intake(foundation):
    _, database, *_ = foundation
    indexer = ProspectListIndexer(database)
    result = make_import(indexer, [row()])
    assert result["states"] == {"sent_to_qualification": 1}
    with database.read() as connection:
        imported = connection.execute("SELECT * FROM community_import_rows").fetchone()
        org = connection.execute("SELECT * FROM organizations WHERE id=?",
                                 (imported["organization_id"],)).fetchone()
        intake = connection.execute("SELECT * FROM qualification_intakes WHERE id=?",
                                    (imported["intake_id"],)).fetchone()
        observation = connection.execute("SELECT * FROM source_observations WHERE id=?",
                                         (intake["source_observation_id"],)).fetchone()
        assert org["identity_status"] == "candidate"
        assert org["canonical_domain"] == "acme.example"
        assert intake["state"] == "needs_review" and intake["finding_id"] is None
        assert observation["canonical_url"] == row()["source_url"]
        assert observation["permitted_use"] == "internal_research"
        assert connection.execute("SELECT COUNT(*) FROM prospects").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    assert indexer.report(batch_id=result["batch_id"])["rows"] == 1


def test_reimport_and_duplicate_rows_are_idempotent(foundation):
    _, database, *_ = foundation
    indexer = ProspectListIndexer(database)
    first = make_import(indexer, [row(), row(domain="https://www.acme.example/path",
                                             source_url=row()["source_url"] + "#fragment")])
    assert first["rows"] == 1
    assert first["reimported"] is False
    again = make_import(indexer, [row(), row(domain="https://www.acme.example/path",
                                             source_url=row()["source_url"] + "#fragment")])
    assert again["batch_id"] == first["batch_id"]
    assert again["reimported"] is True
    next_version = make_import(indexer, [row()], version="v2")
    assert next_version["batch_id"] != first["batch_id"]
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM qualification_intakes").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] == 1


def test_invalid_rows_quarantined_without_unsafe_downstream_objects(foundation):
    _, database, *_ = foundation
    indexer = ProspectListIndexer(database)
    result = make_import(indexer, [row(source_url="not-a-url"),
                                        row(company_name="!", source_url="https://dir.example/x"),
                                        row(domain="invalid", source_url="https://dir.example/y"),
                                        row(country="Kenya", source_url="https://dir.example/z")])
    assert result["states"] == {"quarantined": 4}
    assert set(result["quarantine_reasons"]) == {
        "invalid_source_url", "invalid_company_name", "invalid_domain", "invalid_country_code"}
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM qualification_intakes").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] == 0


def test_existing_candidate_reused_but_ambiguous_match_quarantined(foundation):
    _, database, *_ = foundation
    indexer = ProspectListIndexer(database)
    make_import(indexer, [row()])
    second = make_import(indexer, [row(source_url="https://directory.example.org/listing/acme-2")],
                         version="v2")
    assert second["states"] == {"sent_to_qualification": 1}
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO organizations(id,display_name,normalized_name,country_code,"
            "canonical_domain,identity_status,created_at_utc,updated_at_utc) "
            "VALUES ('duplicate','Acme Analytics','acme analytics','KE','acme.example',"
            "'candidate','2026-01-01','2026-01-01')")
    third = make_import(indexer, [row(source_url="https://directory.example.org/listing/acme-3")],
                        version="v3")
    assert third["quarantine_reasons"] == {"ambiguous_domain_match": 1}
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM qualification_intakes").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] == 3


def test_name_country_conflict_and_verified_identity_quarantined(foundation):
    _, database, *_ = foundation
    indexer = ProspectListIndexer(database)
    make_import(indexer, [row()])
    mismatch = make_import(indexer, [row(domain="other.example")], version="v2")
    assert mismatch["quarantine_reasons"] == {"ambiguous_name_country_match": 1}
    with database.transaction() as connection:
        connection.execute("UPDATE organizations SET identity_status='verified' "
                           "WHERE canonical_domain='acme.example'")
    verified = make_import(indexer, [row(source_url="https://dir.example/verified")], version="v3")
    assert verified["quarantine_reasons"] == {"existing_non_candidate_identity": 1}


def test_import_requires_approved_source_and_rolls_back_non_json(foundation):
    _, database, *_ = foundation
    indexer = ProspectListIndexer(database)
    with pytest.raises(ValueError):
        indexer.import_rows(source_name="", source_url=SOURCE, source_version="v1",
                            approved_by="operator", rows=[row()])
    with pytest.raises(TypeError):
        make_import(indexer, [row(), row(source_url=object())])
    assert indexer.report() == {"batches": 0, "rows": 0, "states": {}}


def test_prior_review_decision_is_not_automatically_reopened(foundation):
    _, database, *_ = foundation
    indexer = ProspectListIndexer(database)
    make_import(indexer, [row()])
    with database.transaction() as connection:
        connection.execute("UPDATE qualification_intakes SET state='rejected',"
                           "reviewer='operator',reason='not a fit',reviewed_at_utc='2026-01-01'")
    again = make_import(indexer, [row(source_url="https://dir.example/new-evidence")], version="v2")
    assert again["quarantine_reasons"] == {"prior_review_decision": 1}
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM qualification_intakes").fetchone()[0] == 1


def test_claimed_6e_task_requires_a_linked_sourced_batch(foundation):
    clock, database, pause, *_ = foundation
    guide_dir = Path(__file__).parents[1] / "DEPARTMENT_6_AGENT_GUIDES"
    community = CommunityRuntime(database, pause, guide_dir, clock=clock)
    indexer = ProspectListIndexer(database, clock=clock)
    scope = {"source_name": "Example Directory", "source_url": SOURCE,
             "account_reference": "public:example-directory"}
    task_id = community.trigger(agent_id="6E", trigger_key="directory:test:v1",
                                input_payload=scope)
    community.connection(agent_id="6E", platform="directory", state="ready",
                         account_reference=scope["account_reference"], actor="operator",
                         verified=True)
    community.claim(agent_id="6E", worker_id="codex-6E")
    with pytest.raises(ValueError, match="linked import batch"):
        community.finish(task_id=task_id, worker_id="codex-6E", outcome="review",
                         reason="not imported")
    with pytest.raises(ValueError, match="differs"):
        indexer.import_rows(source_name="Wrong Directory", source_url=SOURCE,
                            source_version="v1", approved_by="operator", rows=[row()],
                            task_id=task_id, worker_id="codex-6E")
    with pytest.raises(ValueError, match="lease"):
        indexer.import_rows(source_name="Example Directory", source_url=SOURCE,
                            source_version="v1", approved_by="operator", rows=[row()],
                            task_id=task_id, worker_id="other")
    result = indexer.import_rows(source_name="Example Directory", source_url=SOURCE,
                                 source_version="v1", approved_by="operator",
                                 rows=[row(source_row_id="row-1", field_provenance={
                                     "company_name": "directory title", "domain": "website link",
                                     "country": "location label"}),
                                       row(source_row_id="row-1", field_provenance={
                                           "company_name": "directory title", "domain": "website link",
                                           "country": "location label"})],
                                 task_id=task_id, worker_id="codex-6E")
    assert result["submitted_rows"] == 2
    assert result["rows"] == 1 and result["duplicate_rows"] == 1
    assert result["task_ids"] == [task_id]
    assert len(result["items"]) == 1
    assert result["items"][0]["organization_id"] and result["items"][0]["intake_id"]
    with database.read() as connection:
        assert connection.execute("SELECT batch_id FROM community_task_import_links "
                                  "WHERE task_id=?", (task_id,)).fetchone()[0] == result["batch_id"]
        evidence = connection.execute("SELECT excerpt FROM source_observations WHERE "
                                      "source_type='community:directory'").fetchone()[0]
        assert '"source_row_id":"row-1"' in evidence
        assert '"domain":"website link"' in evidence
    assert community.finish(task_id=task_id, worker_id="codex-6E", outcome="review",
                            reason="source-backed batch linked") == "needs_review"


def test_import_obeys_community_pause(foundation):
    _, database, pause, *_ = foundation
    indexer = ProspectListIndexer(database, pause=pause)
    pause.pause(actor="operator", reason="hold imports", lane="community")
    with pytest.raises(PausedError, match="paused"):
        make_import(indexer, [row()])
    assert indexer.report() == {"batches": 0, "rows": 0, "states": {}}
