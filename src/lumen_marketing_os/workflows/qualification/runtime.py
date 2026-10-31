"""Department 2: reviewed intake, asynchronous enrichment, fit, and contacts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from lumen_marketing_os.adapters.qualification import HunterClient, LumenClient, ProviderError
from lumen_marketing_os.config import Settings
from lumen_marketing_os.core.errors import PausedError
from lumen_marketing_os.core.time import utc_now, utc_text
from lumen_marketing_os.db.database import Database, append_audit
from lumen_marketing_os.engine.outbox import Outbox
from lumen_marketing_os.engine.queue import Job, JobQueue, OutgoingEvent
from lumen_marketing_os.policy.pause import PauseControl
from lumen_marketing_os.workflows.qualification.model import assess_fit, normalize_profile


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _secret(workspace: Path, name: str) -> str:
    if name in os.environ:
        return os.environ[name].strip()
    path = workspace / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip().startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _domain(value: str | None) -> str | None:
    if not value:
        return None
    if "://" in value:
        value = urlsplit(value).hostname or ""
    value = value.lower().strip().removeprefix("www.")
    return value if re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", value) else None


def _url(value: str) -> bool:
    target = urlsplit(value)
    return target.scheme in {"http", "https"} and bool(target.hostname)


def _fit_input_hash(*, prospect_id: str, snapshot_id: str, config_hash: str,
                    segment: str, use_case_id: str | None, use_case_status: str | None,
                    pain_evidence_url: str | None, pain_evidence_note: str | None,
                    identity_status: str,
                    rubric_approved: bool) -> str:
    return hashlib.sha256(_json({
        "prospect_id": prospect_id, "snapshot_id": snapshot_id,
        "config_hash": config_hash, "segment": segment,
        "use_case_id": use_case_id, "use_case_status": use_case_status,
        "pain_evidence_url": pain_evidence_url, "pain_evidence_note": pain_evidence_note,
        "identity_status": identity_status,
        "rubric_approved": rubric_approved,
    }).encode()).hexdigest()


class QualificationRuntime:
    def __init__(self, *, settings: Settings, database: Database, pause: PauseControl,
                 queue: JobQueue, outbox: Outbox, lumen: LumenClient | None = None,
                 hunter: HunterClient | None = None, allow_provider_calls: bool | None = None,
                 clock: Callable = utc_now) -> None:
        self.settings, self.db, self.pause = settings, database, pause
        self.queue, self.outbox, self.clock = queue, outbox, clock
        self.config = json.loads((settings.workspace / "config" / "qualification.json").read_text(encoding="utf-8"))
        self._validate_config()
        self.config_hash = hashlib.sha256(_json(self.config).encode()).hexdigest()
        if allow_provider_calls is None:
            allow_provider_calls = _secret(settings.workspace, "LMOS_ALLOW_LUMEN_LOOKUPS").lower() == "true"
        self.allow_provider_calls = allow_provider_calls
        if lumen is None and allow_provider_calls:
            base, key = _secret(settings.workspace, "LUMEN_API_BASE_URL"), _secret(settings.workspace, "LUMEN_API_KEY")
            if base and key:
                lumen = LumenClient(base, key, allow_http_local=settings.environment == "development")
        self.lumen = lumen
        if hunter is None and self.config["hunter_enabled"] and self.config["hunter_max_queries_per_day"] > 0:
            key = _secret(settings.workspace, "HUNTER_API_KEY")
            if key:
                hunter = HunterClient(key)
        self.hunter = hunter

    def _validate_config(self) -> None:
        c = self.config
        if c.get("version") != 1 or c.get("rubric_status") != "draft" and c.get("rubric_status") != "approved":
            raise ValueError("invalid qualification config version or status")
        if not isinstance(c.get("rubric_version"), str) or not c["rubric_version"].strip():
            raise ValueError("rubric version required")
        if not isinstance(c.get("approved_segments"), list) or any(
                not isinstance(s, str) or not s.strip() for s in c["approved_segments"]):
            raise ValueError("approved_segments must be a list of names")
        for key in ("minimum_score", "poll_interval_seconds", "lookup_deadline_seconds",
                    "max_poll_count", "hunter_max_contacts_per_domain", "hunter_max_queries_per_day",
                    "lumen_max_lookups_per_day"):
            if type(c.get(key)) is not int or c[key] < (0 if key in {
                    "hunter_max_queries_per_day", "lumen_max_lookups_per_day"} else 1):
                raise ValueError(f"invalid {key}")
        if not isinstance(c.get("minimum_profile_confidence"), (int, float)) or not 0 <= c["minimum_profile_confidence"] <= 1:
            raise ValueError("invalid minimum_profile_confidence")
        if type(c.get("hunter_enabled")) is not bool or c["hunter_max_contacts_per_domain"] > 10:
            raise ValueError("invalid Hunter configuration")

    def _now(self) -> str:
        return utc_text(self.clock())

    def consume_discovery_once(self, *, worker_id: str = "qualification-intake") -> dict | None:
        item = self.outbox.claim(worker_id=worker_id, destination="qualification.discovery")
        if item is None:
            return None
        try:
            if item.event_type != "research.organization_candidate_reviewed" or item.schema_version != 1:
                raise ValueError("unsupported qualification discovery event")
            organization_id = item.payload.get("organization_id")
            finding_id = item.payload.get("finding_id")
            with self.db.transaction() as connection:
                finding = connection.execute(
                    "SELECT source_observation_id FROM research_findings WHERE id=? AND agent_id='1E' "
                    "AND finding_type='organization_candidate' AND status='reviewed'", (finding_id,)).fetchone()
                organization = connection.execute(
                    "SELECT id FROM organizations WHERE id=? AND identity_status='candidate'", (organization_id,)).fetchone()
                if finding is None or organization is None:
                    raise ValueError("discovery event has no reviewed candidate")
                connection.execute(
                    "INSERT OR IGNORE INTO qualification_intakes(id,organization_id,source_observation_id,"
                    "finding_id,state,created_at_utc) VALUES (?,?,?,?,'needs_review',?)",
                    (uuid4().hex, organization_id, finding["source_observation_id"], finding_id, self._now()),
                )
            self.outbox.acknowledge(item_id=item.id, worker_id=worker_id)
            return {"organization_id": organization_id, "finding_id": finding_id, "status": "needs_review"}
        except (ValueError, sqlite3.Error):
            self.outbox.fail(item_id=item.id, worker_id=worker_id, error_class="invalid_discovery_event")
            return {"event_id": item.id, "status": "needs_review"}

    def consume_internal_once(self, *, worker_id: str = "qualification-handoff") -> dict | None:
        for destination in ("qualification.fit", "qualification.contact"):
            item = self.outbox.claim(worker_id=worker_id, destination=destination)
            if item is None:
                continue
            with self.db.read() as connection:
                if destination == "qualification.fit":
                    valid = item.event_type == "enrichment.finished" and connection.execute(
                        "SELECT 1 FROM enrichment_snapshots WHERE id=?",
                        (item.payload.get("snapshot_id"),)).fetchone()
                else:
                    valid = item.event_type == "fit.decided" and connection.execute(
                        "SELECT 1 FROM fit_assessments WHERE id=? AND decision='qualified'",
                        (item.payload.get("assessment_id"),)).fetchone()
            if not valid:
                self.outbox.fail(item_id=item.id, worker_id=worker_id,
                                 error_class="invalid_qualification_handoff")
                return {"event_id": item.id, "status": "needs_review"}
            self.outbox.acknowledge(item_id=item.id, worker_id=worker_id)
            return {"event_id": item.id, "status": "delivered"}
        return None

    def approve_rubric(self, *, reviewer: str) -> str:
        if not reviewer.strip() or self.config["rubric_status"] != "approved" or not self.config["approved_segments"]:
            raise ValueError("reviewer and approved rubric with target segments required")
        now = self._now()
        with self.db.transaction() as connection:
            old = connection.execute("SELECT config_hash FROM qualification_rubrics WHERE version=?",
                                     (self.config["rubric_version"],)).fetchone()
            if old and old["config_hash"] != self.config_hash:
                raise ValueError("rubric version changed; use a new version")
            connection.execute(
                "INSERT OR IGNORE INTO qualification_rubrics(version,config_hash,config_json,approved_by,approved_at_utc) "
                "VALUES (?,?,?,?,?)", (self.config["rubric_version"], self.config_hash,
                                    _json(self.config), reviewer, now),
            )
            append_audit(connection, actor=reviewer, event_type="qualification.rubric_approved",
                         entity_type="qualification_rubric", entity_id=self.config["rubric_version"],
                         occurred_at_utc=now)
        return self.config["rubric_version"]

    def approve_use_case(self, *, finding_id: str, vertical: str, problem: str,
                         buyer: str, reviewer: str) -> str:
        if not all(value.strip() for value in (finding_id, vertical, problem, buyer, reviewer)):
            raise ValueError("finding, vertical, problem, buyer and reviewer required")
        now = self._now()
        with self.db.transaction() as connection:
            finding = connection.execute(
                "SELECT source_observation_id FROM research_findings WHERE id=? AND agent_id IN ('1A','1B') "
                "AND status!='rejected'", (finding_id,)).fetchone()
            if finding is None:
                raise ValueError("finding must be a non-rejected 1A/1B signal")
            existing = connection.execute(
                "SELECT entity_id FROM observation_links WHERE source_observation_id=? "
                "AND entity_type='use_case' AND relation='approved_signal'",
                (finding["source_observation_id"],)).fetchone()
            if existing:
                return existing["entity_id"]
            case_id = uuid4().hex
            connection.execute("INSERT INTO use_cases(id,vertical,problem_statement,buyer_hypothesis,status,owner,"
                               "created_at_utc,updated_at_utc) VALUES (?,?,?,?,'approved',?,?,?)",
                               (case_id, vertical, problem, buyer, reviewer, now, now))
            connection.execute("INSERT INTO observation_links(source_observation_id,entity_type,entity_id,relation) "
                               "VALUES (?,'use_case',?,'approved_signal')",
                               (finding["source_observation_id"], case_id))
            connection.execute("UPDATE research_findings SET status='reviewed',reviewed_at_utc=?,reviewer=? "
                               "WHERE id=? AND status='needs_codex_review'", (now, reviewer, finding_id))
            append_audit(connection, actor=reviewer, event_type="qualification.use_case_approved",
                         entity_type="use_case", entity_id=case_id,
                         details_json=_json({"finding_id": finding_id}), occurred_at_utc=now)
        return case_id

    def admit(self, *, intake_id: str, segment: str, reviewer: str,
              country_code: str | None = None, location_hint: str | None = None,
              use_case_id: str | None = None, pain_evidence_url: str | None = None,
              pain_evidence_note: str | None = None) -> str:
        if not all(value.strip() for value in (intake_id, segment, reviewer)):
            raise ValueError("intake, segment and reviewer required")
        if country_code and not re.fullmatch(r"[A-Z]{2}", country_code):
            raise ValueError("country_code must be ISO alpha-2 uppercase")
        if use_case_id and (not pain_evidence_url or not _url(pain_evidence_url) or
                            not pain_evidence_note or not pain_evidence_note.strip()):
            raise ValueError("a linked use case requires prospect-specific pain evidence URL and note")
        now = self._now()
        with self.db.transaction() as connection:
            intake = connection.execute("SELECT * FROM qualification_intakes WHERE id=?", (intake_id,)).fetchone()
            if intake is None or intake["state"] != "needs_review":
                raise ValueError("intake is absent or already decided")
            org = connection.execute("SELECT * FROM organizations WHERE id=?", (intake["organization_id"],)).fetchone()
            if org is None or org["identity_status"] != "candidate" or not _domain(org["canonical_domain"]):
                raise ValueError("a reviewed candidate with a valid domain is required")
            if use_case_id and not connection.execute(
                    "SELECT 1 FROM use_cases WHERE id=? AND status='approved'", (use_case_id,)).fetchone():
                raise ValueError("use case is not approved")
            existing = connection.execute(
                "SELECT id FROM prospects WHERE organization_id=? AND segment=? "
                "AND COALESCE(use_case_id,'')=COALESCE(?,'')",
                (org["id"], segment, use_case_id)).fetchone()
            if existing:
                prospect_id = existing["id"]
            else:
                prospect_id = uuid4().hex
                connection.execute(
                    "INSERT INTO prospects(id,organization_id,use_case_id,segment,acquisition_source_id,"
                    "lifecycle_state,location_hint,pain_evidence_url,pain_evidence_note,created_at_utc,updated_at_utc) "
                    "VALUES (?,?,?,?,?,'discovered',?,?,?,?,?)",
                    (prospect_id, org["id"], use_case_id, segment, intake["source_observation_id"],
                     location_hint, pain_evidence_url, pain_evidence_note, now, now),
                )
            connection.execute("UPDATE organizations SET identity_status='verified',country_code=COALESCE(?,country_code),"
                               "updated_at_utc=? WHERE id=?", (country_code, now, org["id"]))
            connection.execute("UPDATE qualification_intakes SET state='admitted',reviewer=?,reviewed_at_utc=? WHERE id=?",
                               (reviewer, now, intake_id))
            append_audit(connection, actor=reviewer, event_type="qualification.prospect_admitted",
                         entity_type="prospect", entity_id=prospect_id,
                         details_json=_json({"intake_id": intake_id, "segment": segment}),
                         occurred_at_utc=now)
        return prospect_id

    def reject_intake(self, *, intake_id: str, reviewer: str, reason: str) -> None:
        if not reviewer.strip() or not reason.strip():
            raise ValueError("reviewer and reason required")
        now = self._now()
        with self.db.transaction() as connection:
            changed = connection.execute(
                "UPDATE qualification_intakes SET state='rejected',reviewer=?,reason=?,reviewed_at_utc=? "
                "WHERE id=? AND state='needs_review'", (reviewer, reason, now, intake_id))
            if changed.rowcount != 1:
                raise ValueError("intake absent or already decided")
            append_audit(connection, actor=reviewer, event_type="qualification.intake_rejected",
                         entity_type="qualification_intake", entity_id=intake_id,
                         details_json=_json({"reason": reason}), occurred_at_utc=now)

    def link_use_case(self, *, prospect_id: str, use_case_id: str, reviewer: str,
                      pain_evidence_url: str, pain_evidence_note: str) -> None:
        if not reviewer.strip() or not _url(pain_evidence_url) or not pain_evidence_note.strip():
            raise ValueError("reviewer and prospect-specific pain evidence required")
        now = self._now()
        with self.db.transaction() as connection:
            case = connection.execute("SELECT 1 FROM use_cases WHERE id=? AND status='approved'",
                                      (use_case_id,)).fetchone()
            prospect = connection.execute("SELECT use_case_id FROM prospects WHERE id=?",
                                          (prospect_id,)).fetchone()
            if not case or not prospect or prospect["use_case_id"] not in {None, use_case_id}:
                raise ValueError("approved use case and unlinked prospect required")
            connection.execute("UPDATE prospects SET use_case_id=?,pain_evidence_url=?,"
                               "pain_evidence_note=?,updated_at_utc=? WHERE id=?",
                               (use_case_id, pain_evidence_url, pain_evidence_note, now, prospect_id))
            append_audit(connection, actor=reviewer, event_type="qualification.use_case_linked",
                         entity_type="prospect", entity_id=prospect_id,
                         details_json=_json({"use_case_id": use_case_id}), occurred_at_utc=now)

    def rescore(self, *, prospect_id: str, reviewer: str) -> dict:
        if not reviewer.strip():
            raise ValueError("reviewer required")
        now = self._now()
        with self.db.transaction() as connection:
            prospect = connection.execute(
                "SELECT p.segment,p.use_case_id,p.pain_evidence_url,p.pain_evidence_note,"
                "o.identity_status FROM prospects p "
                "JOIN organizations o ON o.id=p.organization_id WHERE p.id=?", (prospect_id,)).fetchone()
            snapshot = connection.execute(
                "SELECT s.id,s.normalized_json,s.ambiguous FROM enrichment_snapshots s "
                "JOIN enrichment_attempts a ON a.id=s.attempt_id WHERE a.prospect_id=? "
                "ORDER BY s.created_at_utc DESC LIMIT 1", (prospect_id,)).fetchone()
            if not prospect or not snapshot:
                raise ValueError("prospect has no completed enrichment snapshot")
            rubric = connection.execute("SELECT 1 FROM qualification_rubrics WHERE version=? AND config_hash=?",
                                        (self.config["rubric_version"], self.config_hash)).fetchone()
            case = connection.execute("SELECT status FROM use_cases WHERE id=?",
                                      (prospect["use_case_id"],)).fetchone() if prospect["use_case_id"] else None
            input_hash = _fit_input_hash(
                prospect_id=prospect_id, snapshot_id=snapshot["id"], config_hash=self.config_hash,
                segment=prospect["segment"], use_case_id=prospect["use_case_id"],
                use_case_status=case["status"] if case else None,
                pain_evidence_url=prospect["pain_evidence_url"],
                pain_evidence_note=prospect["pain_evidence_note"],
                identity_status=prospect["identity_status"], rubric_approved=bool(rubric))
            previous = connection.execute(
                "SELECT id,score,decision FROM fit_assessments WHERE prospect_id=? AND snapshot_id=? "
                "AND rubric_version=? AND config_hash=? AND input_hash=?",
                (prospect_id, snapshot["id"], self.config["rubric_version"],
                 self.config_hash, input_hash)).fetchone()
            if previous:
                return {"assessment_id": previous["id"], "score": previous["score"],
                        "decision": previous["decision"], "new": False}
            normalized = json.loads(snapshot["normalized_json"])
            score, factors, decision = assess_fit(
                segment=prospect["segment"], approved_segments=self.config["approved_segments"],
                rubric_approved=bool(rubric), use_case_approved=bool(case and case["status"] == "approved"
                                                            and prospect["pain_evidence_url"]),
                identity_verified=prospect["identity_status"] == "verified",
                ambiguous=bool(snapshot["ambiguous"]), normalized=normalized,
                minimum_confidence=self.config["minimum_profile_confidence"],
                minimum_score=self.config["minimum_score"])
            assessment_id = uuid4().hex
            connection.execute(
                "INSERT INTO fit_assessments(id,prospect_id,snapshot_id,rubric_version,score,reasons_json,"
                "decision,assessed_at_utc,config_hash,input_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (assessment_id, prospect_id, snapshot["id"], self.config["rubric_version"],
                 score, _json(factors), decision, now, self.config_hash, input_hash),
            )
            state = "ambiguous_review" if snapshot["ambiguous"] else (
                "qualified" if decision == "qualified" else "enriched")
            connection.execute("UPDATE prospects SET lifecycle_state=?,updated_at_utc=? WHERE id=?",
                               (state, now, prospect_id))
            connection.execute(
                "INSERT INTO outbox_events(id,event_type,schema_version,aggregate_type,aggregate_id,"
                "destination,payload_json,deduplication_key,state,due_at_utc,created_at_utc,updated_at_utc) "
                "VALUES (?,'fit.decided',1,'prospect',?,?,?,?, 'pending',?,?,?)",
                (uuid4().hex, prospect_id,
                 "qualification.contact" if decision == "qualified" else "qualification.review",
                 _json({"prospect_id": prospect_id, "assessment_id": assessment_id,
                        "snapshot_id": snapshot["id"], "decision": decision}),
                 f"qualification:fit:{assessment_id}", now, now, now),
            )
            append_audit(connection, actor=reviewer, event_type="qualification.rescored",
                         entity_type="prospect", entity_id=prospect_id,
                         details_json=_json({"assessment_id": assessment_id, "decision": decision}),
                         occurred_at_utc=now)
        return {"assessment_id": assessment_id, "score": score, "decision": decision,
                "new": True}

    def retry_lookup(self, *, prospect_id: str, reviewer: str, reason: str,
                     provider_effect_reconciled: bool) -> None:
        if not reviewer.strip() or not reason.strip() or not provider_effect_reconciled:
            raise ValueError("reviewer, reason and explicit provider-effect reconciliation required")
        now = self._now()
        with self.db.transaction() as connection:
            prospect = connection.execute("SELECT lifecycle_state FROM prospects WHERE id=?",
                                          (prospect_id,)).fetchone()
            prior = connection.execute("SELECT outcome FROM enrichment_attempts WHERE prospect_id=? "
                                       "ORDER BY started_at_utc DESC,rowid DESC LIMIT 1",
                                       (prospect_id,)).fetchone()
            if (not prospect or prospect["lifecycle_state"] != "enrichment_failed" or not prior or
                    prior["outcome"] not in {"not_found", "timeout", "transient_error", "permanent_error"}):
                raise ValueError("only a reviewed terminal enrichment failure can be retried")
            connection.execute("UPDATE prospects SET lifecycle_state='discovered',updated_at_utc=? WHERE id=?",
                               (now, prospect_id))
            append_audit(connection, actor=reviewer, event_type="qualification.lookup_retry_approved",
                         entity_type="prospect", entity_id=prospect_id,
                         details_json=_json({"reason": reason, "prior_outcome": prior["outcome"]}),
                         occurred_at_utc=now)

    def tick(self) -> list[Job]:
        self.pause.guard()
        jobs: list[Job] = []
        now = self._now()
        with self.db.read() as connection:
            starts = connection.execute(
                "SELECT p.id,COUNT(a.id) AS attempt_count FROM prospects p "
                "LEFT JOIN enrichment_attempts a ON a.prospect_id=p.id "
                "WHERE p.lifecycle_state='discovered' GROUP BY p.id "
                "HAVING SUM(CASE WHEN a.outcome='started' THEN 1 ELSE 0 END)=0 "
                "ORDER BY p.created_at_utc LIMIT 50").fetchall()
            polls = connection.execute(
                "SELECT id,prospect_id,poll_count FROM enrichment_attempts WHERE outcome='started' "
                "AND poll_path IS NOT NULL AND poll_after_utc<=? ORDER BY poll_after_utc LIMIT 50",
                (now,)).fetchall()
            contacts = connection.execute(
                "SELECT p.id FROM prospects p LEFT JOIN contact_research c ON c.prospect_id=p.id "
                "WHERE p.lifecycle_state='qualified' AND c.prospect_id IS NULL LIMIT 50").fetchall()
        if self.allow_provider_calls and self.lumen is not None:
            with self.db.read() as connection:
                used = connection.execute(
                    "SELECT COUNT(*) FROM qualification_provider_calls WHERE provider='lumen' "
                    "AND reserved_at_utc>=? AND reserved_at_utc<?",
                    (now[:10], (datetime.fromisoformat(now[:10]) + timedelta(days=1)).date().isoformat()),
                ).fetchone()[0]
            allowance = max(0, self.config["lumen_max_lookups_per_day"] - used)
            for row in starts[:allowance]:
                jobs.append(self.queue.enqueue(workflow="department2.start", lane="qualification.2A",
                                               payload={"prospect_id": row["id"]},
                                               logical_key=f"qualification:start:{row['id']}:{row['attempt_count']}"))
            for row in polls:
                jobs.append(self.queue.enqueue(workflow="department2.poll", lane="qualification.2A",
                                               payload={"attempt_id": row["id"]},
                                               logical_key=f"qualification:poll:{row['id']}:{row['poll_count']}"))
        if self.config["hunter_enabled"] and self.hunter and self.config["hunter_max_queries_per_day"] > 0:
            for row in contacts:
                jobs.append(self.queue.enqueue(workflow="department2.contacts", lane="qualification.2C",
                                               payload={"prospect_id": row["id"]},
                                               logical_key=f"qualification:contacts:{row['id']}"))
        return jobs

    def work_once(self, *, worker_id: str = "qualification-local") -> dict | None:
        for lane in ("qualification.2A", "qualification.2C"):
            try:
                job = self.queue.claim(worker_id=worker_id, lease_seconds=120, lane=lane)
            except PausedError:
                continue
            if job is None:
                continue
            if job.workflow == "department2.start":
                return self._start(job, worker_id)
            if job.workflow == "department2.poll":
                return self._poll(job, worker_id)
            if job.workflow == "department2.contacts":
                return self._contacts(job, worker_id)
            self.queue.fail(job_id=job.id, worker_id=worker_id,
                            error_class="invalid_qualification_job", retryable=False)
            return {"job_id": job.id, "status": "invalid_job"}
        return None

    def _start(self, job: Job, worker: str) -> dict:
        if not self.allow_provider_calls or self.lumen is None:
            self.queue.fail(job_id=job.id, worker_id=worker,
                            error_class="lumen_not_configured", retryable=False)
            return {"job_id": job.id, "status": "needs_review"}
        prospect_id = job.payload.get("prospect_id")
        now = self._now()
        budget_exhausted = False
        with self.db.transaction() as connection:
            prospect = connection.execute(
                "SELECT p.*,o.display_name,o.canonical_domain,o.country_code,o.identity_status "
                "FROM prospects p JOIN organizations o ON o.id=p.organization_id WHERE p.id=?",
                (prospect_id,)).fetchone()
            if (prospect is None or prospect["identity_status"] != "verified" or
                    not _domain(prospect["canonical_domain"])):
                raise ValueError("prospect identity is not verified")
            invalid_state = prospect["lifecycle_state"] not in {"discovered", "enrichment_pending"}
            attempt = connection.execute("SELECT * FROM enrichment_attempts WHERE prospect_id=? "
                                         "ORDER BY started_at_utc DESC,rowid DESC LIMIT 1", (prospect_id,)).fetchone()
            if invalid_state:
                pass
            elif attempt is None or attempt["outcome"] != "started":
                used = connection.execute(
                    "SELECT COUNT(*) FROM qualification_provider_calls WHERE provider='lumen' "
                    "AND reserved_at_utc>=? AND reserved_at_utc<?",
                    (now[:10], (datetime.fromisoformat(now[:10]) + timedelta(days=1)).date().isoformat()),
                ).fetchone()[0]
                if used >= self.config["lumen_max_lookups_per_day"]:
                    budget_exhausted = True
                else:
                    payload = {"domain": prospect["canonical_domain"], "mode": "full",
                               "include_attributions": True}
                    attempt_id, request_key = uuid4().hex, uuid4().hex
                    connection.execute(
                        "INSERT INTO enrichment_attempts(id,prospect_id,request_key,adapter_version,"
                        "started_at_utc,outcome,request_json) VALUES (?,?,?,'lumen-http-v1',?,'started',?)",
                        (attempt_id, prospect_id, request_key, now, _json(payload)),
                    )
                    connection.execute(
                        "INSERT INTO qualification_provider_calls(id,provider,prospect_id,request_key,state,"
                        "reserved_at_utc) VALUES (?,'lumen',?,?,'reserved',?)",
                        (uuid4().hex, prospect_id, request_key, now),
                    )
                    connection.execute("UPDATE prospects SET lifecycle_state='enrichment_pending',updated_at_utc=? "
                                       "WHERE id=?", (now, prospect_id))
            else:
                attempt_id, request_key = attempt["id"], attempt["request_key"]
                payload = json.loads(attempt["request_json"])
        if invalid_state:
            self.queue.fail(job_id=job.id, worker_id=worker,
                            error_class="invalid_prospect_state", retryable=False)
            return {"job_id": job.id, "status": "needs_review", "error_class": "invalid_prospect_state"}
        if budget_exhausted:
            self.queue.fail(job_id=job.id, worker_id=worker,
                            error_class="lumen_daily_budget", retryable=False)
            return {"job_id": job.id, "status": "needs_review", "error_class": "lumen_daily_budget"}
        started = time.monotonic()
        try:
            response = self.lumen.start(payload, request_key)
            return self._handle_lumen_response(job, worker, attempt_id, prospect_id, response,
                                               latency_ms=int((time.monotonic()-started)*1000))
        except ProviderError as exc:
            return self._provider_failure(job, worker, attempt_id, prospect_id, exc,
                                          latency_ms=int((time.monotonic()-started)*1000))

    def _poll(self, job: Job, worker: str) -> dict:
        if not self.allow_provider_calls or self.lumen is None:
            self.queue.fail(job_id=job.id, worker_id=worker,
                            error_class="lumen_not_configured", retryable=False)
            return {"job_id": job.id, "status": "needs_review"}
        with self.db.read() as connection:
            attempt = connection.execute("SELECT * FROM enrichment_attempts WHERE id=?",
                                         (job.payload.get("attempt_id"),)).fetchone()
        if attempt is None or attempt["outcome"] != "started" or not attempt["poll_path"]:
            self.queue.complete(job_id=job.id, worker_id=worker)
            return {"job_id": job.id, "status": "stale_poll"}
        now = self.clock()
        started = datetime.fromisoformat(attempt["started_at_utc"].replace("Z", "+00:00"))
        if ((now - started).total_seconds() > self.config["lookup_deadline_seconds"] or
                attempt["poll_count"] >= self.config["max_poll_count"]):
            return self._provider_failure(job, worker, attempt["id"], attempt["prospect_id"],
                                          ProviderError("lookup_deadline_exceeded"), latency_ms=0)
        began = time.monotonic()
        try:
            response = self.lumen.poll(attempt["poll_path"])
            return self._handle_lumen_response(job, worker, attempt["id"], attempt["prospect_id"],
                                               response, latency_ms=int((time.monotonic()-began)*1000))
        except ProviderError as exc:
            return self._provider_failure(job, worker, attempt["id"], attempt["prospect_id"], exc,
                                          latency_ms=int((time.monotonic()-began)*1000))

    def _handle_lumen_response(self, job: Job, worker: str, attempt_id: str, prospect_id: str,
                               response, *, latency_ms: int) -> dict:
        if response.status == 202:
            with self.db.read() as connection:
                attempt = connection.execute("SELECT poll_path,poll_count FROM enrichment_attempts WHERE id=?",
                                             (attempt_id,)).fetchone()
            poll_path = response.body.get("pollUrl") or attempt["poll_path"]
            business_id = response.body.get("businessId")
            if not isinstance(poll_path, str) or not poll_path.startswith("/v1/profiles/"):
                return self._provider_failure(job, worker, attempt_id, prospect_id,
                                              ProviderError("invalid_poll_reference"), latency_ms=latency_ms)
            # The adapter independently validates the URL before sending credentials.
            due = utc_text(self.clock() + timedelta(seconds=self.config["poll_interval_seconds"]))
            def write(connection: sqlite3.Connection) -> None:
                connection.execute("UPDATE enrichment_attempts SET poll_path=?,provider_business_id=COALESCE(?,provider_business_id),"
                                   "poll_after_utc=?,poll_count=poll_count+1,last_http_status=202 WHERE id=?",
                                   (poll_path, business_id if isinstance(business_id, str) else None, due, attempt_id))
                connection.execute("UPDATE qualification_provider_calls SET state='complete',finished_at_utc=? "
                                   "WHERE provider='lumen' AND request_key=(SELECT request_key FROM enrichment_attempts WHERE id=?)",
                                   (self._now(), attempt_id))
            self.queue.complete(job_id=job.id, worker_id=worker, mutate=write)
            return {"job_id": job.id, "prospect_id": prospect_id, "status": "processing", "poll_after_utc": due}
        with self.db.read() as connection:
            prospect = connection.execute(
                "SELECT p.segment,p.use_case_id,p.pain_evidence_url,p.pain_evidence_note,"
                "o.display_name,o.canonical_domain,o.country_code,o.identity_status "
                "FROM prospects p JOIN organizations o ON o.id=p.organization_id WHERE p.id=?",
                (prospect_id,)).fetchone()
        try:
            normalized, evidence, ambiguous = normalize_profile(
                response.body.get("profile"), requested_name=prospect["display_name"],
                requested_domain=prospect["canonical_domain"], requested_country=prospect["country_code"])
        except ValueError:
            return self._provider_failure(job, worker, attempt_id, prospect_id,
                                          ProviderError("invalid_profile_response"), latency_ms=latency_ms)
        snapshot_id, assessment_id = uuid4().hex, uuid4().hex
        now = self._now()
        events: list[OutgoingEvent] = []
        result: dict = {"job_id": job.id, "prospect_id": prospect_id, "snapshot_id": snapshot_id}
        def write(connection: sqlite3.Connection) -> None:
            started_at = connection.execute("SELECT started_at_utc FROM enrichment_attempts WHERE id=?",
                                            (attempt_id,)).fetchone()["started_at_utc"]
            total_latency_ms = max(0, int((self.clock() - datetime.fromisoformat(
                started_at.replace("Z", "+00:00"))).total_seconds() * 1000))
            rubric = connection.execute("SELECT 1 FROM qualification_rubrics WHERE version=? AND config_hash=?",
                                        (self.config["rubric_version"], self.config_hash)).fetchone()
            case = connection.execute("SELECT status FROM use_cases WHERE id=?",
                                      (prospect["use_case_id"],)).fetchone() if prospect["use_case_id"] else None
            input_hash = _fit_input_hash(
                prospect_id=prospect_id, snapshot_id=snapshot_id, config_hash=self.config_hash,
                segment=prospect["segment"], use_case_id=prospect["use_case_id"],
                use_case_status=case["status"] if case else None,
                pain_evidence_url=prospect["pain_evidence_url"],
                pain_evidence_note=prospect["pain_evidence_note"],
                identity_status=prospect["identity_status"], rubric_approved=bool(rubric))
            score, factors, decision = assess_fit(
                segment=prospect["segment"], approved_segments=self.config["approved_segments"],
                rubric_approved=bool(rubric), use_case_approved=bool(case and case["status"] == "approved"
                                                            and prospect["pain_evidence_url"]),
                identity_verified=prospect["identity_status"] == "verified", ambiguous=ambiguous,
                normalized=normalized, minimum_confidence=self.config["minimum_profile_confidence"],
                minimum_score=self.config["minimum_score"])
            connection.execute(
                "UPDATE enrichment_attempts SET outcome=?,finished_at_utc=?,latency_ms=?,error_class=NULL,"
                "provider_profile_id=?,last_http_status=200,poll_after_utc=NULL WHERE id=?",
                ("ambiguous" if ambiguous else "success", now, total_latency_ms,
                 response.body.get("profileId") if isinstance(response.body.get("profileId"), str) else None,
                 attempt_id),
            )
            connection.execute("UPDATE qualification_provider_calls SET state='complete',finished_at_utc=? "
                               "WHERE provider='lumen' AND request_key=(SELECT request_key FROM enrichment_attempts WHERE id=?)",
                               (now, attempt_id))
            connection.execute(
                "INSERT INTO enrichment_snapshots(id,attempt_id,schema_version,raw_response_json,normalized_json,"
                "field_evidence_json,ambiguous,created_at_utc) VALUES (?,?,?,?,?,?,?,?)",
                (snapshot_id, attempt_id, "lumen-profile-v1", _json(response.body), _json(normalized),
                 _json(evidence), int(ambiguous), now),
            )
            connection.execute(
                "INSERT INTO fit_assessments(id,prospect_id,snapshot_id,rubric_version,score,reasons_json,"
                "decision,assessed_at_utc,config_hash,input_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (assessment_id, prospect_id, snapshot_id, self.config["rubric_version"], score,
                 _json(factors), decision, now, self.config_hash, input_hash),
            )
            state = "ambiguous_review" if ambiguous else "qualified" if decision == "qualified" else "enriched"
            connection.execute("UPDATE prospects SET lifecycle_state=?,updated_at_utc=? WHERE id=?",
                               (state, now, prospect_id))
            # Lumen returns business channels, not named decision-makers.
            for email in normalized["emails"]:
                if _domain(email["address"].split("@", 1)[1]) != prospect["canonical_domain"]:
                    continue
                contact_id = uuid4().hex
                connection.execute(
                    "INSERT OR IGNORE INTO contacts(id,organization_id,role_title,channel,address,normalized_address,"
                    "verification_status,created_at_utc,updated_at_utc) "
                    "SELECT ?,p.organization_id,'general business contact','email',?,?,'candidate',?,? "
                    "FROM prospects p WHERE p.id=?",
                    (contact_id, email["address"], email["address"], now, now, prospect_id),
                )
                row = connection.execute("SELECT id FROM contacts WHERE organization_id=(SELECT organization_id "
                                         "FROM prospects WHERE id=?) AND channel='email' AND normalized_address=?",
                                         (prospect_id, email["address"])).fetchone()
                for source in email["sources"]:
                    connection.execute("INSERT OR IGNORE INTO contact_provenance(id,contact_id,provider,source_url,"
                                       "provider_confidence,observed_at_utc,source_last_seen_on) "
                                       "VALUES (?,?,?,?,?,?,?)",
                                       (uuid4().hex, row["id"], "lumen", source, email["confidence"],
                                        now, email["last_verified"]))
            append_audit(connection, actor="2A", event_type="qualification.enrichment_finished",
                         entity_type="prospect", entity_id=prospect_id,
                         details_json=_json({"attempt_id": attempt_id, "snapshot_id": snapshot_id,
                                             "ambiguous": ambiguous}), occurred_at_utc=now)
            result.update({"status": state, "score": score, "decision": decision})
            events.extend((
                OutgoingEvent("enrichment.finished", "prospect", prospect_id, "qualification.fit",
                              {"prospect_id": prospect_id, "attempt_id": attempt_id,
                               "snapshot_id": snapshot_id, "ambiguous": ambiguous},
                              f"qualification:enrichment:{attempt_id}"),
                OutgoingEvent("fit.decided", "prospect", prospect_id,
                              "qualification.contact" if decision == "qualified" else "qualification.review",
                              {"prospect_id": prospect_id, "assessment_id": assessment_id,
                               "snapshot_id": snapshot_id, "decision": decision},
                              f"qualification:fit:{assessment_id}"),
            ))
        self.queue.complete(job_id=job.id, worker_id=worker, events=events, mutate=write)
        return result

    def _provider_failure(self, job: Job, worker: str, attempt_id: str, prospect_id: str,
                          error: ProviderError, *, latency_ms: int) -> dict:
        now = self._now()
        with self.db.transaction() as connection:
            started_at = connection.execute("SELECT started_at_utc FROM enrichment_attempts WHERE id=?",
                                            (attempt_id,)).fetchone()["started_at_utc"]
            total_latency_ms = max(0, int((self.clock() - datetime.fromisoformat(
                started_at.replace("Z", "+00:00"))).total_seconds() * 1000))
            connection.execute(
                "UPDATE enrichment_attempts SET outcome=?,error_class=?,latency_ms=?,last_http_status=?,"
                "finished_at_utc=? WHERE id=?",
                ("started" if error.retryable else "timeout" if error.error_class == "lookup_deadline_exceeded"
                 else "not_found" if error.error_class == "not_found" else "permanent_error",
                 error.error_class, total_latency_ms, error.status, None if error.retryable else now, attempt_id),
            )
            if not error.retryable:
                connection.execute("UPDATE prospects SET lifecycle_state='enrichment_failed',updated_at_utc=? WHERE id=?",
                                   (now, prospect_id))
                connection.execute("UPDATE qualification_provider_calls SET state='unknown',finished_at_utc=? "
                                   "WHERE provider='lumen' AND request_key=(SELECT request_key FROM enrichment_attempts WHERE id=?) "
                                   "AND state='reserved'", (now, attempt_id))
            append_audit(connection, actor="2A", event_type="qualification.enrichment_error",
                         entity_type="prospect", entity_id=prospect_id,
                         details_json=_json({"attempt_id": attempt_id, "error_class": error.error_class}),
                         occurred_at_utc=now)
        failed = self.queue.fail(job_id=job.id, worker_id=worker,
                                 error_class=error.error_class, retryable=error.retryable,
                                 retry_after_seconds=error.retry_after_seconds)
        if failed.state == "dead_letter":
            with self.db.transaction() as connection:
                connection.execute("UPDATE enrichment_attempts SET outcome='transient_error',finished_at_utc=? "
                                   "WHERE id=? AND outcome='started'", (now, attempt_id))
                connection.execute("UPDATE prospects SET lifecycle_state='enrichment_failed',updated_at_utc=? "
                                   "WHERE id=?", (now, prospect_id))
                connection.execute("UPDATE qualification_provider_calls SET state='unknown',finished_at_utc=? "
                                   "WHERE provider='lumen' AND request_key=(SELECT request_key FROM enrichment_attempts WHERE id=?) "
                                   "AND state='reserved'", (now, attempt_id))
        return {"job_id": job.id, "prospect_id": prospect_id, "status": failed.state,
                "error_class": error.error_class}

    def _contacts(self, job: Job, worker: str) -> dict:
        prospect_id = job.payload.get("prospect_id")
        now = self._now()
        if self.hunter is None or not self.config["hunter_enabled"]:
            self.queue.fail(job_id=job.id, worker_id=worker, error_class="hunter_not_configured",
                            retryable=False)
            return {"job_id": job.id, "status": "needs_review"}
        with self.db.transaction() as connection:
            row = connection.execute("SELECT p.lifecycle_state,p.organization_id,o.canonical_domain "
                                     "FROM prospects p JOIN organizations o ON o.id=p.organization_id WHERE p.id=?",
                                     (prospect_id,)).fetchone()
            if row is None or row["lifecycle_state"] != "qualified" or not _domain(row["canonical_domain"]):
                raise ValueError("contact job requires a qualified, domain-verified prospect")
            prior = connection.execute("SELECT state FROM contact_research WHERE prospect_id=?", (prospect_id,)).fetchone()
            if prior is not None:
                prior_state = prior["state"]
                blocked = False
            else:
                prior_state = None
                day = now[:10]
                used = connection.execute("SELECT COUNT(*) AS n FROM qualification_provider_calls "
                                          "WHERE provider='hunter' AND reserved_at_utc>=? AND reserved_at_utc<?",
                                          (day, (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat())).fetchone()["n"]
                if used >= self.config["hunter_max_queries_per_day"]:
                    connection.execute("INSERT INTO contact_research(prospect_id,state,provider,error_class,updated_at_utc) "
                                       "VALUES (?,'blocked','hunter','daily_budget',?)", (prospect_id, now))
                    blocked = True
                else:
                    call_id = uuid4().hex
                    connection.execute("INSERT INTO qualification_provider_calls(id,provider,prospect_id,request_key,state,"
                                       "reserved_at_utc) VALUES (?,'hunter',?,?,'reserved',?)",
                                       (call_id, prospect_id, uuid4().hex, now))
                    connection.execute("INSERT INTO contact_research(prospect_id,state,provider,attempt_count,updated_at_utc) "
                                       "VALUES (?,'in_progress','hunter',1,?)", (prospect_id, now))
                    blocked = False
        if prior_state is not None:
            if prior_state == "in_progress":
                with self.db.transaction() as connection:
                    connection.execute("UPDATE contact_research SET state='needs_review',"
                                       "error_class='uncertain_provider_call',updated_at_utc=? WHERE prospect_id=?",
                                       (now, prospect_id))
                    connection.execute("UPDATE qualification_provider_calls SET state='unknown',finished_at_utc=? "
                                       "WHERE prospect_id=? AND provider='hunter' AND state='reserved'",
                                       (now, prospect_id))
                prior_state = "needs_review"
            self.queue.complete(job_id=job.id, worker_id=worker)
            return {"job_id": job.id, "status": prior_state}
        if blocked:
            self.queue.complete(job_id=job.id, worker_id=worker)
            return {"job_id": job.id, "status": "blocked", "reason": "daily_budget"}
        try:
            emails = self.hunter.search(row["canonical_domain"],
                                        limit=self.config["hunter_max_contacts_per_domain"])
        except ProviderError as exc:
            with self.db.transaction() as connection:
                connection.execute("UPDATE contact_research SET state='needs_review',error_class=?,"
                                   "last_checked_at_utc=?,updated_at_utc=? WHERE prospect_id=?",
                                   (exc.error_class, now, now, prospect_id))
                connection.execute("UPDATE qualification_provider_calls SET state='unknown',finished_at_utc=? WHERE id=?",
                                   (now, call_id))
            self.queue.complete(job_id=job.id, worker_id=worker)
            return {"job_id": job.id, "status": "needs_review", "error_class": exc.error_class}
        added = 0
        with self.db.transaction() as connection:
            for item in emails:
                address = item.get("value")
                if (not isinstance(address, str) or "@" not in address or
                        _domain(address.split("@", 1)[1]) != row["canonical_domain"] or
                        item.get("type") != "personal" or item.get("decision_maker") is not True):
                    continue
                sources = item.get("sources") if isinstance(item.get("sources"), list) else []
                source_rows = [s for s in sources if isinstance(s, dict) and
                               isinstance(s.get("uri"), str) and _url(s["uri"])]
                if not source_rows or not isinstance(item.get("position"), str) or not item["position"].strip():
                    continue
                address = address.lower().strip()
                name = " ".join(str(item.get(k) or "").strip() for k in ("first_name", "last_name")).strip()
                contact_id = uuid4().hex
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO contacts(id,organization_id,display_name,role_title,channel,address,"
                    "normalized_address,verification_status,created_at_utc,updated_at_utc) "
                    "VALUES (?,?,?,?, 'email',?,?,'candidate',?,?)",
                    (contact_id, row["organization_id"], name or None, item["position"],
                     address, address, now, now),
                )
                added += int(inserted.rowcount > 0)
                current = connection.execute("SELECT id FROM contacts WHERE organization_id=? AND channel='email' "
                                             "AND normalized_address=?", (row["organization_id"], address)).fetchone()
                verification = item.get("verification") if isinstance(item.get("verification"), dict) else {}
                confidence = item.get("confidence") if isinstance(item.get("confidence"), (int, float)) else None
                for source in source_rows[:10]:
                    connection.execute("INSERT OR IGNORE INTO contact_provenance(id,contact_id,provider,source_url,"
                                       "provider_confidence,provider_verification,observed_at_utc,"
                                       "source_first_seen_on,source_last_seen_on,still_on_page) "
                                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                                       (uuid4().hex, current["id"], "hunter", source["uri"],
                                        confidence, verification.get("status"), now,
                                        source.get("extracted_on"), source.get("last_seen_on"),
                                        int(source["still_on_page"]) if type(source.get("still_on_page")) is bool else None))
            connection.execute("UPDATE contact_research SET state='complete',last_checked_at_utc=?,"
                               "updated_at_utc=? WHERE prospect_id=?", (now, now, prospect_id))
            connection.execute("UPDATE qualification_provider_calls SET state='complete',finished_at_utc=? WHERE id=?",
                               (now, call_id))
            append_audit(connection, actor="2C", event_type="qualification.contacts_found",
                         entity_type="prospect", entity_id=prospect_id,
                         details_json=_json({"candidate_count": added}), occurred_at_utc=now)
        self.queue.complete(job_id=job.id, worker_id=worker)
        return {"job_id": job.id, "status": "complete", "candidate_contacts_new": added}

    def add_contact(self, *, prospect_id: str, name: str, role: str,
                    email: str, source_url: str, reviewer: str) -> str:
        if not all(value.strip() for value in (prospect_id, name, role, email, reviewer)) or not _url(source_url):
            raise ValueError("prospect, name, role, email, source URL and reviewer required")
        address = email.lower().strip()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address):
            raise ValueError("invalid business email")
        now = self._now()
        with self.db.transaction() as connection:
            prospect = connection.execute(
                "SELECT p.organization_id,p.lifecycle_state,o.canonical_domain FROM prospects p "
                "JOIN organizations o ON o.id=p.organization_id WHERE p.id=?", (prospect_id,)).fetchone()
            if (prospect is None or prospect["lifecycle_state"] != "qualified" or
                    _domain(address.split("@", 1)[1]) != prospect["canonical_domain"]):
                raise ValueError("qualified prospect and matching business domain required")
            connection.execute(
                "INSERT OR IGNORE INTO contacts(id,organization_id,display_name,role_title,channel,address,"
                "normalized_address,verification_status,created_at_utc,updated_at_utc) "
                "VALUES (?,?,?,?, 'email',?,?,'candidate',?,?)",
                (uuid4().hex, prospect["organization_id"], name, role, address, address, now, now),
            )
            contact = connection.execute("SELECT id FROM contacts WHERE organization_id=? AND channel='email' "
                                         "AND normalized_address=?", (prospect["organization_id"], address)).fetchone()
            connection.execute("INSERT OR IGNORE INTO contact_provenance(id,contact_id,provider,source_url,"
                               "observed_at_utc) VALUES (?,?,'manual',?,?)",
                               (uuid4().hex, contact["id"], source_url, now))
            append_audit(connection, actor=reviewer, event_type="qualification.contact_candidate_added",
                         entity_type="contact", entity_id=contact["id"], occurred_at_utc=now)
        return contact["id"]

    def verify_contact(self, *, contact_id: str, prospect_id: str,
                       source_url: str, reviewer: str) -> None:
        if not reviewer.strip() or not _url(source_url):
            raise ValueError("reviewer and source URL required")
        now = self._now()
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT c.*,p.lifecycle_state FROM contacts c JOIN prospects p ON p.organization_id=c.organization_id "
                "WHERE c.id=? AND p.id=?", (contact_id, prospect_id)).fetchone()
            if (row is None or row["lifecycle_state"] != "qualified" or
                    row["verification_status"] != "candidate" or not row["display_name"] or
                    not row["role_title"] or row["role_title"] == "general business contact"):
                raise ValueError("qualified prospect and named, role-bearing candidate required")
            source = connection.execute("SELECT 1 FROM contact_provenance WHERE contact_id=? AND source_url=?",
                                        (contact_id, source_url)).fetchone()
            if not source:
                raise ValueError("reviewed URL must match recorded contact provenance")
            connection.execute("UPDATE contacts SET verification_status='verified',verified_at_utc=?,updated_at_utc=? "
                               "WHERE id=?", (now, now, contact_id))
            connection.execute(
                "INSERT OR IGNORE INTO outbox_events(id,event_type,schema_version,aggregate_type,aggregate_id,"
                "destination,payload_json,deduplication_key,state,due_at_utc,created_at_utc,updated_at_utc) "
                "VALUES (?,'contact.verified',1,'contact',?,'content.review',?,?,'pending',?,?,?)",
                (uuid4().hex, contact_id, _json({"contact_id": contact_id, "prospect_id": prospect_id,
                                                 "reviewer": reviewer, "evidence_url": source_url}),
                 f"qualification:contact:{contact_id}:verified", now, now, now),
            )
            append_audit(connection, actor=reviewer, event_type="qualification.contact_verified",
                         entity_type="contact", entity_id=contact_id, occurred_at_utc=now)

    def report(self, *, limit: int = 30) -> dict:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be 1..200")
        with self.db.read() as connection:
            intakes = [dict(r) for r in connection.execute(
                "SELECT id,organization_id,finding_id,state,created_at_utc FROM qualification_intakes "
                "ORDER BY created_at_utc DESC LIMIT ?", (limit,))]
            prospects = [dict(r) for r in connection.execute(
                "SELECT p.id,p.segment,p.lifecycle_state,o.display_name,o.canonical_domain,o.country_code,"
                "p.created_at_utc FROM prospects p JOIN organizations o ON o.id=p.organization_id "
                "ORDER BY p.created_at_utc DESC LIMIT ?", (limit,))]
            attempts = [dict(r) for r in connection.execute(
                "SELECT id,prospect_id,outcome,error_class,started_at_utc,finished_at_utc,poll_count "
                "FROM enrichment_attempts ORDER BY started_at_utc DESC LIMIT ?", (limit,))]
            assessments = [dict(r) for r in connection.execute(
                "SELECT id,prospect_id,rubric_version,score,decision,assessed_at_utc "
                "FROM fit_assessments ORDER BY assessed_at_utc DESC LIMIT ?", (limit,))]
        return {"provider_ready": self.allow_provider_calls and self.lumen is not None and
                                  self.config["lumen_max_lookups_per_day"] > 0,
                "hunter_ready": self.config["hunter_enabled"] and self.hunter is not None,
                "rubric_version": self.config["rubric_version"],
                "intakes": intakes, "prospects": prospects,
                "attempts": attempts, "assessments": assessments}

    def prospect(self, prospect_id: str) -> dict:
        with self.db.read() as connection:
            row = connection.execute("SELECT p.*,o.display_name,o.canonical_domain,o.country_code "
                                     "FROM prospects p JOIN organizations o ON o.id=p.organization_id WHERE p.id=?",
                                     (prospect_id,)).fetchone()
            if row is None:
                raise ValueError("prospect not found")
            attempts = [dict(r) for r in connection.execute(
                "SELECT id,outcome,error_class,started_at_utc,finished_at_utc,poll_count FROM enrichment_attempts "
                "WHERE prospect_id=? ORDER BY started_at_utc DESC", (prospect_id,))]
            assessments = [dict(r) for r in connection.execute(
                "SELECT id,score,reasons_json,decision,rubric_version,assessed_at_utc FROM fit_assessments "
                "WHERE prospect_id=? ORDER BY assessed_at_utc DESC", (prospect_id,))]
            contacts = [dict(r) for r in connection.execute(
                "SELECT id,display_name,role_title,channel,address,verification_status,verified_at_utc "
                "FROM contacts WHERE organization_id=? ORDER BY created_at_utc", (row["organization_id"],))]
            provenance = [dict(r) for r in connection.execute(
                "SELECT cp.contact_id,cp.provider,cp.source_url,cp.provider_confidence,"
                "cp.provider_verification,cp.observed_at_utc,cp.source_first_seen_on,"
                "cp.source_last_seen_on,cp.still_on_page FROM contact_provenance cp "
                "JOIN contacts c ON c.id=cp.contact_id WHERE c.organization_id=? "
                "ORDER BY cp.observed_at_utc DESC", (row["organization_id"],))]
        for assessment in assessments:
            assessment["reasons"] = json.loads(assessment.pop("reasons_json"))
        return {"prospect": dict(row), "attempts": attempts, "assessments": assessments,
                "contacts": contacts, "contact_provenance": provenance}

    def run_loop(self, *, interval_seconds: int = 15) -> None:
        if interval_seconds <= 0:
            raise ValueError("positive interval required")
        while True:
            try:
                while self.consume_discovery_once() is not None:
                    pass
                while self.consume_internal_once() is not None:
                    pass
                self.tick()
                while self.work_once() is not None:
                    pass
                while self.consume_internal_once() is not None:
                    pass
            except PausedError:
                pass
            time.sleep(interval_seconds)
