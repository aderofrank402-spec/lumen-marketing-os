from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from lumen_marketing_os.adapters.research import SourceItem
from lumen_marketing_os.config import Settings
from lumen_marketing_os.workflows.research.agents import DepartmentAgents, ResearchConfig
from lumen_marketing_os.workflows.research.runtime import ResearchRuntime
from lumen_marketing_os.workflows.research.store import SourceResult


class FakeAgents:
    def __init__(self):
        self.config = ResearchConfig((), (), (), (), (), ("company data",), 10)

    def collect(self, agent_id):
        if agent_id == "1A":
            return [SourceResult("1A:test", "rss", (
                SourceItem("1A:test", "rss", "a1", "https://example.com/a",
                           "Company data is stale", "A buyer describes stale company data."),))]
        if agent_id == "1B":
            return [SourceResult("1B:reddit:SaaS", "reddit_public", (
                SourceItem("1B:reddit:SaaS", "reddit_public", "r1",
                           "https://www.reddit.com/r/SaaS/comments/x/", "Company data gap",
                           "Found inaccurate company data"),))]
        if agent_id == "1C":
            return [SourceResult("1C:competitor", "competitor_page", (
                SourceItem("1C:competitor", "competitor_page", None,
                           "https://competitor.example/pricing", "Competitor pricing",
                           "Plan A costs $10"),))]
        return [SourceResult("1E:hn-show", "hacker_news", (
            SourceItem("1E:hn-show", "hacker_news", "h1",
                       "https://news.ycombinator.com/item?id=1", "Show HN: Acme - Company data startup",
                       "Launch", subject_url="https://acme.example"),))]

    def draft(self, agent_id, item, *, previous):
        # Use the real deterministic role logic, not a test-only classification.
        return DepartmentAgents.draft(self, agent_id, item, previous=previous)


def make_runtime(foundation, tmp_path, agents=None):
    clock, db, pause, queue, outbox = foundation
    (tmp_path / "config").mkdir(exist_ok=True)
    source = Path(__file__).parents[1] / "config" / "research.json"
    shutil.copyfile(source, tmp_path / "config" / "research.json")
    settings = Settings(tmp_path, db.path, tmp_path / "PAUSE_ALL.lock",
                        "development", True, False, 5000, 300, 3, 30, 300)
    return ResearchRuntime(settings=settings, database=db, pause=pause, queue=queue,
                           agents=agents or FakeAgents(), clock=clock)


def test_all_four_agents_persist_findings_and_handoffs(foundation, tmp_path):
    clock, db, pause, queue, outbox = foundation
    runtime = make_runtime(foundation, tmp_path)
    assert len(runtime.tick()) == 4
    assert runtime.tick() == []
    results = [runtime.work_once() for _ in range(4)]
    assert {r["agent_id"] for r in results} == {"1A", "1B", "1C", "1E"}
    assert all(r["findings_new"] == 1 for r in results)
    assert runtime.work_once() is None
    with db.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM research_findings").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 4
        destinations = {r[0] for r in connection.execute("SELECT destination FROM outbox_events")}
        assert destinations == {"strategy.review", "community.review", "research.identity_review"}
    # A new scheduled run of unchanged sources must not duplicate evidence or findings.
    clock.advance(24 * 3600 + 1)
    runtime.tick()
    reruns = [runtime.work_once() for _ in range(4)]
    assert all(r["findings_new"] == 0 for r in reruns)
    company_finding = next(f for f in runtime.report()["findings"] if f["agent_id"] == "1E")
    detail = runtime.finding(company_finding["id"])
    assert detail["evidence_url"].startswith("https://")
    assert detail["content_hash"]
    with pytest.raises(ValueError, match="specialist-reviewed"):
        runtime.promote_organization(finding_id=company_finding["id"],
                                     name="Acme", domain="acme.example", reviewer="human")
    runtime.review(finding_id=company_finding["id"], reviewer="human", decision="reviewed")
    organization_id = runtime.promote_organization(finding_id=company_finding["id"],
                                                    name="Acme", domain="acme.example",
                                                    reviewer="human")
    assert organization_id
    with db.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 5
    finding_id = next(f["id"] for f in runtime.report()["findings"] if f["agent_id"] != "1E")
    runtime.review(finding_id=finding_id, reviewer="human", decision="reviewed")
    assert next(f for f in runtime.report()["findings"] if f["id"] == finding_id)["status"] == "reviewed"


def test_competitor_change_and_partial_source_health(foundation, tmp_path):
    clock, db, pause, queue, outbox = foundation
    agents = FakeAgents()
    runtime = make_runtime(foundation, tmp_path, agents)
    runtime.tick()
    for _ in range(4):
        runtime.work_once()
    original_collect = agents.collect
    def changed(agent_id):
        if agent_id == "1C":
            return [SourceResult("1C:competitor", "competitor_page", (
                SourceItem("1C:competitor", "competitor_page", None,
                           "https://competitor.example/pricing", "Competitor pricing",
                           "Plan A costs $20"),)),
                    SourceResult("1C:blocked", "competitor_page", error_class="access_denied")]
        return original_collect(agent_id)
    agents.collect = changed
    clock.advance(24 * 3600 + 1)
    runtime.tick()
    results = [runtime.work_once() for _ in range(4)]
    competitor = next(r for r in results if r["agent_id"] == "1C")
    assert competitor["status"] == "partial"
    assert competitor["findings_new"] == 1
    report = runtime.report()
    assert any(s["source_key"] == "1C:blocked" and s["status"] == "blocked"
               for s in report["source_health"])
    assert any(f["finding_type"] == "competitor_change" for f in report["findings"])


def test_pause_skips_agent_lane(foundation, tmp_path):
    clock, db, pause, queue, outbox = foundation
    runtime = make_runtime(foundation, tmp_path)
    runtime.tick()
    pause.pause(actor="human", reason="test", lane="research.1A")
    assert runtime.work_once()["agent_id"] == "1B"


def test_all_sources_failure_retries_and_preserves_health(foundation, tmp_path):
    clock, db, pause, queue, outbox = foundation
    agents = FakeAgents()
    agents.collect = lambda agent_id: [SourceResult(f"{agent_id}:down", "rss",
                                                    error_class="network_error")]
    runtime = make_runtime(foundation, tmp_path, agents)
    runtime.tick()
    result = runtime.work_once()
    assert result["status"] == "retry_wait"
    assert queue.get(result["job_id"]).state == "retry_wait"
    assert runtime.report()["source_health"][0]["status"] == "degraded"


def test_directory_article_is_signal_not_company(foundation, tmp_path):
    runtime = make_runtime(foundation, tmp_path)
    article = SourceItem("1E:techcrunch", "launch_feed", None,
                         "https://techcrunch.com/article", "Five VCs judge startups",
                         "A news story about an event")
    assert runtime.agents.draft("1E", article, previous=None) is None
    relevant_article = SourceItem("1E:techcrunch", "launch_feed", None,
                                  "https://techcrunch.com/relevant", "Company data startup raises funding",
                                  "A news story about a product")
    draft = runtime.agents.draft("1E", relevant_article, previous=None)
    assert draft.kind == "launch_signal"
    assert draft.subject_name is None
    api_listing = SourceItem("1E:producthunt", "directory_search", None,
                             "https://www.producthunt.com/products/companies-api",
                             "The Companies API", "Search 54 million companies")
    assert runtime.agents.draft("1E", api_listing, previous=None) is not None


def test_legacy_directory_screen_is_reversible_and_not_a_specialist_review(foundation, tmp_path):
    clock, db, pause, queue, outbox = foundation
    runtime = make_runtime(foundation, tmp_path)
    runtime.tick()
    for _ in range(4):
        runtime.work_once()
    with db.transaction() as c:
        finding = c.execute("SELECT id FROM research_findings WHERE agent_id='1E'").fetchone()[0]
        c.execute("UPDATE research_findings SET title='Show HN: A photo timer',"
                  "evidence_excerpt='A tool for darkroom printing' WHERE id=?", (finding,))
    screened = runtime.screen_legacy_directory(actor="operator")
    assert len(screened) == 1 and screened[0]["finding_id"] == finding
    assert runtime.screen_legacy_directory(actor="operator") == []
    assert runtime.dispatch_reviews(agent_id="1E", limit=10) == []
    assert runtime.review_report()["screened_out"] == 1
    with db.read() as c:
        assert c.execute("SELECT status FROM research_findings WHERE id=?", (finding,)).fetchone()[0] == "needs_codex_review"
        assert c.execute("SELECT state,last_error_class FROM outbox_events WHERE aggregate_id=?",
                         (finding,)).fetchone()[:] == ("needs_review", "intake_screened_out")
        assert c.execute("SELECT COUNT(*) FROM research_review_results").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    runtime.restore_screened_finding(finding_id=finding, actor="operator",
                                     reason="The source has newly verified relevance")
    assert runtime.screen_legacy_directory(actor="operator") == []
    assert len(runtime.dispatch_reviews(agent_id="1E", limit=10)) == 1
    with db.read() as c:
        assert c.execute("SELECT state FROM outbox_events WHERE aggregate_id=?",
                         (finding,)).fetchone()[0] == "pending"
