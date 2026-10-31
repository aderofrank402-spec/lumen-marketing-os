"""Durable Codex handoffs for Department 6's browser-facing roles.

The local process cannot silently log into or spawn Codex browser sessions. It
records due tasks, evidence and drafts; an authorized Codex run claims a task,
uses its own connected session, and submits results through this boundary.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from lumen_marketing_os.core.errors import ConflictError, PausedError
from lumen_marketing_os.core.time import Clock, parse_utc, utc_now, utc_text
from lumen_marketing_os.db.database import Database, append_audit
from lumen_marketing_os.policy.pause import PauseControl

GUIDES = {"6A": "6A_REDDIT.md", "6B": "6B_HACKER_NEWS.md", "6C": "6C_LAUNCHPADS.md",
          "6D": "6D_LINKEDIN.md", "6E": "6E_PROSPECT_INDEXER.md"}
PLATFORMS = {"6A": {"reddit"}, "6B": {"hacker_news"},
             "6C": {"product_hunt", "launchpad"}, "6D": {"linkedin"}, "6E": {"directory"}}
ARTIFACTS = {"6A": {"helpful_reply"},
             "6B": {"technical_article", "technical_comment"},
             "6C": {"launch_asset", "launch_comment"},
             "6D": {"linkedin_insight", "linkedin_response"}, "6E": set()}
LAUNCH_ITEMS = {
    "positioning": "Target problem, audience and positioning approved",
    "claims": "Every product/customer claim has approved evidence",
    "assets": "Listing copy and visual assets reviewed",
    "demo": "Working demo or screenshot validated",
    "faq": "FAQ and expected objections prepared",
    "tracking": "Attribution and signup tracking checked",
    "support": "Launch-day response owner and support path assigned",
    "window": "Launch window and platform/account confirmed",
}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _https_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("evidence must be an HTTPS URL without embedded credentials")
    return value


def _host_matches(url: str, suffix: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host == suffix or host.endswith("." + suffix)


class CommunityRuntime:
    def __init__(self, database: Database, pause: PauseControl, guides_dir: Path,
                 *, clock: Clock = utc_now) -> None:
        self.database, self.pause, self.guides_dir, self.clock = database, pause, Path(guides_dir), clock

    def _guide(self, agent_id: str) -> tuple[str, str]:
        if agent_id not in GUIDES:
            raise ValueError("unknown Department 6 agent")
        path = (self.guides_dir / GUIDES[agent_id]).resolve()
        data = path.read_bytes()
        if not data:
            raise ValueError("agent guide is empty")
        return str(path), hashlib.sha256(data).hexdigest()

    def configure_trigger(self, *, trigger_id: str, agent_id: str, trigger_kind: str,
                          owner: str, input_payload: dict, interval_seconds: int | None = None,
                          next_due_at: datetime | None = None, active: bool = False,
                          expected_version: str | None = None) -> str:
        self._guide(agent_id)
        if not trigger_id.strip() or not owner.strip() or not isinstance(input_payload, dict):
            raise ValueError("trigger ID, owner and JSON input are required")
        if trigger_kind not in {"interval", "event", "manual"}:
            raise ValueError("invalid trigger kind")
        if trigger_kind == "interval":
            if interval_seconds is None or interval_seconds <= 0 or next_due_at is None:
                raise ValueError("interval trigger needs positive interval and next due time")
        elif interval_seconds is not None or next_due_at is not None:
            raise ValueError("non-interval trigger cannot have interval or next due time")
        due = utc_text(next_due_at) if next_due_at else None
        policy = {"agent": agent_id, "kind": trigger_kind, "owner": owner,
                  "input": input_payload, "interval": interval_seconds, "first_due": due}
        version = hashlib.sha256(_json(policy).encode()).hexdigest()[:24]
        now = utc_text(self.clock())
        with self.database.transaction() as c:
            old = c.execute("SELECT config_version FROM community_trigger_definitions WHERE id=?",
                            (trigger_id,)).fetchone()
            if old:
                if old["config_version"] != expected_version:
                    raise ConflictError("trigger changed; supply current version")
                c.execute("UPDATE community_trigger_definitions SET agent_id=?,trigger_kind=?,"
                          "interval_seconds=?,next_due_at_utc=?,active=?,input_json=?,"
                          "config_version=?,owner=?,updated_at_utc=? WHERE id=?",
                          (agent_id, trigger_kind, interval_seconds, due, int(active),
                           _json(input_payload), version, owner, now, trigger_id))
            else:
                if expected_version is not None:
                    raise ConflictError("trigger does not exist")
                c.execute("INSERT INTO community_trigger_definitions VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (trigger_id, agent_id, trigger_kind, interval_seconds, due, int(active),
                           _json(input_payload), version, owner, now))
            append_audit(c, actor=owner, event_type="community.trigger_configured",
                         entity_type="community_trigger", entity_id=trigger_id,
                         details_json=_json({"agent_id": agent_id, "version": version,
                                             "active": active}), occurred_at_utc=now)
        return version

    def set_trigger_active(self, trigger_id: str, *, active: bool, actor: str) -> None:
        if not actor.strip():
            raise ValueError("actor is required")
        now = utc_text(self.clock())
        with self.database.transaction() as c:
            if not c.execute("UPDATE community_trigger_definitions SET active=?,updated_at_utc=? "
                             "WHERE id=?", (int(active), now, trigger_id)).rowcount:
                raise ValueError("trigger not found")
            append_audit(c, actor=actor, event_type="community.trigger_activated" if active
                         else "community.trigger_deactivated", entity_type="community_trigger",
                         entity_id=trigger_id, occurred_at_utc=now)

    def _create_task_locked(self, c: sqlite3.Connection, *, agent_id: str,
                            trigger_key: str, payload: dict, due: str,
                            trigger_id: str | None = None) -> str:
        _, guide_hash = self._guide(agent_id)
        required = {"6A": ("thread_url", "scope"), "6B": ("topic", "source_url"),
                    "6D": ("topic", "source_url")}
        if agent_id in required and not any(isinstance(payload.get(key), str) and
                                            payload[key].strip() for key in required[agent_id]):
            raise ValueError(f"{agent_id} trigger needs a source URL, topic or scope")
        if agent_id == "6C" and payload.get("platform") not in PLATFORMS[agent_id]:
            raise ValueError("6C trigger needs a supported platform")
        if agent_id == "6C" and payload.get("mode") == "listing_audit":
            if not isinstance(payload.get("source_url"), str):
                raise ValueError("6C listing audit needs a source URL")
            if payload["platform"] == "product_hunt" and not _host_matches(
                payload["source_url"], "producthunt.com"
            ):
                raise ValueError("Product Hunt audit source must be on producthunt.com")
        elif agent_id == "6C" and not isinstance(payload.get("launch_at"), str):
            raise ValueError("6C launch preparation needs launch_at")
        if agent_id == "6C" and payload.get("mode") != "listing_audit":
            utc_text(datetime.fromisoformat(payload["launch_at"].replace("Z", "+00:00")))
        if agent_id == "6E" and not (isinstance(payload.get("source_name"), str) and
                                      isinstance(payload.get("source_url"), str)):
            raise ValueError("6E trigger needs source_name and source_url")
        for field in ("thread_url", "source_url"):
            if field in payload:
                if not isinstance(payload[field], str):
                    raise ValueError(f"{field} must be an HTTPS URL")
                _https_url(payload[field])
        encoded = _json(payload)
        old = c.execute("SELECT id,agent_id,input_json FROM community_tasks WHERE trigger_key=?",
                        (trigger_key,)).fetchone()
        if old:
            if old["agent_id"] != agent_id or old["input_json"] != encoded:
                raise ConflictError("trigger key already has different content")
            return old["id"]
        now, task_id = utc_text(self.clock()), uuid4().hex
        c.execute("INSERT INTO community_tasks(id,agent_id,trigger_id,trigger_key,input_json,"
                  "instructions_sha256,state,due_at_utc,created_at_utc,updated_at_utc) "
                  "VALUES (?,?,?,?,?,?,'pending',?,?,?)",
                  (task_id, agent_id, trigger_id, trigger_key, encoded, guide_hash, due, now, now))
        if agent_id == "6C" and payload.get("mode") != "listing_audit":
            for key, description in LAUNCH_ITEMS.items():
                c.execute("INSERT INTO community_launch_items(id,task_id,item_key,description,state,updated_at_utc) "
                          "VALUES (?,?,?,?,'open',?)",
                          (uuid4().hex, task_id, key, description, now))
        append_audit(c, actor="community", event_type="community.task_created",
                     entity_type="community_task", entity_id=task_id,
                     details_json=_json({"agent_id": agent_id, "trigger_key": trigger_key}),
                     occurred_at_utc=now)
        return task_id

    def trigger(self, *, agent_id: str, trigger_key: str, input_payload: dict,
                trigger_id: str | None = None) -> str:
        self.pause.guard("community")
        if not trigger_key.strip() or not isinstance(input_payload, dict):
            raise ValueError("trigger key and JSON input required")
        now = utc_text(self.clock())
        with self.database.transaction() as c:
            if self.pause.pause_file.is_file() or c.execute(
                "SELECT 1 FROM system_controls WHERE paused=1 AND scope IN ('global','lane:community')"
            ).fetchone():
                raise PausedError("community work is paused")
            if trigger_id:
                definition = c.execute("SELECT * FROM community_trigger_definitions WHERE id=?",
                                       (trigger_id,)).fetchone()
                if definition is None or not definition["active"] or definition["agent_id"] != agent_id:
                    raise ValueError("active matching trigger required")
                if definition["trigger_kind"] == "interval":
                    raise ValueError("interval triggers are timer-owned")
            return self._create_task_locked(c, agent_id=agent_id, trigger_key=trigger_key,
                                            payload=input_payload, due=now, trigger_id=trigger_id)

    def tick(self) -> list[dict]:
        self.pause.guard("community")
        now_dt = self.clock()
        now = utc_text(now_dt)
        result: list[dict] = []
        with self.database.transaction() as c:
            if self.pause.pause_file.is_file() or c.execute(
                "SELECT 1 FROM system_controls WHERE paused=1 AND scope IN ('global','lane:community')"
            ).fetchone():
                raise PausedError("community work is paused")
            rows = c.execute("SELECT * FROM community_trigger_definitions WHERE active=1 "
                             "AND trigger_kind='interval' AND next_due_at_utc<=? "
                             "ORDER BY next_due_at_utc,id", (now,)).fetchall()
            for row in rows:
                first = parse_utc(row["next_due_at_utc"])
                missed = int((now_dt - first).total_seconds() // row["interval_seconds"]) + 1
                due = first + timedelta(seconds=(missed - 1) * row["interval_seconds"])
                occurrence = utc_text(due)
                payload = json.loads(row["input_json"])
                task_id = self._create_task_locked(
                    c, agent_id=row["agent_id"], trigger_key=f"timer:{row['id']}:{occurrence}",
                    payload=payload, due=occurrence, trigger_id=row["id"])
                c.execute("UPDATE community_trigger_definitions SET next_due_at_utc=? WHERE id=?",
                          (utc_text(first + timedelta(seconds=missed * row["interval_seconds"])),
                           row["id"]))
                result.append({"trigger_id": row["id"], "task_id": task_id,
                               "agent_id": row["agent_id"], "due_at_utc": occurrence})
        return result

    def connection(self, *, agent_id: str, state: str, platform: str,
                   actor: str, account_reference: str | None = None,
                   failure_class: str | None = None, verified: bool = False) -> None:
        self._guide(agent_id)
        if state not in {"not_connected", "ready", "blocked"} or platform not in PLATFORMS[agent_id]:
            raise ValueError("invalid connection state or platform")
        if not actor.strip() or state == "ready" and (not account_reference or not verified):
            raise ValueError("actor and ready-account reference are required")
        now = utc_text(self.clock())
        with self.database.transaction() as c:
            c.execute("INSERT INTO community_connection_registry(agent_id,platform,account_reference,state,"
                      "last_verified_at_utc,failure_class,updated_at_utc) VALUES (?,?,?,?,?,?,?) "
                      "ON CONFLICT(agent_id,platform) DO UPDATE SET "
                      "account_reference=excluded.account_reference,state=excluded.state,"
                      "last_verified_at_utc=excluded.last_verified_at_utc,"
                      "failure_class=excluded.failure_class,updated_at_utc=excluded.updated_at_utc",
                      (agent_id, platform, account_reference, state,
                       now if state == "ready" else None, failure_class, now))
            append_audit(c, actor=actor, event_type="community.connection_status",
                         entity_type="community_connection", entity_id=agent_id,
                         details_json=_json({"state": state, "platform": platform}),
                         occurred_at_utc=now)

    def claim(self, *, agent_id: str, worker_id: str, lease_seconds: int = 900) -> dict | None:
        self.pause.guard("community")
        if not worker_id.strip() or lease_seconds <= 0:
            raise ValueError("worker and positive lease required")
        now_dt = self.clock()
        now = utc_text(now_dt)
        with self.database.transaction() as c:
            if self.pause.pause_file.is_file() or c.execute(
                "SELECT 1 FROM system_controls WHERE paused=1 AND scope IN ('global','lane:community')"
            ).fetchone():
                raise PausedError("community work is paused")
            c.execute("UPDATE community_tasks SET state='pending',lease_owner=NULL,lease_expires_at_utc=NULL,"
                      "updated_at_utc=? WHERE agent_id=? AND state='leased' AND lease_expires_at_utc<=?",
                      (now, agent_id, now))
            row = c.execute("SELECT * FROM community_tasks WHERE agent_id=? AND state='pending' "
                            "AND due_at_utc<=? ORDER BY due_at_utc,created_at_utc LIMIT 1",
                            (agent_id, now)).fetchone()
            if row is None:
                return None
            payload = json.loads(row["input_json"])
            target_platform = payload.get("platform")
            if target_platform in PLATFORMS[agent_id]:
                connection = c.execute("SELECT state,account_reference FROM community_connection_registry "
                                       "WHERE agent_id=? AND platform=?", (agent_id, target_platform)).fetchone()
            else:
                connection = c.execute("SELECT state,account_reference FROM community_connection_registry WHERE agent_id=? "
                                       "AND state='ready' LIMIT 1", (agent_id,)).fetchone()
            if not connection or connection["state"] != "ready":
                raise ValueError("agent connection has not been verified ready")
            expected_account = payload.get("account_reference")
            if expected_account and expected_account != connection["account_reference"]:
                raise ValueError("task account reference differs from verified connection")
            _, current_hash = self._guide(agent_id)
            if row["instructions_sha256"] != current_hash:
                c.execute("UPDATE community_tasks SET state='blocked',last_error_class='guide_changed',"
                          "updated_at_utc=? WHERE id=?", (now, row["id"]))
                return {"task_id": row["id"], "state": "blocked", "reason": "guide_changed"}
            expires = utc_text(now_dt + timedelta(seconds=lease_seconds))
            c.execute("UPDATE community_tasks SET state='leased',lease_owner=?,lease_expires_at_utc=?,"
                      "attempt_count=attempt_count+1,updated_at_utc=? WHERE id=?",
                      (worker_id, expires, now, row["id"]))
            return self.brief_locked(c, row["id"])

    def brief_locked(self, c: sqlite3.Connection, task_id: str) -> dict:
        row = c.execute("SELECT * FROM community_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("task not found")
        guide_path, _ = self._guide(row["agent_id"])
        observations = [dict(item) for item in c.execute(
            "SELECT o.* FROM community_observations o JOIN community_task_observations t "
            "ON t.observation_id=o.id WHERE t.task_id=?", (task_id,))]
        drafts = [dict(item) for item in c.execute(
            "SELECT id,artifact_type,state,body_sha256,created_at_utc FROM community_drafts WHERE task_id=?",
            (task_id,))]
        checklist = [dict(item) for item in c.execute(
            "SELECT item_key,description,state,evidence_ref FROM community_launch_items WHERE task_id=? "
            "ORDER BY item_key", (task_id,))]
        return {"task_id": row["id"], "agent_id": row["agent_id"], "state": row["state"],
                "trigger_key": row["trigger_key"], "input": json.loads(row["input_json"]),
                "guide_path": guide_path, "guide_sha256": row["instructions_sha256"],
                "lease_owner": row["lease_owner"], "lease_expires_at_utc": row["lease_expires_at_utc"],
                "observations": observations, "drafts": drafts, "launch_checklist": checklist}

    def brief(self, task_id: str) -> dict:
        with self.database.read() as c:
            return self.brief_locked(c, task_id)

    def draft_detail(self, draft_id: str) -> dict:
        with self.database.read() as c:
            row = c.execute("SELECT id,task_id,observation_id,artifact_type,body,evidence_json,"
                            "body_sha256,state,author,created_at_utc,reviewer,review_note,reviewed_at_utc "
                            "FROM community_drafts WHERE id=?", (draft_id,)).fetchone()
            if row is None:
                raise ValueError("draft not found")
            result = dict(row)
            result["evidence_urls"] = json.loads(result.pop("evidence_json"))
            return result

    def _owned(self, c: sqlite3.Connection, task_id: str, worker_id: str) -> sqlite3.Row:
        row = c.execute("SELECT * FROM community_tasks WHERE id=?", (task_id,)).fetchone()
        if (row is None or row["state"] != "leased" or row["lease_owner"] != worker_id
                or row["lease_expires_at_utc"] <= utc_text(self.clock())):
            raise ValueError("worker does not hold a live task lease")
        return row

    def observe(self, *, task_id: str, worker_id: str, platform: str,
                canonical_url: str, title: str, excerpt: str,
                observed_at: datetime, relevance_note: str,
                external_id: str | None = None) -> str:
        self.pause.guard("community")
        _https_url(canonical_url)
        if not title.strip() or not excerpt.strip() or not relevance_note.strip():
            raise ValueError("title, excerpt and relevance note required")
        if len(title) > 500 or len(excerpt) > 5000 or len(relevance_note) > 1500:
            raise ValueError("observation exceeds size limit")
        hosts = {"reddit": "reddit.com", "hacker_news": "news.ycombinator.com",
                 "product_hunt": "producthunt.com", "linkedin": "linkedin.com"}
        if platform in hosts and not _host_matches(canonical_url, hosts[platform]):
            raise ValueError("source URL does not match platform")
        now, observed = utc_text(self.clock()), utc_text(observed_at)
        digest = hashlib.sha256((title + "\n" + excerpt).encode()).hexdigest()
        with self.database.transaction() as c:
            task = self._owned(c, task_id, worker_id)
            if platform not in PLATFORMS[task["agent_id"]]:
                raise ValueError("platform does not match agent")
            prior = c.execute("SELECT * FROM community_observations WHERE platform=? AND canonical_url=?",
                              (platform, canonical_url)).fetchone()
            if prior:
                if external_id and prior["external_id"] not in (None, external_id):
                    raise ConflictError("source ID conflicts with existing URL")
                observation_id = prior["id"]
                c.execute("UPDATE community_observations SET title=?,excerpt=?,observed_at_utc=?,"
                          "captured_at_utc=?,captured_by=? WHERE id=?",
                          (title, excerpt, observed, now, worker_id, observation_id))
            else:
                observation_id = uuid4().hex
                c.execute("INSERT INTO community_observations VALUES (?,?,?,?,?,?,?,?,?)",
                          (observation_id, platform, external_id, canonical_url, title, excerpt,
                           observed, now, worker_id))
            c.execute("INSERT OR IGNORE INTO community_observation_versions VALUES (?,?,?,?,?,?,?)",
                      (uuid4().hex, observation_id, digest, title, excerpt, observed, now))
            c.execute("INSERT OR IGNORE INTO community_task_observations VALUES (?,?,?)",
                      (task_id, observation_id, relevance_note))
            append_audit(c, actor=worker_id, event_type="community.observed",
                         entity_type="community_observation", entity_id=observation_id,
                         occurred_at_utc=now)
        return observation_id

    def draft(self, *, task_id: str, worker_id: str, artifact_type: str,
              body: str, evidence_urls: list[str],
              observation_id: str | None = None) -> str:
        self.pause.guard("community")
        if not body.strip() or len(body) > 30000 or not evidence_urls:
            raise ValueError("nonempty bounded draft and evidence URLs required")
        if any(not isinstance(url, str) for url in evidence_urls):
            raise ValueError("evidence URLs must be strings")
        for url in evidence_urls:
            _https_url(url)
        now = utc_text(self.clock())
        digest = hashlib.sha256(body.encode()).hexdigest()
        with self.database.transaction() as c:
            task = self._owned(c, task_id, worker_id)
            if artifact_type not in ARTIFACTS[task["agent_id"]]:
                raise ValueError("artifact type not allowed for agent")
            observation = None
            if observation_id:
                observation = c.execute(
                    "SELECT o.canonical_url FROM community_observations o "
                    "JOIN community_task_observations t ON t.observation_id=o.id "
                    "WHERE t.task_id=? AND o.id=?", (task_id, observation_id)).fetchone()
                if observation is None:
                    raise ValueError("draft observation is not linked to this task")
                if observation["canonical_url"] not in evidence_urls:
                    raise ValueError("draft evidence must include its source observation URL")
            if artifact_type in {"helpful_reply", "technical_comment", "launch_comment", "linkedin_response"} and not observation_id:
                raise ValueError("contextual reply/comment requires a sourced observation")
            prior = c.execute("SELECT id FROM community_drafts WHERE task_id=? AND artifact_type=? "
                              "AND body_sha256=?", (task_id, artifact_type, digest)).fetchone()
            if prior:
                return prior["id"]
            draft_id = uuid4().hex
            c.execute("INSERT INTO community_drafts(id,task_id,observation_id,artifact_type,body,"
                      "evidence_json,body_sha256,state,author,created_at_utc) "
                      "VALUES (?,?,?,?,?,?,?,'needs_review',?,?)",
                      (draft_id, task_id, observation_id, artifact_type, body,
                       _json(sorted(set(evidence_urls))), digest, worker_id, now))
            append_audit(c, actor=worker_id, event_type="community.draft_created",
                         entity_type="community_draft", entity_id=draft_id,
                         details_json=_json({"body_sha256": digest, "artifact_type": artifact_type}),
                         occurred_at_utc=now)
        return draft_id

    def finish(self, *, task_id: str, worker_id: str, outcome: str,
               reason: str) -> str:
        self.pause.guard("community")
        if outcome not in {"review", "no_match", "blocked"} or not reason.strip():
            raise ValueError("outcome and reason required")
        now = utc_text(self.clock())
        with self.database.transaction() as c:
            task = self._owned(c, task_id, worker_id)
            if outcome == "review":
                count = c.execute("SELECT COUNT(*) FROM community_drafts WHERE task_id=?",
                                  (task_id,)).fetchone()[0]
                if task["agent_id"] != "6E" and count == 0:
                    raise ValueError("review outcome needs a draft")
                if task["agent_id"] == "6E" and not c.execute(
                    "SELECT 1 FROM community_task_import_links WHERE task_id=? LIMIT 1",
                    (task_id,),
                ).fetchone():
                    raise ValueError("6E review outcome needs a linked import batch")
            state = {"review": "needs_review", "no_match": "completed", "blocked": "blocked"}[outcome]
            c.execute("UPDATE community_tasks SET state=?,lease_owner=NULL,lease_expires_at_utc=NULL,"
                      "last_error_class=?,updated_at_utc=? WHERE id=?",
                      (state, reason if state == "blocked" else None, now, task_id))
            append_audit(c, actor=worker_id, event_type="community.task_finished",
                         entity_type="community_task", entity_id=task_id,
                         details_json=_json({"outcome": outcome, "reason": reason}),
                         occurred_at_utc=now)
        return state

    def review_draft(self, *, draft_id: str, reviewer: str, decision: str,
                     note: str, expected_hash: str) -> None:
        if decision not in {"approved", "rejected"} or not reviewer.strip() or not note.strip():
            raise ValueError("reviewer, decision and note required")
        now = utc_text(self.clock())
        with self.database.transaction() as c:
            row = c.execute("SELECT * FROM community_drafts WHERE id=?", (draft_id,)).fetchone()
            if row is None or row["state"] != "needs_review" or row["body_sha256"] != expected_hash:
                raise ValueError("draft absent, changed or already reviewed")
            c.execute("UPDATE community_drafts SET state=?,reviewer=?,review_note=?,"
                      "reviewed_at_utc=? WHERE id=?", (decision, reviewer, note, now, draft_id))
            append_audit(c, actor=reviewer, event_type="community.draft_reviewed",
                         entity_type="community_draft", entity_id=draft_id,
                         details_json=_json({"decision": decision, "body_sha256": expected_hash}),
                         occurred_at_utc=now)

    def review_launch_item(self, *, task_id: str, item_key: str, state: str,
                           actor: str, evidence_ref: str | None = None) -> None:
        if state not in {"open", "ready", "reviewed"} or not actor.strip():
            raise ValueError("invalid checklist state or actor")
        if state == "reviewed" and not evidence_ref:
            raise ValueError("reviewed checklist item needs evidence")
        now = utc_text(self.clock())
        with self.database.transaction() as c:
            if not c.execute("UPDATE community_launch_items SET state=?,evidence_ref=?,"
                             "reviewed_by=?,updated_at_utc=? WHERE task_id=? AND item_key=?",
                             (state, evidence_ref, actor if state == "reviewed" else None,
                              now, task_id, item_key)).rowcount:
                raise ValueError("launch checklist item not found")
            append_audit(c, actor=actor, event_type="community.launch_item_reviewed",
                         entity_type="community_task", entity_id=task_id,
                         details_json=_json({"item_key": item_key, "state": state}),
                         occurred_at_utc=now)

    def report(self, *, limit: int = 50) -> dict:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be 1..500")
        with self.database.read() as c:
            triggers = [dict(row) for row in c.execute(
                "SELECT id,agent_id,trigger_kind,active,next_due_at_utc,config_version,owner "
                "FROM community_trigger_definitions ORDER BY agent_id,id")]
            tasks = [dict(row) for row in c.execute(
                "SELECT id,agent_id,trigger_key,state,due_at_utc,attempt_count,last_error_class "
                "FROM community_tasks ORDER BY created_at_utc DESC LIMIT ?", (limit,))]
            drafts = [dict(row) for row in c.execute(
                "SELECT id,task_id,artifact_type,state,body_sha256,created_at_utc "
                "FROM community_drafts ORDER BY created_at_utc DESC LIMIT ?", (limit,))]
            connections = [dict(row) for row in c.execute(
                "SELECT agent_id,platform,account_reference,state,last_verified_at_utc,failure_class "
                "FROM community_connection_registry ORDER BY agent_id")]
            imports = [dict(row) for row in c.execute(
                "SELECT state,COUNT(*) AS rows FROM community_import_rows GROUP BY state")]
        return {"triggers": triggers, "tasks": tasks, "drafts": drafts,
                "connections": connections, "import_rows": imports}
