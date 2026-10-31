"""Department 1 scheduling, execution, evidence commits and review surface."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from lumen_marketing_os.adapters.research import HackerNewsClient, HttpClient, SearchClient
from lumen_marketing_os.config import Settings
from lumen_marketing_os.core.errors import PausedError
from lumen_marketing_os.core.time import utc_now, utc_text
from lumen_marketing_os.db.database import Database, append_audit
from lumen_marketing_os.engine.queue import Job, JobQueue, OutgoingEvent
from lumen_marketing_os.policy.pause import PauseControl
from lumen_marketing_os.workflows.research.agents import DepartmentAgents, ResearchConfig, directory_relevant
from lumen_marketing_os.workflows.research.store import FindingDraft, ResearchStore, SourceResult, _domain

AGENTS = ("1A", "1B", "1C", "1E")
WORKFLOW = "department1.collect"
REVIEW_WORKFLOW = "department1.review"
REVIEW_ROLES = {"1A": "trend_explorer", "1B": "forum_researcher",
                "1C": "competitor_auditor", "1E": "directory_sieve"}


def load_local_env(workspace: Path) -> dict[str, str]:
    """Read a simple local .env without exporting secrets or logging their values."""
    values: dict[str, str] = {}
    path = workspace / ".env"
    if path.is_file():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            if name in {"BRAVE_SEARCH_API_KEY_1", "BRAVE_SEARCH_API_KEY_2",
                        "BRAVE_SEARCH_API_KEY_3",
                        "LMOS_SUPPRESSION_HMAC_KEY_HEX"}:
                values[name] = value.strip().strip('"').strip("'")
    return {key: os.environ.get(key, value) for key, value in values.items()} | {
        key: value for key in ("BRAVE_SEARCH_API_KEY_1", "BRAVE_SEARCH_API_KEY_2",
                               "BRAVE_SEARCH_API_KEY_3",
                               "LMOS_SUPPRESSION_HMAC_KEY_HEX")
        if (value := os.environ.get(key)) is not None
    }


def load_research_config(workspace: Path) -> tuple[ResearchConfig, dict[str, Any]]:
    data = json.loads((workspace / "config" / "research.json").read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError("unsupported research config version")
    max_items = data["max_items_per_source"]
    if type(max_items) is not int or not 1 <= max_items <= 50:
        raise ValueError("max_items_per_source must be 1..50")
    for section in ("feeds", "competitors"):
        if not isinstance(data[section], list):
            raise ValueError(f"{section} must be a list")
        for entry in data[section]:
            if not entry.get("id") or not entry.get("url", "").startswith("https://"):
                raise ValueError(f"invalid {section} entry")
    for community in data["reddit_communities"]:
        if not community or not all(c.isalnum() or c == "_" for c in community):
            raise ValueError("invalid Reddit community")
    schedule = data["schedule_hours"]
    if set(schedule) != set(AGENTS) or any(type(v) is not int or v < 1 for v in schedule.values()):
        raise ValueError("schedule_hours must include positive intervals for all four agents")
    config = ResearchConfig(tuple(data["queries"]), tuple(data["feeds"]),
                            tuple(data["reddit_communities"]), tuple(data["competitors"]),
                            tuple(data["directory_sources"]), tuple(data["keywords"]), max_items,
                            tuple(data.get("directory_queries", [])))
    return config, data


class ResearchRuntime:
    def __init__(self, *, settings: Settings, database: Database, pause: PauseControl,
                 queue: JobQueue, agents: DepartmentAgents | None = None,
                 clock: Callable = utc_now) -> None:
        self.settings = settings
        self.database = database
        self.pause = pause
        self.queue = queue
        self.clock = clock
        self.config, raw = load_research_config(settings.workspace)
        self.schedule_hours = raw["schedule_hours"]
        if agents is None:
            credentials = load_local_env(settings.workspace)
            http = HttpClient(user_agent=raw["user_agent"],
                              timeout=raw["request_timeout_seconds"],
                              max_bytes=raw["max_response_bytes"],
                              host_interval=raw["min_seconds_between_requests_per_host"])
            search = SearchClient(http, brave_keys=[credentials.get(f"BRAVE_SEARCH_API_KEY_{i}", "")
                                                     for i in (1, 2, 3)])
            agents = DepartmentAgents(http=http, search=search,
                                      hacker_news=HackerNewsClient(http), config=self.config)
        self.agents = agents

    def initialize_schedule(self) -> None:
        now = utc_text(self.clock())
        with self.database.transaction() as connection:
            for agent_id in AGENTS:
                connection.execute(
                    "INSERT OR IGNORE INTO schedule_definitions(id,workflow,lane,interval_seconds,"
                    "timezone,catch_up_policy,active,next_due_at_utc,config_version) "
                    "VALUES (?,?,?,?,'UTC','one',1,?,'research-v1')",
                    (f"department1.{agent_id}", WORKFLOW, f"research.{agent_id}",
                     self.schedule_hours[agent_id] * 3600, now),
                )

    def tick(self) -> list[Job]:
        """At most one durable job per agent/slot, even across repeated ticks."""
        self.pause.guard()
        self.initialize_schedule()
        now_dt = self.clock()
        now = utc_text(now_dt)
        with self.database.read() as connection:
            due = connection.execute(
                "SELECT id,lane,next_due_at_utc,interval_seconds FROM schedule_definitions "
                "WHERE workflow=? AND active=1 AND next_due_at_utc<=? ORDER BY id",
                (WORKFLOW, now),
            ).fetchall()
        jobs: list[Job] = []
        for row in due:
            agent_id = row["id"].rsplit(".", 1)[-1]
            slot = row["next_due_at_utc"]
            job = self.queue.enqueue(workflow=WORKFLOW, lane=row["lane"],
                                     payload={"agent_id": agent_id},
                                     logical_key=f"department1:{agent_id}:{slot}",
                                     max_attempts=self.settings.default_max_attempts)
            jobs.append(job)
            next_due = utc_text(now_dt + timedelta(seconds=row["interval_seconds"]))
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE schedule_definitions SET last_due_at_utc=?,next_due_at_utc=? "
                    "WHERE id=? AND next_due_at_utc=?", (slot, next_due, row["id"], slot),
                )
        return jobs

    def work_once(self, *, worker_id: str = "research-local", lease_seconds: int = 1800) -> dict | None:
        for agent_id in AGENTS:
            try:
                job = self.queue.claim(worker_id=worker_id,
                                       lease_seconds=lease_seconds, lane=f"research.{agent_id}")
            except PausedError:
                continue
            if job is None:
                continue
            if job.workflow != WORKFLOW or job.payload.get("agent_id") != agent_id:
                self.queue.fail(job_id=job.id, worker_id=worker_id,
                                error_class="invalid_research_job", retryable=False)
                return {"job_id": job.id, "status": "invalid_job"}
            return self._execute(job, worker_id)
        return None

    def _execute(self, job: Job, worker_id: str) -> dict:
        agent_id = job.payload["agent_id"]
        results = self.agents.collect(agent_id)
        errors = sum(bool(result.error_class) for result in results)
        now = utc_text(self.clock())
        if not results or errors == len(results):
            with self.database.transaction() as connection:
                store = ResearchStore(connection, now=now)
                for result in results:
                    store.health(agent_id=agent_id, result=result)
                connection.execute(
                    "INSERT INTO research_runs(id,job_id,agent_id,started_at_utc,finished_at_utc,"
                    "sources_checked,sources_failed,observations_new,findings_new,status) "
                    "VALUES (?,?,?,?,?,?,?,0,0,'failed') ON CONFLICT(job_id) DO UPDATE SET "
                    "finished_at_utc=excluded.finished_at_utc,sources_checked=excluded.sources_checked,"
                    "sources_failed=excluded.sources_failed,status='failed'",
                    (uuid4().hex, job.id, agent_id, now, now, len(results), errors),
                )
            self.queue.fail(job_id=job.id, worker_id=worker_id,
                            error_class="all_sources_unavailable", retryable=True)
            return {"job_id": job.id, "agent_id": agent_id, "sources_checked": len(results),
                    "sources_failed": errors, "observations_new": 0, "findings_new": 0,
                    "organizations_new": 0, "status": "retry_wait"}
        events: list[OutgoingEvent] = []
        counts = {"observations_new": 0, "findings_new": 0, "organizations_new": 0}

        def write(connection: sqlite3.Connection) -> None:
            store = ResearchStore(connection, now=now)
            for result in results:
                store.health(agent_id=agent_id, result=result)
                for item in result.items:
                    observation_id, is_new, previous = store.record_source(
                        agent_id=agent_id, item=item)
                    counts["observations_new"] += int(is_new)
                    if not is_new:
                        continue
                    draft = self.agents.draft(agent_id, item, previous=previous)
                    if draft is None:
                        continue
                    subject_domain = _domain(item.subject_url)
                    if agent_id == "1E" and subject_domain:
                        from dataclasses import replace
                        draft = replace(draft, subject_domain=subject_domain)
                    finding_id = store.record_finding(agent_id=agent_id,
                                                      observation_id=observation_id,
                                                      url=item.url, draft=draft)
                    if finding_id is None:
                        continue
                    counts["findings_new"] += 1
                    destination = {"1A": "strategy.review", "1B": "community.review",
                                   "1C": "strategy.review", "1E": "research.identity_review"}[agent_id]
                    payload = {"finding_id": finding_id, "agent_id": agent_id,
                               "evidence_url": item.url, "status": "needs_codex_review"}
                    events.append(OutgoingEvent(
                        event_type="research.finding_ready", aggregate_type="research_finding",
                        aggregate_id=finding_id, destination=destination, payload=payload,
                        deduplication_key=f"research:{finding_id}:{destination}"))
            status = "failed" if results and errors == len(results) else (
                "partial" if errors else "succeeded")
            connection.execute(
                "INSERT INTO research_runs(id,job_id,agent_id,started_at_utc,finished_at_utc,"
                "sources_checked,sources_failed,observations_new,findings_new,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
                "finished_at_utc=excluded.finished_at_utc,sources_checked=excluded.sources_checked,"
                "sources_failed=excluded.sources_failed,observations_new=excluded.observations_new,"
                "findings_new=excluded.findings_new,status=excluded.status",
                (uuid4().hex, job.id, agent_id, now, now, len(results), errors,
                 counts["observations_new"], counts["findings_new"], status),
            )
        self.queue.complete(job_id=job.id, worker_id=worker_id, events=events, mutate=write)
        return {"job_id": job.id, "agent_id": agent_id, "sources_checked": len(results),
                "sources_failed": errors, **counts,
                "status": "failed" if results and errors == len(results) else (
                    "partial" if errors else "succeeded")}

    def report(self, *, limit: int = 30) -> dict:
        with self.database.read() as connection:
            findings = connection.execute(
                "SELECT id,agent_id,finding_type,title,evidence_url,status,created_at_utc "
                "FROM research_findings ORDER BY created_at_utc DESC,rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
            health = connection.execute(
                "SELECT source_key,agent_id,status,error_class,item_count,last_checked_at_utc "
                "FROM research_source_health ORDER BY agent_id,source_key"
            ).fetchall()
            runs = connection.execute(
                "SELECT agent_id,status,sources_checked,sources_failed,observations_new,"
                "findings_new,finished_at_utc FROM research_runs ORDER BY finished_at_utc DESC LIMIT 12"
            ).fetchall()
        return {"findings": [dict(row) for row in findings],
                "source_health": [dict(row) for row in health],
                "recent_runs": [dict(row) for row in runs]}

    def finding(self, finding_id: str) -> dict:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT rf.*,so.source_type,so.content_hash,so.observed_at_utc "
                "FROM research_findings rf JOIN source_observations so "
                "ON so.id=rf.source_observation_id WHERE rf.id=?", (finding_id,),
            ).fetchone()
        if row is None:
            raise ValueError("research finding not found")
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        return result

    def import_reddit_observation(self, *, task_id: str, observation_id: str,
                                  worker_id: str, analysis: dict[str, str]) -> dict:
        """Atomically promote a leased 6A source observation to 1B review evidence.

        The browser operator supplies interpretation, never unsourced source text. The
        community observation is the evidence snapshot and remains independently auditable.
        """
        self.pause.guard("community")
        self.pause.guard("research.1B")
        if not worker_id.strip() or not isinstance(analysis, dict):
            raise ValueError("worker and analysis object required")
        required = ("post_date", "problem", "business_workflow", "workaround_or_consequence",
                    "buyer_evidence", "urgency_or_buying_signal", "fit_rationale",
                    "counter_evidence", "confidence", "confidence_reason", "next_step")
        if any(not isinstance(analysis.get(key), str) or not analysis[key].strip()
               or len(analysis[key]) > 1500 for key in required):
            raise ValueError("all bounded research analysis fields are required; use 'unknown' when absent")
        if analysis["confidence"] not in {"high", "medium", "low"}:
            raise ValueError("confidence must be high, medium or low")
        if analysis["next_step"] not in {"investigate_use_case", "verify_organization"}:
            raise ValueError("invalid next_step")
        now = utc_text(self.clock())
        with self.database.transaction() as connection:
            task = connection.execute(
                "SELECT agent_id,state,lease_owner,lease_expires_at_utc,input_json "
                "FROM community_tasks WHERE id=?", (task_id,)).fetchone()
            if (task is None or task["agent_id"] != "6A" or task["state"] != "leased"
                    or task["lease_owner"] != worker_id or task["lease_expires_at_utc"] <= now):
                raise ValueError("a live leased 6A task is required")
            account = connection.execute(
                "SELECT state,account_reference FROM community_connection_registry "
                "WHERE agent_id='6A' AND platform='reddit'").fetchone()
            expected = json.loads(task["input_json"]).get("account_reference")
            if (account is None or account["state"] != "ready" or not expected
                    or account["account_reference"] != expected):
                raise ValueError("6A Reddit account is not registered for this task")
            observation = connection.execute(
                "SELECT o.* FROM community_observations o "
                "JOIN community_task_observations t ON t.observation_id=o.id "
                "WHERE t.task_id=? AND o.id=? AND o.platform='reddit'",
                (task_id, observation_id)).fetchone()
            if observation is None:
                raise ValueError("Reddit observation is not linked to this task")
            parsed = urllib.parse.urlsplit(observation["canonical_url"])
            if (parsed.scheme != "https" or parsed.hostname not in {"reddit.com", "www.reddit.com"}
                    or "/comments/" not in parsed.path):
                raise ValueError("a canonical Reddit discussion permalink is required")
            item = self._reddit_source_item(observation)
            store = ResearchStore(connection, now=now)
            source_id, _, _ = store.record_source(agent_id="1B", item=item)
            metadata = {key: analysis[key].strip() for key in required}
            metadata.update({"community_task_id": task_id,
                             "community_observation_id": observation_id,
                             "community_observed_at_utc": observation["observed_at_utc"],
                             "source_key": "6A:authenticated_reddit"})
            draft = FindingDraft(
                "reddit_pain_signal", observation["title"], analysis["problem"],
                observation["excerpt"], metadata=metadata)
            finding_id = store.record_finding(agent_id="1B", observation_id=source_id,
                                              url=observation["canonical_url"], draft=draft)
            created = finding_id is not None
            if finding_id is not None:
                connection.execute(
                    "INSERT INTO outbox_events(id,event_type,schema_version,aggregate_type,"
                    "aggregate_id,destination,payload_json,deduplication_key,state,due_at_utc,"
                    "created_at_utc,updated_at_utc) VALUES (?,?,?,?,?,?,?,?, 'pending',?,?,?)",
                    (uuid4().hex, "research.finding_ready", 1, "research_finding", finding_id,
                     "strategy.review", json.dumps({"finding_id": finding_id, "agent_id": "1B",
                     "evidence_url": observation["canonical_url"], "status": "needs_codex_review"},
                     sort_keys=True), f"research:{finding_id}:strategy.review", now, now, now))
            else:
                row = connection.execute(
                    "SELECT id,metadata_json FROM research_findings WHERE agent_id='1B' AND "
                    "source_observation_id=? AND finding_type='reddit_pain_signal'",
                    (source_id,)).fetchone()
                if row is None or any(json.loads(row["metadata_json"]).get(key) != metadata[key]
                                      for key in required):
                    raise ValueError("research finding already exists with a different analysis")
                finding_id = row["id"]
            return {"finding_id": finding_id, "source_observation_id": source_id,
                    "created": created}

    @staticmethod
    def _reddit_source_item(observation: sqlite3.Row):
        from lumen_marketing_os.adapters.research import SourceItem
        return SourceItem("6A:authenticated_reddit", "authenticated_reddit",
                          observation["external_id"], observation["canonical_url"],
                          observation["title"], observation["excerpt"])

    def review(self, *, finding_id: str, reviewer: str, decision: str) -> None:
        if decision not in {"reviewed", "rejected"} or not reviewer.strip():
            raise ValueError("reviewer and reviewed/rejected decision required")
        now = utc_text(self.clock())
        with self.database.transaction() as connection:
            result = connection.execute(
                "UPDATE research_findings SET status=?,reviewed_at_utc=?,reviewer=? "
                "WHERE id=? AND status='needs_codex_review'",
                (decision, now, reviewer, finding_id),
            )
            if result.rowcount != 1:
                raise ValueError("finding absent or already reviewed")
            append_audit(connection, actor=reviewer, event_type=f"research.{decision}",
                         entity_type="research_finding", entity_id=finding_id,
                         occurred_at_utc=now)

    def dispatch_reviews(self, *, agent_id: str | None = None, limit: int = 10) -> list[dict]:
        """Idempotently assign unreviewed findings to the four Codex role lanes."""
        if agent_id is not None and agent_id not in REVIEW_ROLES:
            raise ValueError("unknown Department 1 review role")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        self.pause.guard()
        with self.database.read() as connection:
            query = ("SELECT id,agent_id FROM research_findings f "
                     "WHERE status='needs_codex_review' AND NOT EXISTS ("
                     "SELECT 1 FROM research_intake_screenings s "
                     "WHERE s.finding_id=f.id AND s.state='active') AND NOT EXISTS ("
                     "SELECT 1 FROM jobs j WHERE j.logical_key='research:review:' || f.id)")
            params: list[Any] = []
            if agent_id:
                query += " AND f.agent_id=?"
                params.append(agent_id)
            query += " ORDER BY f.created_at_utc,f.id LIMIT ?"
            params.append(limit)
            findings = [dict(row) for row in connection.execute(query, params)]
        assignments = []
        for row in findings:
            role = REVIEW_ROLES[row["agent_id"]]
            job = self.queue.enqueue(
                workflow=REVIEW_WORKFLOW, lane=f"research.review.{row['agent_id']}",
                payload={"finding_id": row["id"], "agent_id": row["agent_id"],
                         "role_name": role},
                logical_key=f"research:review:{row['id']}", max_attempts=3)
            assignments.append({"finding_id": row["id"], "agent_id": row["agent_id"],
                                "role_name": role, "job_id": job.id, "state": job.state})
        return assignments

    def claim_review(self, *, agent_id: str, worker_id: str,
                     lease_seconds: int = 1800) -> dict | None:
        if agent_id not in REVIEW_ROLES:
            raise ValueError("unknown Department 1 review role")
        self.pause.guard(f"research.{agent_id}")
        lane = f"research.review.{agent_id}"
        while True:
            job = self.queue.claim(worker_id=worker_id, lease_seconds=lease_seconds, lane=lane)
            if job is None:
                return None
            if (job.workflow != REVIEW_WORKFLOW or job.payload.get("agent_id") != agent_id
                    or job.payload.get("role_name") != REVIEW_ROLES[agent_id]):
                self.queue.fail(job_id=job.id, worker_id=worker_id,
                                error_class="invalid_review_job", retryable=False)
                continue
            finding = self.finding(job.payload["finding_id"])
            if finding["status"] != "needs_codex_review":
                self.queue.complete(job_id=job.id, worker_id=worker_id)
                continue
            return {"job_id": job.id, "finding": finding, "agent_id": agent_id,
                    "role_name": REVIEW_ROLES[agent_id], "attempt_count": job.attempt_count,
                    "lease_expires_at_utc": job.lease_expires_at_utc}

    def complete_review(self, *, job_id: str, worker_id: str, reviewer: str,
                        agent_run_ref: str, decision: str, assessment: dict) -> dict:
        if decision not in {"reviewed", "rejected"} or not reviewer.strip() or not agent_run_ref.strip():
            raise ValueError("decision, reviewer and actual agent run reference required")
        if not isinstance(assessment, dict):
            raise ValueError("assessment must be a JSON object")
        required = ("source_url", "source_checked_at_utc", "source_status", "evidence_summary",
                    "counter_evidence", "buyer_evidence", "product_fit", "rationale",
                    "confidence", "recommendation")
        if any(not isinstance(assessment.get(key), str) or not assessment[key].strip()
               or len(assessment[key]) > 3000 for key in required):
            raise ValueError("all bounded review assessment fields are required")
        if assessment["source_status"] != "verified" or assessment["confidence"] not in {
                "high", "medium", "low"}:
            raise ValueError("source must be verified and confidence high/medium/low")
        try:
            checked = datetime.fromisoformat(assessment["source_checked_at_utc"])
            checked_utc = utc_text(checked)
        except (ValueError, TypeError) as exc:
            raise ValueError("source_checked_at_utc must be timezone-aware ISO 8601") from exc
        if checked_utc > utc_text(self.clock() + timedelta(minutes=5)):
            raise ValueError("source check cannot be in the future")
        job = self.queue.get(job_id)
        if job.workflow != REVIEW_WORKFLOW or job.payload.get("agent_id") not in REVIEW_ROLES:
            raise ValueError("job is not a Department 1 review")
        agent_id = job.payload["agent_id"]
        self.pause.guard(f"research.{agent_id}")
        self.pause.guard(f"research.review.{agent_id}")
        now = utc_text(self.clock())
        finding_id = job.payload["finding_id"]
        encoded = json.dumps({key: assessment[key].strip() for key in required},
                             sort_keys=True, ensure_ascii=False)

        def write(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT agent_id,status,evidence_url FROM research_findings WHERE id=?",
                (finding_id,)).fetchone()
            if (row is None or row["agent_id"] != agent_id or row["status"] != "needs_codex_review"
                    or assessment["source_url"].strip() != row["evidence_url"]):
                raise ValueError("finding changed, source URL mismatched, or already reviewed")
            connection.execute(
                "UPDATE research_findings SET status=?,reviewer=?,reviewed_at_utc=? WHERE id=?",
                (decision, reviewer, now, finding_id))
            connection.execute(
                "INSERT INTO research_review_results(id,job_id,finding_id,agent_id,role_name,"
                "decision,assessment_json,source_checked_at_utc,reviewed_by,agent_run_ref,created_at_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (uuid4().hex, job_id, finding_id, agent_id, REVIEW_ROLES[agent_id], decision,
                 encoded, checked_utc, reviewer, agent_run_ref, now))
            append_audit(connection, actor=reviewer, event_type=f"research.review_{decision}",
                         entity_type="research_finding", entity_id=finding_id,
                         details_json=json.dumps({"job_id": job_id, "agent_run_ref": agent_run_ref}),
                         occurred_at_utc=now)
        self.queue.complete(job_id=job_id, worker_id=worker_id, mutate=write)
        return {"job_id": job_id, "finding_id": finding_id, "decision": decision,
                "agent_id": agent_id, "role_name": REVIEW_ROLES[agent_id]}

    def fail_review(self, *, job_id: str, worker_id: str, reason: str,
                    retryable: bool = True) -> dict:
        if not reason.strip() or len(reason) > 500:
            raise ValueError("bounded failure reason required")
        job = self.queue.get(job_id)
        if job.workflow != REVIEW_WORKFLOW:
            raise ValueError("job is not a Department 1 review")
        self.pause.guard(f"research.{job.payload['agent_id']}")
        result = self.queue.fail(job_id=job_id, worker_id=worker_id,
                                 error_class="research_source_unavailable" if retryable
                                 else "research_review_invalid",
                                 retryable=retryable, redacted_diagnostic=reason)
        return {"job_id": job_id, "finding_id": job.payload["finding_id"],
                "state": result.state, "reason": reason}

    def review_report(self, *, limit: int = 30) -> dict:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        with self.database.read() as connection:
            counts = [dict(row) for row in connection.execute(
                "SELECT json_extract(payload_json,'$.agent_id') agent_id,state,COUNT(*) count "
                "FROM jobs WHERE workflow=? GROUP BY agent_id,state ORDER BY agent_id,state",
                (REVIEW_WORKFLOW,))]
            jobs = [dict(row) for row in connection.execute(
                "SELECT j.id job_id,json_extract(j.payload_json,'$.finding_id') finding_id,"
                "json_extract(j.payload_json,'$.agent_id') agent_id,j.state,j.attempt_count,"
                "j.last_error_class,j.lease_expires_at_utc,r.decision,r.agent_run_ref "
                "FROM jobs j LEFT JOIN research_review_results r ON r.job_id=j.id "
                "WHERE j.workflow=? ORDER BY j.created_at_utc DESC LIMIT ?",
                (REVIEW_WORKFLOW, limit))]
            undispatched = [dict(row) for row in connection.execute(
                "SELECT f.agent_id,COUNT(*) count FROM research_findings f "
                "WHERE f.status='needs_codex_review' AND NOT EXISTS ("
                "SELECT 1 FROM research_intake_screenings s "
                "WHERE s.finding_id=f.id AND s.state='active') AND NOT EXISTS ("
                "SELECT 1 FROM jobs j WHERE j.logical_key='research:review:' || f.id) "
                "GROUP BY f.agent_id ORDER BY f.agent_id")]
            screened = connection.execute(
                "SELECT COUNT(*) FROM research_intake_screenings WHERE state='active'").fetchone()[0]
        return {"counts": counts, "undispatched": undispatched,
                "screened_out": screened, "jobs": jobs}

    def screen_legacy_directory(self, *, actor: str, limit: int = 100) -> list[dict]:
        """Quarantine old 1E headlines failing today's intake gate, without claiming source review."""
        if not actor.strip() or not 1 <= limit <= 100:
            raise ValueError("actor and limit 1..100 required")
        self.pause.guard("research.1E")
        now = utc_text(self.clock())
        screened: list[dict] = []
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT f.id,f.title,f.evidence_excerpt,e.id event_id FROM research_findings f "
                "JOIN outbox_events e ON e.aggregate_id=f.id AND e.event_type='research.finding_ready' "
                "AND e.destination='research.identity_review' AND e.state='pending' "
                "WHERE f.agent_id='1E' AND f.status='needs_codex_review' "
                "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.logical_key='research:review:' || f.id) "
                "AND NOT EXISTS (SELECT 1 FROM research_intake_screenings s "
                "WHERE s.finding_id=f.id) "
                "ORDER BY f.created_at_utc,f.id LIMIT 1000"
            ).fetchall()
            for row in rows:
                if len(screened) >= limit:
                    break
                if directory_relevant(row["title"], row["evidence_excerpt"], self.config.keywords):
                    continue
                reason = "Legacy 1E headline/excerpt lacks the configured business-data relevance terms; " \
                         "no original-source or buyer assessment was made"
                connection.execute(
                    "INSERT INTO research_intake_screenings(finding_id,state,rule_version,reason,"
                    "screened_by,screened_at_utc) VALUES (?,'active','1E-relevance-v1',?,?,?) "
                    "ON CONFLICT(finding_id) DO UPDATE SET state='active',rule_version='1E-relevance-v1',"
                    "reason=excluded.reason,screened_by=excluded.screened_by,"
                    "screened_at_utc=excluded.screened_at_utc,restored_by=NULL,restored_at_utc=NULL",
                    (row["id"], reason, actor, now))
                connection.execute(
                    "UPDATE outbox_events SET state='needs_review',last_error_class='intake_screened_out',"
                    "updated_at_utc=? WHERE id=? AND state='pending'", (now, row["event_id"]))
                append_audit(connection, actor=actor, event_type="research.intake_screened_out",
                             entity_type="research_finding", entity_id=row["id"],
                             details_json=json.dumps({"event_id": row["event_id"],
                                                      "rule_version": "1E-relevance-v1"}),
                             occurred_at_utc=now)
                screened.append({"finding_id": row["id"], "event_id": row["event_id"]})
        return screened

    def screening_report(self, *, limit: int = 100) -> dict:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        with self.database.read() as connection:
            counts = [dict(row) for row in connection.execute(
                "SELECT state,COUNT(*) count FROM research_intake_screenings GROUP BY state")]
            items = [dict(row) for row in connection.execute(
                "SELECT s.finding_id,s.state,s.reason,s.screened_at_utc,f.title,f.evidence_url "
                "FROM research_intake_screenings s JOIN research_findings f ON f.id=s.finding_id "
                "ORDER BY s.screened_at_utc DESC,s.finding_id LIMIT ?", (limit,))]
        return {"counts": counts, "items": items}

    def restore_screened_finding(self, *, finding_id: str, actor: str, reason: str) -> None:
        if not actor.strip() or len(reason.strip()) < 10:
            raise ValueError("actor and concrete reason required")
        self.pause.guard("research.1E")
        now = utc_text(self.clock())
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT e.id event_id FROM research_intake_screenings s "
                "JOIN research_findings f ON f.id=s.finding_id "
                "JOIN outbox_events e ON e.aggregate_id=f.id AND e.destination='research.identity_review' "
                "WHERE s.finding_id=? AND s.state='active' AND f.status='needs_codex_review' "
                "AND e.state='needs_review' AND e.last_error_class='intake_screened_out'",
                (finding_id,)).fetchone()
            if row is None:
                raise ValueError("active screened finding required")
            connection.execute(
                "UPDATE research_intake_screenings SET state='restored',restored_by=?,restored_at_utc=? "
                "WHERE finding_id=?", (actor, now, finding_id))
            connection.execute(
                "UPDATE outbox_events SET state='pending',last_error_class=NULL,updated_at_utc=? "
                "WHERE id=?", (now, row["event_id"]))
            append_audit(connection, actor=actor, event_type="research.intake_screen_restored",
                         entity_type="research_finding", entity_id=finding_id,
                         details_json=json.dumps({"reason": reason}), occurred_at_utc=now)

    def promote_organization(self, *, finding_id: str, name: str,
                             domain: str, reviewer: str) -> str:
        if not reviewer.strip() or not name.strip() or not domain.strip():
            raise ValueError("reviewer, company name and domain are required")
        domain = domain.lower().strip().removeprefix("www.")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", domain) or "." not in domain or ".." in domain:
            raise ValueError("domain must be a plain public hostname")
        now = utc_text(self.clock())
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT agent_id,finding_type,source_observation_id,evidence_url,"
                "subject_domain,status "
                "FROM research_findings WHERE id=?", (finding_id,),
            ).fetchone()
            if row is None or row["agent_id"] != "1E" or row["finding_type"] != "organization_candidate":
                raise ValueError("finding is not a 1E organization candidate")
            if row["status"] != "reviewed":
                raise ValueError("specialist-reviewed 1E finding required before promotion")
            if row["subject_domain"] != domain:
                raise ValueError("domain must match the sourced candidate website")
            store = ResearchStore(connection, now=now)
            organization_id = store.candidate_organization(
                name=name, domain=domain, observation_id=row["source_observation_id"])
            if organization_id is None:
                raise ValueError("organization domain already exists or candidate is invalid")
            connection.execute(
                "UPDATE research_findings SET status='reviewed',reviewer=COALESCE(reviewer,?),"
                "reviewed_at_utc=COALESCE(reviewed_at_utc,?),"
                "subject_name=?,subject_domain=? WHERE id=?",
                (reviewer, now, name, domain, finding_id),
            )
            connection.execute(
                "UPDATE research_handoff_intakes SET state='promoted',reviewer=?,reviewed_at_utc=? "
                "WHERE finding_id=? AND destination='research.identity_review' AND state='needs_review'",
                (reviewer, now, finding_id),
            )
            connection.execute(
                "INSERT INTO outbox_events(id,event_type,schema_version,aggregate_type,"
                "aggregate_id,destination,payload_json,deduplication_key,state,due_at_utc,"
                "created_at_utc,updated_at_utc) VALUES (?,?,?,?,?,?,?,?, 'pending',?,?,?)",
                (uuid4().hex, "research.organization_candidate_reviewed", 1, "organization",
                 organization_id, "qualification.discovery",
                 json.dumps({"finding_id": finding_id, "organization_id": organization_id,
                             "evidence_url": row["evidence_url"]}, sort_keys=True),
                 f"research:organization:{organization_id}:qualification.discovery", now, now, now),
            )
            append_audit(connection, actor=reviewer, event_type="research.organization_promoted",
                         entity_type="organization", entity_id=organization_id,
                         details_json=json.dumps({"finding_id": finding_id}),
                         occurred_at_utc=now)
        return organization_id

    def run_loop(self, *, worker_id: str = "research-local", interval_seconds: int = 30) -> None:
        while True:
            try:
                self.tick()
                while self.work_once(worker_id=worker_id) is not None:
                    pass
            except PausedError:
                pass
            time.sleep(interval_seconds)
