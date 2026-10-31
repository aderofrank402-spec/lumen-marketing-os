from __future__ import annotations

import pytest

from lumen_marketing_os.core.errors import LeaseLostError, PausedError
from lumen_marketing_os.core.time import utc_text
from test_research_runtime import make_runtime


def _seed(foundation, tmp_path):
    runtime = make_runtime(foundation, tmp_path)
    assert len(runtime.tick()) == 4
    for _ in range(4):
        assert runtime.work_once()["status"] == "succeeded"
    return runtime


def _assessment(finding, clock):
    return {"source_url": finding["evidence_url"],
            "source_checked_at_utc": utc_text(clock()),
            "source_status": "verified",
            "evidence_summary": "Original source was inspected; claim is limited to what it says.",
            "counter_evidence": "Buyer intent and product fit remain unverified.",
            "buyer_evidence": "unknown",
            "product_fit": "hypothesis only",
            "rationale": "Useful research signal, not a validated lead.",
            "confidence": "low", "recommendation": "retain for research review"}


def test_four_roles_dispatch_claim_complete_and_deduplicate(foundation, tmp_path):
    clock, db, pause, queue, _ = foundation
    runtime = _seed(foundation, tmp_path)
    assignments = runtime.dispatch_reviews(limit=20)
    assert {a["agent_id"] for a in assignments} == {"1A", "1B", "1C", "1E"}
    assert runtime.dispatch_reviews(limit=20) == []
    for agent_id in ("1A", "1B", "1C", "1E"):
        brief = runtime.claim_review(agent_id=agent_id, worker_id=f"codex-{agent_id}")
        assert brief["role_name"] and brief["finding"]["agent_id"] == agent_id
        result = runtime.complete_review(job_id=brief["job_id"],
            worker_id=f"codex-{agent_id}", reviewer=f"codex-{agent_id}",
            agent_run_ref=f"test-agent-thread-{agent_id}",
            decision="rejected" if agent_id == "1E" else "reviewed",
            assessment=_assessment(brief["finding"], clock))
        assert result["agent_id"] == agent_id
        assert runtime.claim_review(agent_id=agent_id, worker_id="other") is None
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM research_review_results").fetchone()[0] == 4
        assert c.execute("SELECT COUNT(*) FROM research_findings WHERE status='needs_codex_review'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 4
    assert len(runtime.review_report()["jobs"]) == 4


def test_dispatch_advances_through_backlog_without_reselecting_first_batch(foundation, tmp_path):
    runtime = _seed(foundation, tmp_path)
    first = runtime.dispatch_reviews(limit=2)
    second = runtime.dispatch_reviews(limit=2)
    assert len(first) == len(second) == 2
    assert {row["finding_id"] for row in first}.isdisjoint(
        {row["finding_id"] for row in second})
    assert runtime.dispatch_reviews(limit=2) == []


def test_wrong_source_worker_pause_and_retry_recovery(foundation, tmp_path):
    clock, db, pause, queue, _ = foundation
    runtime = _seed(foundation, tmp_path)
    runtime.dispatch_reviews(agent_id="1B", limit=1)
    brief = runtime.claim_review(agent_id="1B", worker_id="first", lease_seconds=60)
    assessment = _assessment(brief["finding"], clock)
    with pytest.raises(ValueError, match="source URL mismatched"):
        runtime.complete_review(job_id=brief["job_id"], worker_id="first",
            reviewer="reviewer", agent_run_ref="test-thread", decision="reviewed",
            assessment=assessment | {"source_url": "https://wrong.example"})
    with pytest.raises(LeaseLostError):
        runtime.complete_review(job_id=brief["job_id"], worker_id="wrong",
            reviewer="reviewer", agent_run_ref="test-thread", decision="reviewed",
            assessment=assessment)
    pause.pause(actor="operator", reason="stop", lane="research.1B")
    with pytest.raises(PausedError):
        runtime.complete_review(job_id=brief["job_id"], worker_id="first",
            reviewer="reviewer", agent_run_ref="test-thread", decision="reviewed",
            assessment=assessment)
    pause.resume(actor="operator", reason="resume", lane="research.1B")
    clock.advance(61)
    assert queue.recover_expired()["retried"] == 1
    recovered = runtime.claim_review(agent_id="1B", worker_id="second")
    assert recovered["job_id"] == brief["job_id"] and recovered["attempt_count"] == 2
    result = runtime.fail_review(job_id=brief["job_id"], worker_id="second",
                                 reason="source temporarily unavailable", retryable=True)
    assert result["state"] == "retry_wait"
    clock.advance(61)
    final = runtime.claim_review(agent_id="1B", worker_id="third")
    assert final["attempt_count"] == 3
    runtime.complete_review(job_id=final["job_id"], worker_id="third", reviewer="reviewer",
        agent_run_ref="test-thread", decision="reviewed",
        assessment=_assessment(final["finding"], clock))
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM research_review_results").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM job_attempts WHERE job_id=?",
                         (brief["job_id"],)).fetchone()[0] == 3


def test_directly_reviewed_finding_is_not_reprocessed(foundation, tmp_path):
    runtime = _seed(foundation, tmp_path)
    assignment = runtime.dispatch_reviews(agent_id="1A", limit=1)[0]
    runtime.review(finding_id=assignment["finding_id"], reviewer="human", decision="rejected")
    assert runtime.claim_review(agent_id="1A", worker_id="codex-1A") is None
    assert runtime.queue.get(assignment["job_id"]).state == "succeeded"


def test_verified_1e_review_can_still_promote_organization(foundation, tmp_path):
    clock, db, *_ = foundation
    runtime = _seed(foundation, tmp_path)
    runtime.dispatch_reviews(agent_id="1E", limit=1)
    brief = runtime.claim_review(agent_id="1E", worker_id="codex-1E")
    runtime.complete_review(job_id=brief["job_id"], worker_id="codex-1E",
        reviewer="codex-1E", agent_run_ref="test-agent-thread-1E", decision="reviewed",
        assessment=_assessment(brief["finding"], clock))
    organization_id = runtime.promote_organization(
        finding_id=brief["finding"]["id"], name="Acme", domain="acme.example",
        reviewer="identity-reviewer")
    assert organization_id
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM outbox_events WHERE destination='qualification.discovery'").fetchone()[0] == 1
