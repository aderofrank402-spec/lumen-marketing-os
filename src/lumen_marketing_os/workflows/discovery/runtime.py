"""Department 8: evidence-bound opportunities and reviewed prospect escalation."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from urllib.parse import urlsplit
from uuid import uuid4

from lumen_marketing_os.core.time import utc_now, utc_text
from lumen_marketing_os.db.database import Database, append_audit
from lumen_marketing_os.policy.pause import PauseControl


def _text(value: object, minimum: int = 1, maximum: int = 1000) -> str:
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise ValueError(f"text length must be {minimum}..{maximum}")
    return value.strip()


def _url(value: object) -> tuple[str, str]:
    value = _text(value, 1, 2000)
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme != "https" or not host or "." not in host or
                parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise ValueError("a public HTTPS evidence URL is required")
        return value, host.removeprefix("www.")
    except ValueError as exc:
        raise ValueError("a public HTTPS evidence URL is required") from exc


def _domain(value: object) -> str:
    value = _text(value, 4, 253).lower().rstrip(".").removeprefix("www.")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", value):
        raise ValueError("valid organization domain required")
    return value


class DiscoveryRuntime:
    def __init__(self, db: Database, pause: PauseControl) -> None:
        self.db, self.pause = db, pause

    def _guard(self) -> None:
        self.pause.guard("discovery")

    def cycle(self, *, cycle_key: str) -> dict:
        """Queue bounded research and review tasks from existing local evidence."""
        self._guard()
        cycle_key = _text(cycle_key, 4, 120)
        now = utc_text(utc_now())
        with self.db.transaction() as c:
            existing = c.execute("SELECT id FROM vertical_cycles WHERE cycle_key=?", (cycle_key,)).fetchone()
            if existing:
                return {"cycle_id": existing["id"], "replayed": True, "new_tasks": 0}
            cycle_id = uuid4().hex
            c.execute("INSERT INTO vertical_cycles(id,cycle_key,started_at_utc,finished_at_utc,signals_seen,signals_new,dispatched,status) "
                      "VALUES (?,?,?,?,0,0,0,'complete')", (cycle_id, cycle_key, now, now))
            subjects = {
                "8A": [("research_finding", r["id"]) for r in c.execute(
                    "SELECT id FROM research_findings WHERE agent_id IN ('1A','1B') AND status='reviewed' "
                    "ORDER BY created_at_utc DESC LIMIT 25")] +
                    [("community_observation", r["id"]) for r in c.execute(
                    "SELECT id FROM community_observations ORDER BY captured_at_utc DESC LIMIT 25")],
                "8B": [("opportunity", r["id"]) for r in c.execute(
                    "SELECT id FROM vertical_opportunities WHERE state='candidate' ORDER BY created_at_utc LIMIT 10")],
                "8C": [("opportunity", r["id"]) for r in c.execute(
                    "SELECT id FROM vertical_opportunities WHERE state='validated' ORDER BY score DESC LIMIT 10")],
                "8D": [("opportunity", r["id"]) for r in c.execute(
                    "SELECT DISTINCT opportunity_id AS id FROM vertical_candidates WHERE state='accepted' LIMIT 10")],
            }
            created = 0
            for agent, items in subjects.items():
                for subject_type, subject_id in items:
                    key = f"{agent}:{subject_type}:{subject_id}"
                    if c.execute("SELECT 1 FROM vertical_tasks WHERE task_key=?", (key,)).fetchone():
                        continue
                    c.execute("INSERT INTO vertical_tasks(id,task_key,cycle_id,agent_id,subject_type,subject_id,state,created_at_utc,updated_at_utc) "
                              "VALUES (?,?,?,?,?,?,'pending',?,?)",
                              (uuid4().hex, key, cycle_id, agent, subject_type, subject_id, now, now))
                    created += 1
            c.execute("UPDATE vertical_cycles SET signals_seen=?,signals_new=? WHERE id=?",
                      (sum(map(len, subjects.values())), created, cycle_id))
            append_audit(c, actor="department-8", event_type="discovery.cycle_completed", entity_type="vertical_cycle",
                         entity_id=cycle_id, details_json=json.dumps({"tasks": created}), occurred_at_utc=now)
        return {"cycle_id": cycle_id, "replayed": False, "new_tasks": created,
                "by_agent": {agent: len(items) for agent, items in subjects.items()}}

    def finish_task(self, *, task_id: str, outcome: str, reason: str, actor: str) -> dict:
        self._guard()
        if outcome not in {"completed", "blocked"}:
            raise ValueError("outcome must be completed or blocked")
        reason, actor = _text(reason, 10, 500), _text(actor, 2, 150)
        now = utc_text(utc_now())
        with self.db.transaction() as c:
            row = c.execute("SELECT agent_id,state FROM vertical_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["state"] != "pending":
                raise ValueError("pending task required")
            c.execute("UPDATE vertical_tasks SET state=?,updated_at_utc=? WHERE id=?", (outcome, now, task_id))
            append_audit(c, actor=actor, event_type="discovery.task_finished", entity_type="vertical_task",
                         entity_id=task_id, details_json=json.dumps({"outcome": outcome, "reason": reason}),
                         occurred_at_utc=now)
        return {"task_id": task_id, "outcome": outcome}

    def scout(self, *, vertical: str, problem: str, buyer: str, source_url: str,
              excerpt: str, actor: str = "8A") -> dict:
        self._guard()
        vertical, problem, buyer = (_text(vertical, 3, 160), _text(problem, 20, 1000),
                                    _text(buyer, 3, 200))
        excerpt = _text(excerpt, 20, 1000)
        source_url, host = _url(source_url)
        key = hashlib.sha256(json.dumps([vertical.casefold(), problem.casefold()],
                                      ensure_ascii=False).encode()).hexdigest()
        now = utc_text(utc_now())
        with self.db.transaction() as c:
            row = c.execute("SELECT id FROM vertical_opportunities WHERE signal_key=?", (key,)).fetchone()
            if row:
                opportunity_id = row["id"]
            else:
                opportunity_id = uuid4().hex
                c.execute("INSERT INTO vertical_opportunities(id,signal_key,vertical,problem,buyer,state,created_at_utc,updated_at_utc) "
                          "VALUES (?,?,?,?,?,'candidate',?,?)",
                          (opportunity_id, key, vertical, problem, buyer, now, now))
            c.execute("INSERT OR IGNORE INTO vertical_evidence(id,opportunity_id,polarity,source_url,source_domain,excerpt,observed_at_utc) "
                      "VALUES (?,?,'support',?,?,?,?)", (uuid4().hex, opportunity_id, source_url, host, excerpt, now))
            append_audit(c, actor=actor, event_type="discovery.signal_recorded", entity_type="vertical_opportunity",
                         entity_id=opportunity_id, occurred_at_utc=now)
        return {"opportunity_id": opportunity_id, "state": "candidate"}

    def evidence(self, *, opportunity_id: str, polarity: str, source_url: str,
                 excerpt: str, actor: str = "8B") -> dict:
        self._guard()
        if polarity not in {"support", "counter"}:
            raise ValueError("polarity must be support or counter")
        source_url, host = _url(source_url)
        excerpt = _text(excerpt, 20, 1000)
        now = utc_text(utc_now())
        with self.db.transaction() as c:
            if not c.execute("SELECT 1 FROM vertical_opportunities WHERE id=? AND state='candidate'",
                             (opportunity_id,)).fetchone():
                raise ValueError("candidate opportunity required")
            c.execute("INSERT OR IGNORE INTO vertical_evidence(id,opportunity_id,polarity,source_url,source_domain,excerpt,observed_at_utc) "
                      "VALUES (?,?,?,?,?,?,?)", (uuid4().hex, opportunity_id, polarity, source_url, host, excerpt, now))
            append_audit(c, actor=actor, event_type="discovery.evidence_recorded", entity_type="vertical_opportunity",
                         entity_id=opportunity_id, occurred_at_utc=now)
        return {"opportunity_id": opportunity_id, "polarity": polarity}

    def validate(self, *, opportunity_id: str, buyer_workflow: str, product_fit: str,
                 limitations: str, reviewer: str) -> dict:
        self._guard()
        buyer_workflow, product_fit, limitations = (_text(buyer_workflow, 20, 2000),
                                                     _text(product_fit, 20, 2000),
                                                     _text(limitations, 20, 2000))
        reviewer = _text(reviewer, 2, 150)
        now = utc_text(utc_now())
        with self.db.transaction() as c:
            row = c.execute("SELECT * FROM vertical_opportunities WHERE id=?", (opportunity_id,)).fetchone()
            if row is None or row["state"] not in {"candidate", "validated"}:
                raise ValueError("candidate opportunity required")
            evidence = c.execute("SELECT polarity,source_domain FROM vertical_evidence WHERE opportunity_id=?",
                                 (opportunity_id,)).fetchall()
            supporting = {r["source_domain"] for r in evidence if r["polarity"] == "support"}
            counter = {r["source_domain"] for r in evidence if r["polarity"] == "counter"}
            if len(supporting) < 2 or not counter:
                raise ValueError("two independent support domains and counter evidence required")
            # Deterministic priority only ranks records; it never grants approval.
            score = min(100, 30 + len(supporting) * 10 + min(len(counter), 3) * 5)
            dossier_id = uuid4().hex
            c.execute("INSERT OR IGNORE INTO vertical_dossiers(id,opportunity_id,buyer_workflow,product_fit,limitations,reviewer,created_at_utc) "
                      "VALUES (?,?,?,?,?,?,?)", (dossier_id, opportunity_id, buyer_workflow, product_fit,
                                                  limitations, reviewer, now))
            c.execute("UPDATE vertical_opportunities SET state='validated',score=?,updated_at_utc=? WHERE id=?",
                      (score, now, opportunity_id))
            append_audit(c, actor=reviewer, event_type="discovery.opportunity_validated",
                         entity_type="vertical_opportunity", entity_id=opportunity_id, occurred_at_utc=now)
        return {"opportunity_id": opportunity_id, "score": score, "state": "validated"}

    def candidate(self, *, opportunity_id: str, company: str, domain: str, country: str,
                  source_url: str, pain_url: str, pain_note: str, actor: str = "8C") -> dict:
        self._guard()
        company, domain = _text(company, 2, 250), _domain(domain)
        country = _text(country, 2, 2).upper()
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise ValueError("ISO alpha-2 country required")
        source_url, _ = _url(source_url)
        pain_url, _ = _url(pain_url)
        pain_note = _text(pain_note, 20, 1000)
        now = utc_text(utc_now())
        with self.db.transaction() as c:
            if not c.execute("SELECT 1 FROM vertical_opportunities WHERE id=? AND state IN ('validated','dispatched')",
                             (opportunity_id,)).fetchone():
                raise ValueError("validated opportunity required")
            matches = c.execute("SELECT id,display_name,country_code,identity_status FROM organizations "
                                "WHERE canonical_domain=? AND identity_status!='merged'", (domain,)).fetchall()
            if len(matches) > 1 or (matches and (matches[0]["display_name"].casefold() != company.casefold() or
                                                  matches[0]["country_code"] not in (None, country))):
                raise ValueError("organization identity ambiguous; manual resolution required")
            if matches:
                org_id = matches[0]["id"]
            else:
                org_id = uuid4().hex
                c.execute("INSERT INTO organizations(id,display_name,normalized_name,country_code,canonical_domain,identity_status,created_at_utc,updated_at_utc) "
                          "VALUES (?,?,?,?,?,'candidate',?,?)",
                          (org_id, company, re.sub(r"[^a-z0-9]+", " ", company.casefold()).strip(), country, domain, now, now))
            observation = c.execute("SELECT id FROM source_observations WHERE source_type='department8_candidate' "
                                    "AND canonical_url=? AND content_hash=?", (source_url,
                                    hashlib.sha256(pain_note.encode()).hexdigest())).fetchone()
            if observation:
                observation_id = observation["id"]
            else:
                observation_id = uuid4().hex
                c.execute("INSERT INTO source_observations(id,source_type,canonical_url,content_hash,excerpt,permitted_use,observed_at_utc,fetched_at_utc,created_at_utc) "
                          "VALUES (?,'department8_candidate',?,? ,?,'internal_review',?,?,?)",
                          (observation_id, source_url, hashlib.sha256(pain_note.encode()).hexdigest(), pain_note,
                           now, now, now))
            row = c.execute("SELECT id,state FROM vertical_candidates WHERE opportunity_id=? AND organization_id=?",
                            (opportunity_id, org_id)).fetchone()
            candidate_id = row["id"] if row else uuid4().hex
            if not row:
                c.execute("INSERT INTO vertical_candidates(id,opportunity_id,organization_id,source_observation_id,pain_url,pain_note,state,created_at_utc,updated_at_utc) "
                          "VALUES (?,?,?,?,?,?,'needs_review',?,?)",
                          (candidate_id, opportunity_id, org_id, observation_id, pain_url, pain_note, now, now))
            append_audit(c, actor=actor, event_type="discovery.candidate_recorded", entity_type="vertical_candidate",
                         entity_id=candidate_id, occurred_at_utc=now)
        return {"candidate_id": candidate_id, "state": row["state"] if row else "needs_review"}

    def review_candidate(self, *, candidate_id: str, accept: bool, reviewer: str) -> dict:
        self._guard()
        reviewer = _text(reviewer, 2, 150)
        now = utc_text(utc_now())
        with self.db.transaction() as c:
            row = c.execute("SELECT v.id,o.state,org.identity_status FROM vertical_candidates v "
                            "JOIN vertical_opportunities o ON o.id=v.opportunity_id "
                            "JOIN organizations org ON org.id=v.organization_id WHERE v.id=? AND v.state='needs_review'",
                            (candidate_id,)).fetchone()
            if row is None or row["state"] not in {"validated", "dispatched"} or row["identity_status"] == "ambiguous":
                raise ValueError("reviewable candidate and valid identity required")
            state = "accepted" if accept else "rejected"
            c.execute("UPDATE vertical_candidates SET state=?,reviewer=?,updated_at_utc=? WHERE id=?",
                      (state, reviewer, now, candidate_id))
            append_audit(c, actor=reviewer, event_type="discovery.candidate_reviewed",
                         entity_type="vertical_candidate", entity_id=candidate_id, occurred_at_utc=now)
        return {"candidate_id": candidate_id, "state": state}

    def dispatch(self, *, opportunity_id: str, approver: str) -> dict:
        self._guard()
        approver = _text(approver, 2, 150)
        now = utc_text(utc_now())
        with self.db.transaction() as c:
            row = c.execute("SELECT * FROM vertical_opportunities WHERE id=? AND state IN ('validated','dispatched')",
                            (opportunity_id,)).fetchone()
            if row is None:
                raise ValueError("validated opportunity required")
            accepted = c.execute("SELECT * FROM vertical_candidates WHERE opportunity_id=? AND state='accepted'",
                                 (opportunity_id,)).fetchall()
            if not accepted:
                if row["state"] == "dispatched":
                    return {"opportunity_id": opportunity_id, "use_case_id": row["use_case_id"],
                            "qualification_intakes": 0, "replayed": True}
                raise ValueError("accepted candidate required")
            use_case_id = row["use_case_id"] or uuid4().hex
            if row["use_case_id"] is None:
                c.execute("INSERT INTO use_cases(id,vertical,problem_statement,buyer_hypothesis,status,owner,created_at_utc,updated_at_utc) "
                          "VALUES (?,?,?,?, 'approved',?,?,?)",
                          (use_case_id, row["vertical"], row["problem"], row["buyer"], approver, now, now))
            dispatched = 0
            for item in accepted:
                intake_id = uuid4().hex
                c.execute("INSERT INTO qualification_intakes(id,organization_id,source_observation_id,state,created_at_utc) "
                          "VALUES (?,?,?,'needs_review',?)",
                          (intake_id, item["organization_id"], item["source_observation_id"], now))
                c.execute("UPDATE vertical_candidates SET state='dispatched',intake_id=?,updated_at_utc=? WHERE id=?",
                          (intake_id, now, item["id"]))
                dispatched += 1
            c.execute("UPDATE vertical_opportunities SET state='dispatched',use_case_id=?,updated_at_utc=? WHERE id=?",
                      (use_case_id, now, opportunity_id))
            append_audit(c, actor=approver, event_type="discovery.dispatched", entity_type="vertical_opportunity",
                         entity_id=opportunity_id, details_json=json.dumps({"intakes": dispatched}), occurred_at_utc=now)
        return {"opportunity_id": opportunity_id, "use_case_id": use_case_id,
                "qualification_intakes": dispatched}

    def report(self, *, limit: int = 50) -> dict:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be 1..200")
        with self.db.read() as c:
            opportunities = [dict(r) for r in c.execute("SELECT * FROM vertical_opportunities ORDER BY score DESC,created_at_utc LIMIT ?", (limit,))]
            for opportunity in opportunities:
                opportunity["evidence"] = [dict(r) for r in c.execute(
                    "SELECT polarity,source_url,source_domain,excerpt,observed_at_utc FROM vertical_evidence "
                    "WHERE opportunity_id=? ORDER BY polarity,source_url LIMIT 50", (opportunity["id"],))]
                dossier = c.execute("SELECT buyer_workflow,product_fit,limitations,reviewer,created_at_utc "
                                    "FROM vertical_dossiers WHERE opportunity_id=?", (opportunity["id"],)).fetchone()
                opportunity["dossier"] = dict(dossier) if dossier else None
            candidates = [dict(r) for r in c.execute(
                "SELECT v.*,o.display_name,o.canonical_domain,o.country_code,o.identity_status "
                "FROM vertical_candidates v JOIN organizations o ON o.id=v.organization_id "
                "ORDER BY v.created_at_utc DESC LIMIT ?", (limit,))]
            cycles = [dict(r) for r in c.execute("SELECT * FROM vertical_cycles ORDER BY started_at_utc DESC LIMIT 10")]
            tasks = [dict(r) for r in c.execute("SELECT * FROM vertical_tasks WHERE state='pending' "
                                               "ORDER BY agent_id,created_at_utc LIMIT ?", (limit,))]
        return {"opportunities": opportunities, "candidates": candidates, "cycles": cycles,
                "pending_tasks": tasks}
