from __future__ import annotations

from pathlib import Path

import pytest

from lumen_marketing_os.core.errors import PausedError
from lumen_marketing_os.workflows.community.runtime import CommunityRuntime
from test_research_runtime import make_runtime


GUIDES = Path(__file__).parents[1] / "DEPARTMENT_6_AGENT_GUIDES"
URL = "https://www.reddit.com/r/Data_Enrichment/comments/abc123/company_data_issue/"
ANALYSIS = {
    "post_date": "2026-10-03",
    "problem": "The writer reports incorrect company-domain matches in enrichment.",
    "business_workflow": "CRM enrichment",
    "workaround_or_consequence": "Manual verification; exact cost unknown",
    "buyer_evidence": "unknown",
    "urgency_or_buying_signal": "none",
    "fit_rationale": "Company identity is relevant, product fit unverified",
    "counter_evidence": "Could be a one-off vendor-specific failure",
    "confidence": "medium",
    "confidence_reason": "First-hand problem but no independently identified buyer",
    "next_step": "investigate_use_case",
}


def test_authenticated_6a_observation_reaches_1b_review_once(foundation, tmp_path):
    clock, db, pause, *_ = foundation
    community = CommunityRuntime(db, pause, GUIDES, clock=clock)
    community.connection(agent_id="6A", platform="reddit", state="ready",
                         account_reference="u/LengthinessAble277", actor="test", verified=True)
    task_id = community.trigger(agent_id="6A", trigger_key="research:abc123",
                                input_payload={"thread_url": URL,
                                               "account_reference": "u/LengthinessAble277"})
    assert community.claim(agent_id="6A", worker_id="codex-6A")["task_id"] == task_id
    observation_id = community.observe(
        task_id=task_id, worker_id="codex-6A", platform="reddit", canonical_url=URL,
        title="Company data issue", excerpt="The enrichment vendor matched the wrong domain.",
        observed_at=clock(), relevance_note="First-hand company matching problem",
        external_id="abc123")
    research = make_runtime(foundation, tmp_path)
    result = research.import_reddit_observation(task_id=task_id, observation_id=observation_id,
                                                worker_id="codex-6A", analysis=ANALYSIS)
    again = research.import_reddit_observation(task_id=task_id, observation_id=observation_id,
                                               worker_id="codex-6A", analysis=ANALYSIS)
    assert result["created"] is True and again["created"] is False
    assert result["finding_id"] == again["finding_id"]
    with pytest.raises(ValueError, match="different analysis"):
        research.import_reddit_observation(task_id=task_id, observation_id=observation_id,
            worker_id="codex-6A", analysis=ANALYSIS | {"confidence": "low"})
    finding = research.finding(result["finding_id"])
    assert finding["metadata"]["community_observation_id"] == observation_id
    assert finding["status"] == "needs_codex_review"
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM research_findings WHERE agent_id='1B'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM outbox_events WHERE destination='strategy.review'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    research.review(finding_id=result["finding_id"], reviewer="research-reviewer",
                    decision="reviewed")
    assert research.finding(result["finding_id"])["status"] == "reviewed"
    assert community.finish(task_id=task_id, worker_id="codex-6A", outcome="no_match",
                            reason="No helpful public reply; research handoff saved") == "completed"


def test_reddit_handoff_rejects_wrong_task_account_and_pause(foundation, tmp_path):
    clock, db, pause, *_ = foundation
    community = CommunityRuntime(db, pause, GUIDES, clock=clock)
    community.connection(agent_id="6A", platform="reddit", state="ready",
                         account_reference="u/LengthinessAble277", actor="test", verified=True)
    task_id = community.trigger(agent_id="6A", trigger_key="research:wrong",
                                input_payload={"thread_url": URL})
    community.claim(agent_id="6A", worker_id="codex-6A")
    observation_id = community.observe(task_id=task_id, worker_id="codex-6A",
        platform="reddit", canonical_url=URL, title="Issue", excerpt="Wrong domains",
        observed_at=clock(), relevance_note="Possible issue")
    research = make_runtime(foundation, tmp_path)
    with pytest.raises(ValueError, match="account"):
        research.import_reddit_observation(task_id=task_id, observation_id=observation_id,
                                           worker_id="codex-6A", analysis=ANALYSIS)
    with db.read() as c:
        c.execute("UPDATE community_tasks SET input_json=? WHERE id=?",
                  ('{"account_reference":"u/LengthinessAble277"}', task_id))
        c.commit()
    pause.pause(actor="operator", reason="research stop", lane="research.1B")
    with pytest.raises(PausedError):
        research.import_reddit_observation(task_id=task_id, observation_id=observation_id,
                                           worker_id="codex-6A", analysis=ANALYSIS)
