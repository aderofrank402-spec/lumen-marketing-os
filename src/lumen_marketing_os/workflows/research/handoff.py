"""Atomic, review-gated Department 1 finding handoffs.

No network or public action is performed here. A committed outbox receipt and its
internal downstream task/intake are written in one SQLite transaction, so a crash
before commit leaves the original event pending for a safe retry.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit
from uuid import uuid4

from lumen_marketing_os.core.errors import PausedError
from lumen_marketing_os.core.time import utc_now, utc_text
from lumen_marketing_os.db.database import Database, append_audit
from lumen_marketing_os.policy.pause import PauseControl

DESTINATIONS = ("strategy.review", "community.review", "research.identity_review")
DESTINATION_LANES = {"strategy.review": "discovery", "community.review": "community",
                     "research.identity_review": "research.identity"}
ROLE_DESTINATIONS = {"1A": {"strategy.review"}, "1B": {"strategy.review", "community.review"},
                     "1C": {"strategy.review"}, "1E": {"research.identity_review"}}


class ResearchHandoffRuntime:
    def __init__(self, database: Database, pause: PauseControl, guide_root: Path,
                 *, clock: Callable = utc_now) -> None:
        self.database, self.pause, self.guide_root, self.clock = database, pause, Path(guide_root), clock

    def _guard_locked(self, c: sqlite3.Connection, destination: str) -> None:
        self.pause.guard("research.handoff")
        scopes = ("global", "lane:research.handoff", f"lane:{DESTINATION_LANES[destination]}")
        if self.pause.pause_file.is_file() or c.execute(
            "SELECT 1 FROM system_controls WHERE paused=1 AND scope IN (?,?,?)", scopes
        ).fetchone():
            raise PausedError("research or downstream lane is paused")

    @staticmethod
    def _intake(c: sqlite3.Connection, finding_id: str, destination: str,
                reason: str, now: str) -> str:
        row = c.execute("SELECT id FROM research_handoff_intakes WHERE finding_id=? AND destination=?",
                        (finding_id, destination)).fetchone()
        if row:
            return row["id"]
        intake_id = uuid4().hex
        c.execute("INSERT INTO research_handoff_intakes(id,finding_id,destination,state,reason,created_at_utc) "
                  "VALUES (?,?,?,'needs_review',?,?)", (intake_id, finding_id, destination, reason, now))
        return intake_id

    @staticmethod
    def _strategy_task(c: sqlite3.Connection, finding_id: str, now: str) -> str:
        task_key = f"8A:research_finding:{finding_id}"
        row = c.execute("SELECT id FROM vertical_tasks WHERE task_key=?", (task_key,)).fetchone()
        if row:
            return row["id"]
        cycle_id = uuid4().hex
        c.execute("INSERT INTO vertical_cycles(id,cycle_key,started_at_utc,finished_at_utc,"
                  "signals_seen,signals_new,dispatched,status) VALUES (?,?,?,?,1,1,0,'complete')",
                  (cycle_id, f"research-handoff:{finding_id}", now, now))
        task_id = uuid4().hex
        c.execute("INSERT INTO vertical_tasks(id,task_key,cycle_id,agent_id,subject_type,"
                  "subject_id,state,created_at_utc,updated_at_utc) "
                  "VALUES (?,?,?,'8A','research_finding',?,'pending',?,?)",
                  (task_id, task_key, cycle_id, finding_id, now, now))
        return task_id

    def _community_target(self, c: sqlite3.Connection, finding: sqlite3.Row,
                          now: str) -> tuple[str, str, str, str]:
        if not self._original_post_is_fresh(finding["metadata_json"]):
            reason = "original forum post date is absent, invalid, or older than 90 days"
            return ("manual_review", "research_handoff_intake",
                    self._intake(c, finding["aggregate_id"], "community.review", reason, now), reason)
        host = (urlsplit(finding["evidence_url"]).hostname or "").lower()
        if host in {"reddit.com", "www.reddit.com"}:
            agent, platform, guide = "6A", "reddit", "6A_REDDIT.md"
        elif host == "news.ycombinator.com":
            agent, platform, guide = "6B", "hacker_news", "6B_HACKER_NEWS.md"
        else:
            reason = "unsupported forum surface; operator must choose an authorized channel"
            return ("manual_review", "research_handoff_intake",
                    self._intake(c, finding["aggregate_id"], "community.review", reason, now), reason)
        account = c.execute("SELECT account_reference FROM community_connection_registry "
                            "WHERE agent_id=? AND platform=? AND state='ready'",
                            (agent, platform)).fetchone()
        guide_path = self.guide_root / guide
        if not account or not guide_path.is_file():
            reason = "verified account or pinned role guide unavailable"
            return ("manual_review", "research_handoff_intake",
                    self._intake(c, finding["aggregate_id"], "community.review", reason, now), reason)
        task_key = f"research-handoff:{finding['aggregate_id']}"
        row = c.execute("SELECT id FROM community_tasks WHERE trigger_key=?", (task_key,)).fetchone()
        if row:
            task_id = row["id"]
        else:
            task_id = uuid4().hex
            payload = {"source_url": finding["evidence_url"], "platform": platform,
                       "account_reference": account["account_reference"],
                       "research_finding_id": finding["aggregate_id"],
                       "scope": "Read-only review of a specialist-reviewed Department 1 signal; internal draft only",
                       "policy_version": "research-handoff-v1"}
            if agent == "6A":
                payload["thread_url"] = finding["evidence_url"]
            c.execute("INSERT INTO community_tasks(id,agent_id,trigger_key,input_json,"
                      "instructions_sha256,state,due_at_utc,created_at_utc,updated_at_utc) "
                      "VALUES (?,?,?,?,?,'pending',?,?,?)",
                      (task_id, agent, task_key, json.dumps(payload, sort_keys=True),
                       hashlib.sha256(guide_path.read_bytes()).hexdigest(), now, now, now))
        return ("routed", "community_task", task_id, f"read-only {agent} task created")

    def _original_post_is_fresh(self, metadata_json: str) -> bool:
        metadata = json.loads(metadata_json)
        published_text = metadata.get("published_at") or metadata.get("post_date")
        try:
            published = datetime.fromisoformat(published_text.replace("Z", "+00:00"))
            if published.tzinfo is None:
                raise ValueError("missing source timezone")
            age = self.clock().astimezone(timezone.utc) - published.astimezone(timezone.utc)
            return timedelta(0) <= age <= timedelta(days=90)
        except (AttributeError, TypeError, ValueError):
            return False

    def work_once(self) -> dict | None:
        self.pause.guard("research.handoff")
        now = utc_text(self.clock())
        with self.database.transaction() as c:
            row = c.execute(
                "SELECT e.*,f.agent_id,f.status finding_status,f.evidence_url,"
                "f.reviewed_at_utc,f.metadata_json "
                "FROM outbox_events e JOIN research_findings f ON f.id=e.aggregate_id "
                "WHERE e.event_type='research.finding_ready' AND e.destination IN (?,?,?) "
                "AND e.state IN ('pending','retry_wait') AND e.due_at_utc<=? "
                "AND f.status IN ('reviewed','rejected') "
                "ORDER BY e.due_at_utc,e.created_at_utc,e.id LIMIT 1",
                (*DESTINATIONS, now)).fetchone()
            if row is None:
                return None
            destination = row["destination"]
            self._guard_locked(c, destination)
            disposition, target_type, target_id, reason = "quarantined", None, None, "invalid handoff envelope"
            try:
                payload = json.loads(row["payload_json"])
                parsed = urlsplit(row["evidence_url"])
                valid = (row["schema_version"] == 1 and row["aggregate_type"] == "research_finding"
                         and row["agent_id"] in ROLE_DESTINATIONS
                         and destination in ROLE_DESTINATIONS[row["agent_id"]]
                         and isinstance(payload, dict)
                         and payload.get("finding_id") == row["aggregate_id"]
                         and payload.get("agent_id") == row["agent_id"]
                         and payload.get("evidence_url") == row["evidence_url"]
                         and payload.get("status") == "needs_codex_review"
                         and parsed.scheme == "https" and bool(parsed.hostname)
                         and row["reviewed_at_utc"] is not None)
            except (ValueError, TypeError):
                valid = False
            if valid and row["finding_status"] == "rejected":
                disposition, reason = "rejected", "specialist rejected finding; no downstream task"
            elif valid and destination == "strategy.review":
                disposition, target_type, target_id, reason = (
                    "routed", "vertical_task", self._strategy_task(c, row["aggregate_id"], now),
                    "8A evidence-review task created")
            elif valid and destination == "community.review":
                disposition, target_type, target_id, reason = self._community_target(c, row, now)
            elif valid and destination == "research.identity_review":
                reason = "specialist-reviewed candidate requires operator identity verification"
                disposition, target_type, target_id = (
                    "manual_review", "research_handoff_intake",
                    self._intake(c, row["aggregate_id"], destination, reason, now))
            receipt_id = uuid4().hex
            c.execute("INSERT INTO research_handoff_receipts(id,outbox_event_id,finding_id,destination,"
                      "disposition,target_type,target_id,reason,created_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                      (receipt_id, row["id"], row["aggregate_id"], destination,
                       disposition, target_type, target_id, reason, now))
            if disposition == "quarantined":
                c.execute("UPDATE outbox_events SET state='needs_review',last_error_class='invalid_research_handoff',"
                          "updated_at_utc=? WHERE id=?", (now, row["id"]))
            else:
                c.execute("UPDATE outbox_events SET state='delivered',lease_owner=NULL,"
                          "lease_expires_at_utc=NULL,updated_at_utc=? WHERE id=?", (now, row["id"]))
            append_audit(c, actor="research-handoff", event_type=f"research.handoff_{disposition}",
                         entity_type="outbox_event", entity_id=row["id"],
                         details_json=json.dumps({"receipt_id": receipt_id, "target_id": target_id}),
                         occurred_at_utc=now)
            return {"event_id": row["id"], "finding_id": row["aggregate_id"],
                    "destination": destination, "disposition": disposition,
                    "target_type": target_type, "target_id": target_id, "receipt_id": receipt_id}

    def run(self, *, limit: int = 20) -> list[dict]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        results = []
        for _ in range(limit):
            result = self.work_once()
            if result is None:
                break
            results.append(result)
        return results

    def report(self) -> dict:
        with self.database.read() as c:
            eligible = [dict(row) for row in c.execute(
                "SELECT e.destination,f.status,COUNT(*) count FROM outbox_events e "
                "JOIN research_findings f ON f.id=e.aggregate_id "
                "WHERE e.event_type='research.finding_ready' AND e.destination IN (?,?,?) "
                "AND e.state IN ('pending','retry_wait') GROUP BY e.destination,f.status "
                "ORDER BY e.destination,f.status", DESTINATIONS)]
            receipts = [dict(row) for row in c.execute(
                "SELECT destination,disposition,COUNT(*) count FROM research_handoff_receipts "
                "GROUP BY destination,disposition ORDER BY destination,disposition")]
            intakes = [dict(row) for row in c.execute(
                "SELECT i.id,i.finding_id,i.destination,i.state,i.reason,f.title,f.evidence_url "
                "FROM research_handoff_intakes i JOIN research_findings f ON f.id=i.finding_id "
                "WHERE i.state='needs_review' ORDER BY i.created_at_utc LIMIT 50")]
        return {"pending_by_status": eligible, "receipts": receipts, "manual_intakes": intakes}

    def reconcile_undated_community_tasks(self) -> list[dict]:
        """Quarantine pending tasks routed before original-post freshness was enforced."""
        self.pause.guard("research.handoff")
        now = utc_text(self.clock())
        changed = []
        with self.database.transaction() as c:
            self._guard_locked(c, "community.review")
            rows = c.execute(
                "SELECT r.id receipt_id,r.finding_id,r.target_id task_id,f.metadata_json "
                "FROM research_handoff_receipts r "
                "JOIN research_findings f ON f.id=r.finding_id "
                "JOIN community_tasks t ON t.id=r.target_id "
                "WHERE r.destination='community.review' AND r.disposition='routed' "
                "AND r.target_type='community_task' AND t.state='pending' "
                "ORDER BY r.created_at_utc,r.id"
            ).fetchall()
            for row in rows:
                if self._original_post_is_fresh(row["metadata_json"]):
                    continue
                reason = "original forum post date is absent, invalid, or older than 90 days"
                intake_id = self._intake(c, row["finding_id"], "community.review", reason, now)
                c.execute("UPDATE community_tasks SET state='blocked',last_error_class=?,updated_at_utc=? "
                          "WHERE id=? AND state='pending'",
                          ("source_date_unverified", now, row["task_id"]))
                c.execute("UPDATE research_handoff_receipts SET disposition='manual_review',"
                          "target_type='research_handoff_intake',target_id=?,reason=? WHERE id=?",
                          (intake_id, reason, row["receipt_id"]))
                append_audit(c, actor="research-handoff", event_type="research.handoff_source_date_quarantined",
                             entity_type="research_handoff_receipt", entity_id=row["receipt_id"],
                             details_json=json.dumps({"task_id": row["task_id"], "intake_id": intake_id}),
                             occurred_at_utc=now)
                changed.append({"receipt_id": row["receipt_id"], "task_id": row["task_id"],
                                "intake_id": intake_id})
        return changed

    def dismiss_intake(self, *, intake_id: str, reviewer: str, reason: str) -> None:
        self.pause.guard("research.handoff")
        if not reviewer.strip() or len(reason.strip()) < 10:
            raise ValueError("reviewer and concrete reason required")
        now = utc_text(self.clock())
        with self.database.transaction() as c:
            if not c.execute("UPDATE research_handoff_intakes SET state='dismissed',reviewer=?,"
                             "reason=?,reviewed_at_utc=? WHERE id=? AND state='needs_review'",
                             (reviewer, reason, now, intake_id)).rowcount:
                raise ValueError("reviewable intake required")
            append_audit(c, actor=reviewer, event_type="research.handoff_intake_dismissed",
                         entity_type="research_handoff_intake", entity_id=intake_id,
                         details_json=json.dumps({"reason": reason}), occurred_at_utc=now)

    def retry_community_intake(self, *, intake_id: str, actor: str) -> dict:
        """Route a previously manual forum handoff after its authorized path is ready."""
        if not actor.strip():
            raise ValueError("actor required")
        now = utc_text(self.clock())
        with self.database.transaction() as c:
            self._guard_locked(c, "community.review")
            row = c.execute("SELECT i.finding_id,f.evidence_url,f.metadata_json FROM research_handoff_intakes i "
                            "JOIN research_findings f ON f.id=i.finding_id "
                            "WHERE i.id=? AND i.destination='community.review' AND i.state='needs_review' "
                            "AND f.status='reviewed'", (intake_id,)).fetchone()
            if row is None:
                raise ValueError("reviewed community intake required")
            finding = {"aggregate_id": row["finding_id"], "evidence_url": row["evidence_url"],
                       "metadata_json": row["metadata_json"]}
            disposition, target_type, target_id, reason = self._community_target(c, finding, now)
            if disposition == "routed":
                c.execute("UPDATE research_handoff_intakes SET state='routed',target_type=?,target_id=?,"
                          "reason=?,reviewer=?,reviewed_at_utc=? WHERE id=?",
                          (target_type, target_id, reason, actor, now, intake_id))
                append_audit(c, actor=actor, event_type="research.handoff_intake_routed",
                             entity_type="research_handoff_intake", entity_id=intake_id,
                             details_json=json.dumps({"target_id": target_id}), occurred_at_utc=now)
            return {"intake_id": intake_id, "disposition": disposition,
                    "target_type": target_type, "target_id": target_id, "reason": reason}
