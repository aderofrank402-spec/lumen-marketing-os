from __future__ import annotations

import pytest

from lumen_marketing_os.workflows.discovery.runtime import DiscoveryRuntime


def test_department8_validates_and_dispatches_once(foundation):
    _, db, pause, _, _ = foundation
    runtime = DiscoveryRuntime(db, pause)
    first = runtime.scout(vertical="Revenue operations", problem="Sales teams spend hours correcting stale company records",
                          buyer="Revenue operations lead", source_url="https://forum.example.com/pain",
                          excerpt="A sales operations team described repeated manual company record corrections.")
    oid = first["opportunity_id"]
    assert runtime.scout(vertical="Revenue operations", problem="Sales teams spend hours correcting stale company records",
                         buyer="Revenue operations lead", source_url="https://forum.example.com/pain",
                         excerpt="A sales operations team described repeated manual company record corrections.")["opportunity_id"] == oid
    with pytest.raises(ValueError):
        runtime.validate(opportunity_id=oid, buyer_workflow="The buyer cleans incoming account lists each morning.",
                         product_fit="Fresh company records could reduce manual reconciliation work.",
                         limitations="This source does not prove willingness to pay or purchase authority.", reviewer="owner")
    runtime.evidence(opportunity_id=oid, polarity="support", source_url="https://news.example.net/story",
                     excerpt="Another team described the same account data freshness problem.")
    runtime.evidence(opportunity_id=oid, polarity="counter", source_url="https://research.example.org/report",
                     excerpt="Some teams report that internal cleanup is sufficient for their needs.")
    runtime.validate(opportunity_id=oid, buyer_workflow="The buyer cleans incoming account lists each morning.",
                     product_fit="Fresh company records could reduce manual reconciliation work.",
                     limitations="This source does not prove willingness to pay or purchase authority.", reviewer="owner")
    result = runtime.candidate(opportunity_id=oid, company="Acme Analytics", domain="acme.example",
                               country="KE", source_url="https://acme.example/about",
                               pain_url="https://acme.example/blog/data-quality",
                               pain_note="The company describes manual verification of business account records.")
    cid = result["candidate_id"]
    runtime.review_candidate(candidate_id=cid, accept=True, reviewer="owner")
    dispatched = runtime.dispatch(opportunity_id=oid, approver="owner")
    assert dispatched["qualification_intakes"] == 1
    assert runtime.dispatch(opportunity_id=oid, approver="owner")["qualification_intakes"] == 0
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM qualification_intakes").fetchone()[0] == 1
        assert c.execute("SELECT status FROM use_cases WHERE id=?", (dispatched["use_case_id"],)).fetchone()[0] == "approved"


def test_cycle_replay_and_weak_identity_rejected(foundation):
    _, db, pause, _, _ = foundation
    runtime = DiscoveryRuntime(db, pause)
    result = runtime.cycle(cycle_key="2026-10-03-0")
    assert runtime.cycle(cycle_key="2026-10-03-0")["cycle_id"] == result["cycle_id"]
    with pytest.raises(ValueError):
        runtime.scout(vertical="Sales", problem="Short", buyer="CEO",
                      source_url="http://invalid.example/pain", excerpt="Short")


def test_scoped_tasks_and_identity_conflict(foundation):
    _, db, pause, _, _ = foundation
    runtime = DiscoveryRuntime(db, pause)
    oid = runtime.scout(vertical="Account operations", problem="Teams repeatedly reconcile stale organization details by hand",
                        buyer="Operations manager", source_url="https://forum.example.com/records",
                        excerpt="The operations team describes recurring manual review of account details.")["opportunity_id"]
    runtime.evidence(opportunity_id=oid, polarity="support", source_url="https://other.example.net/records",
                     excerpt="Another organization describes costly duplicate company record checks.")
    runtime.evidence(opportunity_id=oid, polarity="counter", source_url="https://report.example.org/records",
                     excerpt="A report says some small teams are satisfied with manual review.")
    runtime.validate(opportunity_id=oid, buyer_workflow="Operations staff reconcile account records daily by hand.",
                     product_fit="Fresh company details may reduce reconciliation effort.",
                     limitations="The evidence does not establish purchasing intent or budget.", reviewer="owner")
    with db.transaction() as c:
        c.execute("INSERT INTO organizations(id,display_name,normalized_name,country_code,canonical_domain,identity_status,created_at_utc,updated_at_utc) "
                  "VALUES ('existing','Other Business','other business','KE','acme.example','candidate','2026-10-03','2026-10-03')")
    with pytest.raises(ValueError, match="ambiguous"):
        runtime.candidate(opportunity_id=oid, company="Acme Analytics", domain="acme.example", country="KE",
                          source_url="https://acme.example/about", pain_url="https://acme.example/pain",
                          pain_note="The organization explicitly describes stale account data problems.")
    cycle = runtime.cycle(cycle_key="2026-10-03-1")
    task = runtime.report()["pending_tasks"][0]
    assert task["agent_id"] == "8C"
    assert runtime.finish_task(task_id=task["id"], outcome="blocked", reason="No unambiguous organization identity", actor="8C")["outcome"] == "blocked"
    with pytest.raises(ValueError, match="pending task"):
        runtime.finish_task(task_id=task["id"], outcome="completed", reason="Already finished task", actor="8C")
