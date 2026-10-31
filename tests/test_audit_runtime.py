from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import pytest

from lumen_marketing_os.core.errors import InvalidStateError
from lumen_marketing_os.core.time import utc_text
from lumen_marketing_os.workflows.audit.runtime import AuditRuntime
from lumen_marketing_os.workflows.content.model import content_hash
from lumen_marketing_os.workflows.outreach.runtime import OutreachRuntime


def runtime(foundation, *, key=b"x" * 32):
    clock, db, pause, _, _ = foundation
    rt = AuditRuntime(db, pause, suppression_key=key, clock=clock)
    rt.ensure_default_policy(actor="test")
    return rt


def draft(foundation, *, draft_id="d1", body="A sourced note", evidence=None):
    clock, db, _, _, _ = foundation
    digest = content_hash("Hello", body)
    with db.transaction() as c:
        c.execute("INSERT INTO drafts(id,channel,subject,body,content_hash,version,evidence_json,"
                  "author_role,status,created_at_utc) VALUES (?,'email','Hello',?,?,1,?,'3A',"
                  "'review_passed',?)", (draft_id, body, digest,
                  json.dumps(evidence if evidence is not None else {"source": "fixture"}),
                  utc_text(clock())))
    return draft_id, digest


def test_7a_exact_hash_guard_pass_block_unavailable_and_idempotency(foundation):
    rt = runtime(foundation)
    clean_id, digest = draft(foundation)
    passed = rt.guard_draft(draft_id=clean_id)
    assert passed["decision"] == "passed"
    assert rt.guard_draft(draft_id=clean_id)["id"] == passed["id"]
    with foundation[1].read() as c:
        row = c.execute("SELECT reviewer_role,decision,content_hash FROM content_reviews "
                        "WHERE draft_id=? AND reviewer_role='7A'", (clean_id,)).fetchone()
        assert tuple(row) == ("7A", "passed", digest)

    blocked_id, _ = draft(foundation, draft_id="d2", body="Guaranteed and 100% accurate")
    blocked = rt.guard_draft(draft_id=blocked_id)
    assert blocked["decision"] == "blocked"
    assert {f["key"] for f in blocked["findings"]} >= {"guaranteed_result", "absolute_accuracy"}

    unavailable_id, _ = draft(foundation, draft_id="d3")
    unavailable = rt.guard_draft(draft_id=unavailable_id, checker_available=False)
    assert unavailable["decision"] == "blocked"
    assert unavailable["checker_state"] == "unavailable"


def test_7a_requires_evidence_but_warning_is_review_not_false_pass(foundation):
    rt = runtime(foundation)
    missing_id, _ = draft(foundation, evidence={})
    assert rt.guard_draft(draft_id=missing_id)["decision"] == "blocked"
    warning_id, _ = draft(foundation, draft_id="d-warning", body="This is your last chance to review it.")
    result = rt.guard_draft(draft_id=warning_id)
    assert result["decision"] == "needs_review"
    with foundation[1].read() as c:
        assert c.execute("SELECT COUNT(*) FROM content_reviews WHERE draft_id=? AND reviewer_role='7A'",
                         (warning_id,)).fetchone()[0] == 0


def test_7b_health_is_durable_and_explicit(foundation):
    rt = runtime(foundation)
    health_id = rt.record_provider_health(channel="email", account_id="acct", provider="gmail",
                                          status="degraded", reason="bounce signal elevated",
                                          metrics={"bounce_rate": 0.03}, actor="7B")
    report = rt.rate_health_report()
    assert report["provider_health"][0]["id"] == health_id
    assert report["provider_health"][0]["metrics"]["bounce_rate"] == 0.03
    assert report["provider_health"][0]["expired"] is False


def test_7c_detects_merge_cycle_without_modifying_data(foundation):
    rt = runtime(foundation)
    now = utc_text(foundation[0]())
    with foundation[1].transaction() as c:
        c.execute("INSERT INTO organizations(id,display_name,normalized_name,identity_status,"
                  "created_at_utc,updated_at_utc) VALUES ('cycle','Cycle','cycle','merged',?,?)",
                  (now, now))
        c.execute("UPDATE organizations SET merged_into_id='cycle' WHERE id='cycle'")
    result = rt.integrity_scan()
    assert result["status"] == "blocked"
    assert any(f["key"].startswith("organization_cycle") for f in result["findings"])
    with foundation[1].read() as c:
        assert c.execute("SELECT merged_into_id FROM organizations WHERE id='cycle'").fetchone()[0] == "cycle"


def test_7d_low_sample_report_is_idempotent_and_non_executing(foundation):
    rt = runtime(foundation)
    clock, db, _, _, _ = foundation
    now = utc_text(clock())
    with db.transaction() as c:
        c.execute("INSERT INTO actions(id,kind,target_id,channel,state,idempotency_key,policy_version,"
                  "created_at_utc,updated_at_utc) VALUES ('a','test','t','email','accepted','k','p',?,?)",
                  (now, now))
    start = utc_text(clock() - timedelta(hours=1))
    end = utc_text(clock() + timedelta(hours=1))
    one = rt.growth_audit(window_start_utc=start, window_end_utc=end)
    two = rt.growth_audit(window_start_utc=start, window_end_utc=end)
    assert one["report_id"] == two["report_id"]
    assert one["sample_warning"] is True
    assert one["recommendations"][0]["autonomous_change"] is False
    with db.read() as c:
        assert c.execute("SELECT state FROM actions WHERE id='a'").fetchone()[0] == "accepted"


def test_7e_detects_lag_and_unknown_effect_without_replay(foundation):
    rt = runtime(foundation)
    clock, db, _, queue, _ = foundation
    overdue = queue.enqueue(workflow="test", lane="research", payload={}, logical_key="late",
                            due_at=clock() - timedelta(hours=3))
    now = utc_text(clock())
    with db.transaction() as c:
        c.execute("INSERT INTO actions(id,kind,target_id,channel,state,idempotency_key,policy_version,"
                  "created_at_utc,updated_at_utc) VALUES ('unknown','test','t','email','unknown',"
                  "'unknown-k','p',?,?)", (now, now))
    result = rt.pipeline_scan(pending_lag_seconds=3600)
    keys = {f["key"] for f in result["findings"]}
    assert f"lagged_job:{overdue.id}" in keys
    assert "unknown_external_actions" in keys
    assert result["automatic_replays"] == 0
    assert queue.get(overdue.id).state == "pending"


def test_7f_keyed_suppression_check_and_fail_closed(foundation):
    clock, db, pause, _, _ = foundation
    outreach = OutreachRuntime(database=db, pause=pause, suppression_key=b"x" * 32, clock=clock)
    rule_id = outreach.suppress(subject_type="address", value="Buyer@Example.org",
                                reason="opt out", actor="operator")
    rt = runtime(foundation)
    result = rt.suppression_check(subject_type="address", value="buyer@example.org")
    assert result["suppressed"] is True
    assert result["rule_id"] == rule_id
    assert rt.suppression_check(subject_type="address", value="other@example.org")["suppressed"] is False
    with pytest.raises(InvalidStateError, match="fail closed"):
        AuditRuntime(db, pause, suppression_key=None, clock=clock).suppression_check(
            subject_type="address", value="other@example.org")


def test_full_scan_persists_role_runs_and_report(foundation):
    rt = runtime(foundation)
    result = rt.full_scan(actor="test")
    assert result["integrity"]["status"] == "passed"
    assert result["pipeline"]["status"] == "passed"
    report = rt.report()
    assert {r["role_id"] for r in report["runs"]} >= {"7C", "7E"}
