from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from lumen_marketing_os.core.errors import ConflictError, PausedError
from lumen_marketing_os.workflows.community.runtime import CommunityRuntime


GUIDES = Path(__file__).parents[1] / "DEPARTMENT_6_AGENT_GUIDES"


def runtime(foundation):
    clock, db, pause, *_ = foundation
    return CommunityRuntime(db, pause, GUIDES, clock=clock)


def test_due_trigger_is_durable_deduplicated_and_inactive_by_default(foundation):
    clock, db, *_ = foundation
    community = runtime(foundation)
    version = community.configure_trigger(
        trigger_id="reddit-four-hourly", agent_id="6A", trigger_kind="interval",
        owner="operator", input_payload={"scope": "approved communities"},
        interval_seconds=14400, next_due_at=clock())
    assert version and community.tick() == []
    community.set_trigger_active("reddit-four-hourly", active=True, actor="operator")
    created = community.tick()
    assert len(created) == 1 and created[0]["agent_id"] == "6A"
    assert community.tick() == []
    assert runtime(foundation).tick() == []
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM community_tasks").fetchone()[0] == 1
    with pytest.raises(ConflictError):
        community.configure_trigger(trigger_id="reddit-four-hourly", agent_id="6A",
                                    trigger_kind="interval", owner="operator",
                                    input_payload={"scope": "changed"},
                                    interval_seconds=14400, next_due_at=clock())


def test_linkedin_interval_handoff_is_due_once_and_review_gated(foundation):
    clock, db, *_ = foundation
    community = runtime(foundation)
    community.connection(agent_id="6D", platform="linkedin", state="ready",
                         account_reference="linkedin:intended", verified=True, actor="operator")
    community.configure_trigger(trigger_id="linkedin-daily", agent_id="6D",
                                trigger_kind="interval", owner="operator",
                                input_payload={"topic": "company data quality",
                                               "account_reference": "linkedin:intended"},
                                interval_seconds=86400, next_due_at=clock())
    community.set_trigger_active("linkedin-daily", active=True, actor="operator")
    created = community.tick()
    assert len(created) == 1 and created[0]["agent_id"] == "6D"
    assert community.tick() == []
    brief = community.claim(agent_id="6D", worker_id="codex-6D")
    assert brief["trigger_key"] and brief["input"]["topic"] == "company data quality"
    with pytest.raises(ValueError, match="needs a draft"):
        community.finish(task_id=brief["task_id"], worker_id="codex-6D",
                         outcome="review", reason="no draft")
    assert community.finish(task_id=brief["task_id"], worker_id="codex-6D",
                            outcome="no_match", reason="no relevant post") == "completed"


def test_connection_gate_observation_draft_and_review(foundation):
    clock, db, pause, *_ = foundation
    community = runtime(foundation)
    task_id = community.trigger(agent_id="6A", trigger_key="thread:42",
                                input_payload={"thread_url": "https://www.reddit.com/r/test/comments/42"})
    with pytest.raises(ValueError, match="connection"):
        community.claim(agent_id="6A", worker_id="codex-6A")
    with pytest.raises(ValueError):
        community.connection(agent_id="6A", platform="reddit", state="ready",
                             account_reference="lumen-reddit", actor="operator")
    community.connection(agent_id="6A", platform="reddit", state="ready",
                         account_reference="lumen-reddit", actor="operator", verified=True)
    brief = community.claim(agent_id="6A", worker_id="codex-6A")
    assert brief["task_id"] == task_id and Path(brief["guide_path"]).is_file()
    url = "https://www.reddit.com/r/test/comments/42"
    with pytest.raises(ValueError, match="platform"):
        community.observe(task_id=task_id, worker_id="codex-6A", platform="hacker_news",
                          canonical_url="https://news.ycombinator.com/item?id=42", title="Title",
                          excerpt="Question", observed_at=clock(), relevance_note="Relevant")
    observation_id = community.observe(task_id=task_id, worker_id="codex-6A",
                                       platform="reddit", canonical_url=url, title="Question",
                                       excerpt="How can I verify companies?", observed_at=clock(),
                                       relevance_note="Direct company data problem", external_id="42")
    with pytest.raises(ValueError, match="source observation"):
        community.draft(task_id=task_id, worker_id="codex-6A", artifact_type="helpful_reply",
                        body="Here is a practical approach.", evidence_urls=["https://example.com"] ,
                        observation_id=observation_id)
    draft_id = community.draft(task_id=task_id, worker_id="codex-6A",
                               artifact_type="helpful_reply", body="Here is a practical approach.",
                               evidence_urls=[url], observation_id=observation_id)
    detail = community.draft_detail(draft_id)
    assert detail["body"] == "Here is a practical approach."
    assert detail["evidence_urls"] == [url]
    assert community.finish(task_id=task_id, worker_id="codex-6A", outcome="review",
                            reason="sourced draft ready") == "needs_review"
    with db.read() as c:
        draft = c.execute("SELECT body_sha256,state FROM community_drafts WHERE id=?", (draft_id,)).fetchone()
        assert c.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    with pytest.raises(ValueError):
        community.review_draft(draft_id=draft_id, reviewer="editor", decision="approved",
                               note="checked", expected_hash="wrong")
    community.review_draft(draft_id=draft_id, reviewer="editor", decision="approved",
                           note="checked", expected_hash=draft["body_sha256"])
    with db.read() as c:
        assert c.execute("SELECT state FROM community_drafts WHERE id=?", (draft_id,)).fetchone()[0] == "approved"
        assert c.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


def test_launch_checklist_and_manual_dedup(foundation):
    clock, db, *_ = foundation
    community = runtime(foundation)
    payload = {"platform": "product_hunt", "launch_at": "2026-10-10T10:00:00Z"}
    task_id = community.trigger(agent_id="6C", trigger_key="launch:october", input_payload=payload)
    assert community.trigger(agent_id="6C", trigger_key="launch:october", input_payload=payload) == task_id
    with pytest.raises(ConflictError):
        community.trigger(agent_id="6C", trigger_key="launch:october",
                          input_payload={"platform": "product_hunt", "launch_at": "2026-10-11T10:00:00Z"})
    checklist = community.brief(task_id)["launch_checklist"]
    assert len(checklist) >= 7
    community.review_launch_item(task_id=task_id, item_key="positioning", state="reviewed",
                                 actor="operator", evidence_ref="approved positioning brief")
    assert any(item["item_key"] == "positioning" and item["state"] == "reviewed"
               for item in community.brief(task_id)["launch_checklist"])
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM community_tasks").fetchone()[0] == 1


def test_existing_product_hunt_listing_audit_needs_no_fictitious_launch_date(foundation):
    community = runtime(foundation)
    payload = {"mode": "listing_audit", "platform": "product_hunt",
               "source_url": "https://www.producthunt.com/products/lumen-sieve/launches/lumen-sieve"}
    task_id = community.trigger(agent_id="6C", trigger_key="listing-audit:lumen-sieve",
                                input_payload=payload)
    assert community.brief(task_id)["launch_checklist"] == []
    community.connection(agent_id="6C", platform="product_hunt", state="ready",
                         account_reference="@adero", verified=True, actor="test")
    assert community.claim(agent_id="6C", worker_id="codex-6C")["task_id"] == task_id
    with pytest.raises(ValueError, match="Product Hunt audit source"):
        community.trigger(agent_id="6C", trigger_key="wrong-host", input_payload={
            **payload, "source_url": "https://example.com/launch"})


def test_claim_rejects_account_switch(foundation):
    community = runtime(foundation)
    community.connection(agent_id="6A", platform="reddit", state="ready",
                         account_reference="u/intended", verified=True, actor="test")
    community.trigger(agent_id="6A", trigger_key="reddit:account-switch",
                      input_payload={"scope": "approved", "account_reference": "u/other"})
    with pytest.raises(ValueError, match="account reference differs"):
        community.claim(agent_id="6A", worker_id="codex-6A")


def test_lease_expiry_and_pause(foundation):
    clock, db, pause, *_ = foundation
    community = runtime(foundation)
    task_id = community.trigger(agent_id="6D", trigger_key="topic:a",
                                input_payload={"topic": "company data freshness"})
    community.connection(agent_id="6D", platform="linkedin", state="ready",
                         account_reference="lumen-linkedin", actor="operator", verified=True)
    assert community.claim(agent_id="6D", worker_id="first", lease_seconds=30)["task_id"] == task_id
    clock.advance(31)
    assert community.claim(agent_id="6D", worker_id="second")["lease_owner"] == "second"
    with pytest.raises(ValueError, match="lease"):
        community.finish(task_id=task_id, worker_id="first", outcome="no_match", reason="none")
    pause.pause(actor="operator", reason="test")
    with pytest.raises(PausedError):
        community.trigger(agent_id="6D", trigger_key="topic:b", input_payload={"topic": "other"})


def test_guide_change_blocks_claim(foundation, tmp_path):
    clock, db, pause, *_ = foundation
    temp_guides = tmp_path / "guides"
    temp_guides.mkdir()
    guide = temp_guides / "6A_REDDIT.md"
    guide.write_text("first version", encoding="utf-8")
    community = CommunityRuntime(db, pause, temp_guides, clock=clock)
    community.trigger(agent_id="6A", trigger_key="scope:a", input_payload={"scope": "test"})
    community.connection(agent_id="6A", platform="reddit", state="ready",
                         account_reference="test", actor="operator", verified=True)
    guide.write_text("changed version", encoding="utf-8")
    result = community.claim(agent_id="6A", worker_id="codex-6A")
    assert result["state"] == "blocked" and result["reason"] == "guide_changed"


def test_observation_versions_preserve_changes(foundation):
    clock, db, _, _, _ = foundation
    community = runtime(foundation)
    community.connection(agent_id="6B", platform="hacker_news", state="ready",
                         account_reference="hn", actor="operator", verified=True)
    task1 = community.trigger(agent_id="6B", trigger_key="hn:1", input_payload={"topic": "data"})
    community.claim(agent_id="6B", worker_id="codex-6B")
    url = "https://news.ycombinator.com/item?id=123"
    first_id = community.observe(task_id=task1, worker_id="codex-6B", platform="hacker_news",
                                 canonical_url=url, title="Question", excerpt="Version one",
                                 observed_at=clock(), relevance_note="Relevant")
    community.finish(task_id=task1, worker_id="codex-6B", outcome="no_match", reason="not suitable")
    task2 = community.trigger(agent_id="6B", trigger_key="hn:2", input_payload={"topic": "data"})
    community.claim(agent_id="6B", worker_id="codex-6B")
    second_id = community.observe(task_id=task2, worker_id="codex-6B", platform="hacker_news",
                                  canonical_url=url, title="Question", excerpt="Version two",
                                  observed_at=clock() + timedelta(minutes=1), relevance_note="Updated")
    assert first_id == second_id
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM community_observation_versions WHERE observation_id=?",
                         (first_id,)).fetchone()[0] == 2
