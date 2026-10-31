from __future__ import annotations

import json
from pathlib import Path

import pytest

from lumen_marketing_os.adapters.qualification import (LumenClient, ProviderError,
                                                         ProviderResponse)
from lumen_marketing_os.config import Settings
from lumen_marketing_os.workflows.qualification.runtime import QualificationRuntime
from lumen_marketing_os.workflows.content.runtime import ContentRuntime


NOW = "2026-09-30T12:00:00+00:00"


def profile(domain: str = "acme.example") -> dict:
    metadata = {"confidence": 0.95, "sources": [f"https://{domain}/about"],
                "last_verified": NOW}
    return {"business": {"trade_name": {"value": "Acme", "metadata": metadata}},
            "contact": {"website": {"value": f"https://{domain}", "metadata": metadata},
                        "emails": [{"address": f"info@{domain}", "purpose": "main",
                                    "metadata": metadata}]},
            "location": {"headquarters": {"country_code": "KE"}}}


class FakeLumen:
    def __init__(self, *, final_domain: str = "acme.example", immediate: bool = False):
        self.keys = []
        self.final_domain = final_domain
        self.immediate = immediate
        self.polls = 0

    def start(self, payload, request_key):
        self.keys.append(request_key)
        assert payload == {"domain": "acme.example", "mode": "full", "include_attributions": True}
        if self.immediate:
            return ProviderResponse(200, {"profile": profile(self.final_domain), "profileId": "profile-1"}, {})
        return ProviderResponse(202, {"status": "processing", "businessId": "business-1",
                                      "pollUrl": "/v1/profiles/business-1"}, {})

    def poll(self, path):
        self.polls += 1
        assert path == "/v1/profiles/business-1"
        return ProviderResponse(200, {"profile": profile(self.final_domain), "profileId": "profile-1"}, {})


class FakeHunter:
    def __init__(self):
        self.calls = 0

    def search(self, domain, *, limit):
        self.calls += 1
        assert domain == "acme.example" and limit == 10
        return [{"value": "jane@acme.example", "type": "personal", "decision_maker": True,
                 "first_name": "Jane", "last_name": "Doe", "position": "CTO", "confidence": 92,
                 "verification": {"status": "valid"},
                 "sources": [{"uri": "https://acme.example/team", "extracted_on": "2026-01-01",
                              "last_seen_on": "2026-09-25", "still_on_page": True}]}]


def make_runtime(foundation, tmp_path, *, lumen=None, hunter=None, approved=True,
                 calls=True, hunter_enabled=False):
    clock, db, pause, queue, outbox = foundation
    folder = tmp_path / "config"
    folder.mkdir(exist_ok=True)
    config = json.loads((Path(__file__).parents[1] / "config" / "qualification.json").read_text())
    if approved:
        config.update(rubric_version="test-v1", rubric_status="approved",
                      approved_segments=["data-ops"])
    config["lumen_max_lookups_per_day"] = 10
    if hunter_enabled:
        config.update(hunter_enabled=True, hunter_max_queries_per_day=1)
    (folder / "qualification.json").write_text(json.dumps(config), encoding="utf-8")
    settings = Settings(tmp_path, db.path, tmp_path / "PAUSE_ALL.lock",
                        "development", True, False, 5000, 300, 3, 30, 300)
    return QualificationRuntime(settings=settings, database=db, pause=pause, queue=queue,
                                outbox=outbox, lumen=lumen, hunter=hunter,
                                allow_provider_calls=calls, clock=clock)


def seed_discovery(foundation):
    _, db, _, _, _ = foundation
    with db.transaction() as c:
        for observation in ("company-source", "pain-source"):
            c.execute("INSERT INTO source_observations(id,source_type,canonical_url,content_hash,excerpt,"
                      "permitted_use,observed_at_utc,fetched_at_utc,created_at_utc) "
                      "VALUES (?,?,?,?,?,'internal_research',?,?,?)",
                      (observation, "1E:directory" if observation == "company-source" else "1A:rss",
                       "https://acme.example" if observation == "company-source" else "https://example.org/pain",
                       observation, "Business data problem", NOW, NOW, NOW))
        c.execute("INSERT INTO research_findings(id,agent_id,source_observation_id,finding_type,title,"
                  "summary,evidence_url,evidence_excerpt,status,created_at_utc) "
                  "VALUES ('company-finding','1E','company-source','organization_candidate','Acme',"
                  "'Directory listing','https://acme.example','Acme','reviewed',?)", (NOW,))
        c.execute("INSERT INTO research_findings(id,agent_id,source_observation_id,finding_type,title,"
                  "summary,evidence_url,evidence_excerpt,status,created_at_utc) "
                  "VALUES ('pain-finding','1A','pain-source','use_case_signal','Data pain',"
                  "'Stale company data','https://example.org/pain','stale','needs_codex_review',?)", (NOW,))
        c.execute("INSERT INTO organizations(id,display_name,normalized_name,canonical_domain,identity_status,"
                  "created_at_utc,updated_at_utc) VALUES ('org-1','Acme','acme','acme.example','candidate',?,?)",
                  (NOW, NOW))
        c.execute("INSERT INTO outbox_events(id,event_type,schema_version,aggregate_type,aggregate_id,"
                  "destination,payload_json,deduplication_key,state,due_at_utc,created_at_utc,updated_at_utc) "
                  "VALUES ('event-1','research.organization_candidate_reviewed',1,'organization','org-1',"
                  "'qualification.discovery',?,'company:org-1','pending',?,?,?)",
                  (json.dumps({"finding_id": "company-finding", "organization_id": "org-1"}), NOW, NOW, NOW))


def admit(runtime, foundation, *, with_case=True):
    seed_discovery(foundation)
    assert runtime.consume_discovery_once()["status"] == "needs_review"
    assert runtime.consume_discovery_once() is None
    case_id = runtime.approve_use_case(finding_id="pain-finding", vertical="data-ops",
                                       problem="Stale company facts slow reviews", buyer="data lead",
                                       reviewer="operator") if with_case else None
    if runtime.config["rubric_status"] == "approved":
        runtime.approve_rubric(reviewer="operator")
    intake_id = runtime.report()["intakes"][0]["id"]
    prospect_id = runtime.admit(intake_id=intake_id, segment="data-ops", reviewer="operator",
                                country_code="KE", use_case_id=case_id,
                                pain_evidence_url="https://acme.example/blog/stale-data" if case_id else None,
                                pain_evidence_note="Acme reports stale data in a buyer workflow" if case_id else None)
    assert runtime.report()["intakes"][0]["state"] == "admitted"
    return prospect_id


def test_department2_end_to_end_async_lookup_fit_and_contact(foundation, tmp_path):
    clock, db, _, _, _ = foundation
    lumen, hunter = FakeLumen(), FakeHunter()
    runtime = make_runtime(foundation, tmp_path, lumen=lumen, hunter=hunter, hunter_enabled=True)
    prospect_id = admit(runtime, foundation)
    assert len(runtime.tick()) == 1
    assert runtime.work_once()["status"] == "processing"
    assert runtime.tick() == []
    clock.advance(16)
    assert len(runtime.tick()) == 1
    result = runtime.work_once()
    assert result["status"] == "qualified" and result["decision"] == "qualified"
    assert result["score"] == 100
    assert len(lumen.keys) == 1 and lumen.polls == 1
    with db.read() as c:
        assert c.execute("SELECT ambiguous FROM enrichment_snapshots").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM contacts WHERE verification_status='verified'").fetchone()[0] == 0
    assert len(runtime.tick()) == 1
    assert runtime.work_once()["candidate_contacts_new"] == 1
    assert hunter.calls == 1
    detail = runtime.prospect(prospect_id)
    assert len(detail["contacts"]) == 2  # Generic Lumen address and named Hunter candidate.
    named = next(c for c in detail["contacts"] if c["display_name"] == "Jane Doe")
    assert named["verification_status"] == "candidate"
    hunter_source = next(p for p in detail["contact_provenance"] if p["contact_id"] == named["id"])
    assert hunter_source["source_last_seen_on"] == "2026-09-25"
    runtime.verify_contact(contact_id=named["id"], prospect_id=prospect_id,
                           source_url="https://acme.example/team", reviewer="operator")
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM outbox_events WHERE event_type='contact.verified'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM qualification_provider_calls WHERE state='complete'").fetchone()[0] == 2
    content = ContentRuntime(database=db, pause=foundation[2], queue=foundation[3],
                             outbox=foundation[4], clock=clock)
    assert content.consume_contact_once()["status"] == "needs_review"
    assert content.consume_contact_once() is None
    assert len(content.report()["intakes"]) == 1
    assert db.integrity_check()["ok"] is True


def test_no_provider_calls_without_explicit_opt_in(foundation, tmp_path):
    fake = FakeLumen()
    runtime = make_runtime(foundation, tmp_path, lumen=fake, calls=False)
    admit(runtime, foundation)
    assert runtime.tick() == []
    assert fake.keys == []


def test_ambiguous_profile_does_not_qualify_or_find_contacts(foundation, tmp_path):
    runtime = make_runtime(foundation, tmp_path, lumen=FakeLumen(final_domain="other.example", immediate=True))
    prospect_id = admit(runtime, foundation)
    runtime.tick()
    result = runtime.work_once()
    assert result["status"] == "ambiguous_review"
    assert result["decision"] == "review"
    assert runtime.prospect(prospect_id)["contacts"] == []


def test_missing_use_case_and_unapproved_rubric_remain_review(foundation, tmp_path):
    runtime = make_runtime(foundation, tmp_path, lumen=FakeLumen(immediate=True), approved=False)
    prospect_id = admit(runtime, foundation, with_case=False)
    runtime.tick()
    result = runtime.work_once()
    assert result["status"] == "enriched" and result["decision"] == "review"
    assert runtime.prospect(prospect_id)["assessments"][0]["score"] < 75


def test_lumen_adapter_uses_key_and_rejects_cross_origin_poll():
    calls = []
    def transport(method, url, headers, body, timeout):
        calls.append((method, url, headers, body))
        return ProviderResponse(202, {"pollUrl": "/v1/profiles/one", "status": "processing"}, {})
    client = LumenClient("https://api.example.test", "secret", transport=transport)
    client.start({"domain": "acme.example"}, "request-1")
    assert calls[0][2]["Idempotency-Key"] == "request-1"
    assert calls[0][2]["Authorization"] == "Bearer secret"
    with pytest.raises(ProviderError) as error:
        client.poll("https://evil.example/v1/profiles/one")
    assert error.value.error_class == "unsafe_poll_url"
    assert len(calls) == 1


def test_rescore_after_operator_approval_uses_saved_snapshot(foundation, tmp_path):
    fake = FakeLumen(immediate=True)
    draft = make_runtime(foundation, tmp_path, lumen=fake, approved=False)
    prospect_id = admit(draft, foundation, with_case=False)
    draft.tick()
    assert draft.work_once()["decision"] == "review"
    approved = make_runtime(foundation, tmp_path, lumen=fake, approved=True)
    case_id = approved.approve_use_case(finding_id="pain-finding", vertical="data-ops",
                                        problem="Stale facts delay reviews", buyer="data lead",
                                        reviewer="operator")
    approved.approve_rubric(reviewer="operator")
    approved.link_use_case(prospect_id=prospect_id, use_case_id=case_id, reviewer="operator",
                           pain_evidence_url="https://acme.example/blog/stale-data",
                           pain_evidence_note="The company describes its own data review problem")
    result = approved.rescore(prospect_id=prospect_id, reviewer="operator")
    assert result["decision"] == "qualified" and result["new"] is True
    assert approved.rescore(prospect_id=prospect_id, reviewer="operator")["new"] is False
    assert len(fake.keys) == 1


def test_linking_use_case_after_first_assessment_creates_new_fit_decision(foundation, tmp_path):
    fake = FakeLumen(immediate=True)
    runtime = make_runtime(foundation, tmp_path, lumen=fake)
    prospect_id = admit(runtime, foundation, with_case=False)
    runtime.tick()
    initial = runtime.work_once()
    assert initial["decision"] == "review"
    initial_assessment_id = runtime.prospect(prospect_id)["assessments"][0]["id"]
    case_id = runtime.approve_use_case(finding_id="pain-finding", vertical="data-ops",
                                       problem="Stale facts delay reviews", buyer="data lead",
                                       reviewer="operator")
    runtime.link_use_case(prospect_id=prospect_id, use_case_id=case_id, reviewer="operator",
                          pain_evidence_url="https://acme.example/blog/stale-data",
                          pain_evidence_note="Acme describes stale company facts")
    updated = runtime.rescore(prospect_id=prospect_id, reviewer="operator")
    assert updated["decision"] == "qualified" and updated["new"] is True
    assert updated["assessment_id"] != initial_assessment_id
    assert runtime.rescore(prospect_id=prospect_id, reviewer="operator")["new"] is False
    assert len(fake.keys) == 1


def test_malformed_provider_metadata_does_not_break_snapshot(foundation, tmp_path):
    class MalformedMetadata(FakeLumen):
        def start(self, payload, request_key):
            result = profile()
            result["contact"]["emails"][0]["metadata"]["last_verified"] = {"unexpected": "object"}
            result["contact"]["emails"][0]["purpose"] = ["unexpected"]
            return ProviderResponse(200, {"profile": result, "profileId": "profile-1"}, {})
    runtime = make_runtime(foundation, tmp_path, lumen=MalformedMetadata(immediate=True))
    prospect_id = admit(runtime, foundation)
    runtime.tick()
    assert runtime.work_once()["status"] == "qualified"
    assert runtime.prospect(prospect_id)["contacts"][0]["verification_status"] == "candidate"


def test_retry_uses_same_lookup_idempotency_key(foundation, tmp_path):
    class Flaky(FakeLumen):
        def start(self, payload, request_key):
            self.keys.append(request_key)
            if len(self.keys) == 1:
                raise ProviderError("network_failure", retryable=True)
            return ProviderResponse(200, {"profile": profile(), "profileId": "profile-1"}, {})
    clock, db, _, _, _ = foundation
    fake = Flaky()
    runtime = make_runtime(foundation, tmp_path, lumen=fake)
    admit(runtime, foundation)
    runtime.tick()
    assert runtime.work_once()["status"] == "retry_wait"
    clock.advance(31)
    assert runtime.work_once()["status"] == "qualified"
    assert fake.keys[0] == fake.keys[1]
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM qualification_provider_calls WHERE provider='lumen'").fetchone()[0] == 1


def test_lookup_deadline_is_recorded_not_called_success(foundation, tmp_path):
    clock, db, _, _, _ = foundation
    runtime = make_runtime(foundation, tmp_path, lumen=FakeLumen())
    prospect_id = admit(runtime, foundation)
    runtime.tick()
    runtime.work_once()
    clock.advance(241)
    runtime.tick()
    result = runtime.work_once()
    assert result["error_class"] == "lookup_deadline_exceeded"
    with db.read() as c:
        assert c.execute("SELECT outcome FROM enrichment_attempts").fetchone()[0] == "timeout"
        assert c.execute("SELECT COUNT(*) FROM enrichment_snapshots").fetchone()[0] == 0
    assert runtime.prospect(prospect_id)["prospect"]["lifecycle_state"] == "enrichment_failed"


def test_new_lookup_after_terminal_failure_requires_reconciliation(foundation, tmp_path):
    clock, db, _, _, _ = foundation
    fake = FakeLumen()
    runtime = make_runtime(foundation, tmp_path, lumen=fake)
    prospect_id = admit(runtime, foundation)
    runtime.tick()
    runtime.work_once()
    clock.advance(241)
    runtime.tick()
    runtime.work_once()
    with pytest.raises(ValueError, match="reconciliation"):
        runtime.retry_lookup(prospect_id=prospect_id, reviewer="operator",
                             reason="check", provider_effect_reconciled=False)
    runtime.retry_lookup(prospect_id=prospect_id, reviewer="operator",
                         reason="prior resolve failed and quota checked",
                         provider_effect_reconciled=True)
    assert len(runtime.tick()) == 1
    assert runtime.work_once()["status"] == "processing"
    assert len(fake.keys) == 2 and fake.keys[0] != fake.keys[1]
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM enrichment_attempts").fetchone()[0] == 2


def test_generic_mailbox_cannot_be_verified_as_decision_maker(foundation, tmp_path):
    runtime = make_runtime(foundation, tmp_path, lumen=FakeLumen(immediate=True))
    prospect_id = admit(runtime, foundation)
    runtime.tick()
    runtime.work_once()
    generic = runtime.prospect(prospect_id)["contacts"][0]
    with pytest.raises(ValueError, match="named, role-bearing"):
        runtime.verify_contact(contact_id=generic["id"], prospect_id=prospect_id,
                               source_url="https://acme.example/about", reviewer="operator")


def test_three_department2_codex_roles_parse():
    import tomllib
    root = Path(__file__).parents[1] / ".codex"
    config = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
    for name in ("enrichment_orchestrator", "fit_gatekeeper", "decision_maker_locator"):
        role = tomllib.loads((root / config["agents"][name]["config_file"]).read_text(encoding="utf-8"))
        assert len(role["developer_instructions"]) > 200
        assert ".env" in role["developer_instructions"]


def test_lumen_rate_limit_retry_after_is_preserved():
    def transport(method, url, headers, body, timeout):
        return ProviderResponse(429, {"error": {"code": "RATE_LIMITED"}},
                                {"retry-after": "120"})
    client = LumenClient("https://api.example.test", "secret", transport=transport)
    with pytest.raises(ProviderError) as error:
        client.start({"domain": "acme.example"}, "request-1")
    assert error.value.error_class == "rate_limited"
    assert error.value.retry_after_seconds == 120


def test_queue_respects_provider_retry_after(foundation):
    clock, _, _, queue, _ = foundation
    job = queue.enqueue(workflow="test", lane="qualification.2A", payload={}, logical_key="retry-after")
    queue.claim(worker_id="test", lane="qualification.2A")
    delayed = queue.fail(job_id=job.id, worker_id="test", error_class="rate_limited",
                         retryable=True, retry_after_seconds=120)
    assert delayed.state == "retry_wait"
    clock.advance(119)
    assert queue.claim(worker_id="test", lane="qualification.2A") is None
    clock.advance(1)
    assert queue.claim(worker_id="test", lane="qualification.2A").id == job.id
