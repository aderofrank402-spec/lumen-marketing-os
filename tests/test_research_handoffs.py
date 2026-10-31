from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from lumen_marketing_os.core.errors import PausedError
from lumen_marketing_os.core.time import utc_text
from lumen_marketing_os.workflows.community.runtime import CommunityRuntime
from lumen_marketing_os.workflows.qualification.runtime import QualificationRuntime
from lumen_marketing_os.workflows.research.handoff import ResearchHandoffRuntime
from test_research_runtime import make_runtime


def _seed(foundation, tmp_path):
    clock, db, pause, queue, outbox = foundation
    research = make_runtime(foundation, tmp_path)
    assert len(research.tick()) == 4
    for _ in range(4):
        assert research.work_once()["status"] == "succeeded"
    guide_root = tmp_path / "DEPARTMENT_6_AGENT_GUIDES"
    guide_root.mkdir()
    source = Path(__file__).parents[1] / "DEPARTMENT_6_AGENT_GUIDES"
    for name in ("6A_REDDIT.md", "6B_HACKER_NEWS.md"):
        shutil.copyfile(source / name, guide_root / name)
    handoff = ResearchHandoffRuntime(db, pause, guide_root, clock=clock)
    findings = {f["agent_id"]: f for f in research.report()["findings"]}
    return research, handoff, findings


def _mark_fresh(db, finding_id, clock):
    with db.transaction() as c:
        row = c.execute("SELECT metadata_json FROM research_findings WHERE id=?", (finding_id,)).fetchone()
        metadata = json.loads(row["metadata_json"])
        metadata["published_at"] = utc_text(clock())
        c.execute("UPDATE research_findings SET metadata_json=? WHERE id=?",
                  (json.dumps(metadata, sort_keys=True), finding_id))


def test_review_gated_routes_and_qualification_followthrough(foundation, tmp_path):
    clock, db, pause, queue, outbox = foundation
    research, handoff, findings = _seed(foundation, tmp_path)
    assert handoff.run(limit=10) == []  # raw findings are never routed
    community = CommunityRuntime(db, pause, tmp_path / "DEPARTMENT_6_AGENT_GUIDES", clock=clock)
    community.connection(agent_id="6A", platform="reddit", state="ready",
                         account_reference="u/test-owner", verified=True, actor="operator")
    _mark_fresh(db, findings["1B"]["id"], clock)
    for agent in ("1A", "1B", "1E"):
        research.review(finding_id=findings[agent]["id"], reviewer="specialist", decision="reviewed")
    research.review(finding_id=findings["1C"]["id"], reviewer="specialist", decision="rejected")
    results = handoff.run(limit=10)
    assert len(results) == 4
    by_destination = {r["destination"]: r for r in results if r["disposition"] != "rejected"}
    assert by_destination["strategy.review"]["target_type"] == "vertical_task"
    assert by_destination["community.review"]["target_type"] == "community_task"
    assert by_destination["research.identity_review"]["target_type"] == "research_handoff_intake"
    assert {r["disposition"] for r in results} == {"routed", "manual_review", "rejected"}
    assert handoff.run(limit=10) == []
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM research_handoff_receipts").fetchone()[0] == 4
        assert c.execute("SELECT COUNT(*) FROM outbox_events WHERE state='delivered'").fetchone()[0] == 4
        assert c.execute("SELECT COUNT(*) FROM vertical_tasks WHERE agent_id='8A'").fetchone()[0] == 1
        task = c.execute("SELECT agent_id,input_json FROM community_tasks").fetchone()
        assert task["agent_id"] == "6A"
        assert json.loads(task["input_json"])["account_reference"] == "u/test-owner"
        assert c.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    organization_id = research.promote_organization(
        finding_id=findings["1E"]["id"], name="Acme", domain="acme.example",
        reviewer="identity-reviewer")
    shutil.copyfile(Path(__file__).parents[1] / "config" / "qualification.json",
                    tmp_path / "config" / "qualification.json")
    qualification = QualificationRuntime(settings=research.settings, database=db, pause=pause,
                                         queue=queue, outbox=outbox, clock=clock)
    result = qualification.consume_discovery_once()
    assert result["organization_id"] == organization_id
    with db.read() as c:
        assert c.execute("SELECT state FROM research_handoff_intakes WHERE finding_id=?",
                         (findings["1E"]["id"],)).fetchone()[0] == "promoted"
        assert c.execute("SELECT state FROM qualification_intakes WHERE finding_id=?",
                         (findings["1E"]["id"],)).fetchone()[0] == "needs_review"


def test_missing_connection_creates_manual_intake_and_pause_blocks_delivery(foundation, tmp_path):
    clock, db, pause, _, _ = foundation
    research, handoff, findings = _seed(foundation, tmp_path)
    _mark_fresh(db, findings["1B"]["id"], clock)
    research.review(finding_id=findings["1B"]["id"], reviewer="specialist", decision="reviewed")
    pause.pause(actor="operator", reason="stop handoff", lane="research.handoff")
    with pytest.raises(PausedError):
        handoff.run()
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM research_handoff_receipts").fetchone()[0] == 0
    pause.resume(actor="operator", reason="resume", lane="research.handoff")
    result = handoff.work_once()
    assert result["disposition"] == "manual_review"
    assert result["target_type"] == "research_handoff_intake"
    handoff.dismiss_intake(intake_id=result["target_id"], reviewer="operator",
                           reason="No authenticated community connection is available")
    with db.read() as c:
        assert c.execute("SELECT state FROM research_handoff_intakes WHERE id=?",
                         (result["target_id"],)).fetchone()[0] == "dismissed"


def test_manual_community_handoff_can_resume_after_connection(foundation, tmp_path):
    clock, db, pause, _, _ = foundation
    research, handoff, findings = _seed(foundation, tmp_path)
    _mark_fresh(db, findings["1B"]["id"], clock)
    research.review(finding_id=findings["1B"]["id"], reviewer="specialist", decision="reviewed")
    result = handoff.work_once()
    assert result["disposition"] == "manual_review"
    community = CommunityRuntime(db, pause, tmp_path / "DEPARTMENT_6_AGENT_GUIDES", clock=clock)
    community.connection(agent_id="6A", platform="reddit", state="ready",
                         account_reference="u/test-owner", verified=True, actor="operator")
    resumed = handoff.retry_community_intake(intake_id=result["target_id"], actor="operator")
    assert resumed["disposition"] == "routed"
    assert resumed["target_type"] == "community_task"
    with db.read() as c:
        assert c.execute("SELECT state,target_id FROM research_handoff_intakes WHERE id=?",
                         (result["target_id"],)).fetchone()[:] == ("routed", resumed["target_id"])
        assert c.execute("SELECT COUNT(*) FROM community_tasks").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


def test_undated_forum_post_never_becomes_automatic_task(foundation, tmp_path):
    clock, db, pause, _, _ = foundation
    research, handoff, findings = _seed(foundation, tmp_path)
    CommunityRuntime(db, pause, tmp_path / "DEPARTMENT_6_AGENT_GUIDES", clock=clock).connection(
        agent_id="6A", platform="reddit", state="ready",
        account_reference="u/test-owner", verified=True, actor="operator")
    research.review(finding_id=findings["1B"]["id"], reviewer="specialist", decision="reviewed")
    result = handoff.work_once()
    assert result["disposition"] == "manual_review"
    assert result["target_type"] == "research_handoff_intake"
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM community_tasks").fetchone()[0] == 0


def test_reconcile_previous_undated_task_is_atomic_and_idempotent(foundation, tmp_path):
    clock, db, pause, _, _ = foundation
    research, handoff, findings = _seed(foundation, tmp_path)
    CommunityRuntime(db, pause, tmp_path / "DEPARTMENT_6_AGENT_GUIDES", clock=clock).connection(
        agent_id="6A", platform="reddit", state="ready",
        account_reference="u/test-owner", verified=True, actor="operator")
    finding_id = findings["1B"]["id"]
    _mark_fresh(db, finding_id, clock)
    research.review(finding_id=finding_id, reviewer="specialist", decision="reviewed")
    routed = handoff.work_once()
    assert routed["disposition"] == "routed"
    with db.transaction() as c:
        c.execute("UPDATE research_findings SET metadata_json='{}' WHERE id=?", (finding_id,))
    changed = handoff.reconcile_undated_community_tasks()
    assert len(changed) == 1
    assert changed[0]["task_id"] == routed["target_id"]
    assert handoff.reconcile_undated_community_tasks() == []
    with db.read() as c:
        assert c.execute("SELECT state,last_error_class FROM community_tasks WHERE id=?",
                         (routed["target_id"],)).fetchone()[:] == (
                             "blocked", "source_date_unverified")
        assert c.execute("SELECT disposition,target_type,target_id FROM research_handoff_receipts "
                         "WHERE id=?", (routed["receipt_id"],)).fetchone()[:] == (
                             "manual_review", "research_handoff_intake", changed[0]["intake_id"])
        assert c.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


def test_invalid_envelope_quarantines_without_downstream_task(foundation, tmp_path):
    _, db, _, _, _ = foundation
    research, handoff, findings = _seed(foundation, tmp_path)
    research.review(finding_id=findings["1A"]["id"], reviewer="specialist", decision="reviewed")
    with db.transaction() as c:
        c.execute("UPDATE outbox_events SET payload_json=? WHERE aggregate_id=?",
                  ('{"finding_id":"wrong"}', findings["1A"]["id"]))
    result = handoff.work_once()
    assert result["disposition"] == "quarantined"
    with db.read() as c:
        assert c.execute("SELECT state,last_error_class FROM outbox_events WHERE aggregate_id=?",
                         (findings["1A"]["id"],)).fetchone()[:] == (
                             "needs_review", "invalid_research_handoff")
        assert c.execute("SELECT COUNT(*) FROM vertical_tasks").fetchone()[0] == 0


def test_crash_before_commit_leaves_event_retryable(foundation, tmp_path, monkeypatch):
    _, db, _, _, _ = foundation
    research, handoff, findings = _seed(foundation, tmp_path)
    research.review(finding_id=findings["1A"]["id"], reviewer="specialist", decision="reviewed")
    original = handoff._strategy_task
    def crash(*_args):
        raise RuntimeError("simulated process failure")
    monkeypatch.setattr(handoff, "_strategy_task", crash)
    with pytest.raises(RuntimeError, match="simulated"):
        handoff.work_once()
    with db.read() as c:
        assert c.execute("SELECT state FROM outbox_events WHERE aggregate_id=?",
                         (findings["1A"]["id"],)).fetchone()[0] == "pending"
        assert c.execute("SELECT COUNT(*) FROM research_handoff_receipts").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM vertical_tasks").fetchone()[0] == 0
    monkeypatch.setattr(handoff, "_strategy_task", original)
    assert handoff.work_once()["disposition"] == "routed"
