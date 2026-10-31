"""Durable, idempotent local schedule controller (Department 5A)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lumen_marketing_os.core.errors import ConflictError, PausedError
from lumen_marketing_os.core.time import Clock, parse_utc, utc_now, utc_text
from lumen_marketing_os.db.database import Database, append_audit
from lumen_marketing_os.engine.queue import JobQueue
from lumen_marketing_os.policy.pause import PauseControl

# These operations only queue local work. The external dispatch/approval gate lives in Department 4.
WORKFLOWS = {"research.tick", "qualification.tick", "content.tick", "discovery.tick", "support.tick",
             "community.tick", "audit.tick", "operations.backup", "operations.restore_drill",
             "operations.integrity_check"}


def _encoded(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _clock_time(value: str) -> int:
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except (ValueError, TypeError) as exc:
        raise ValueError("local time must be HH:MM") from exc
    if not 0 <= hour < 24 or not 0 <= minute < 60 or len(value) != 5:
        raise ValueError("local time must be HH:MM")
    return hour * 60 + minute


class ScheduleManager:
    def __init__(self, database: Database, pause: PauseControl, queue: JobQueue,
                 *, clock: Clock = utc_now) -> None:
        self.database, self.pause, self.queue, self.clock = database, pause, queue, clock

    def configure(self, *, schedule_id: str, workflow: str, interval_seconds: int,
                  next_due_at: datetime, timezone_name: str = "UTC", catch_up: str = "one",
                  active: bool = False, owner_role: str = "operations",
                  max_overlap: int = 1, window_start: str | None = None,
                  window_end: str | None = None, weekdays: tuple[int, ...] = tuple(range(7)),
                  expected_version: str | None = None) -> str:
        if not schedule_id.strip() or not owner_role.strip() or workflow not in WORKFLOWS:
            raise ValueError("schedule ID, owner and allowlisted workflow are required")
        if interval_seconds <= 0 or max_overlap <= 0 or catch_up not in {"skip", "one", "all"}:
            raise ValueError("invalid interval, overlap or catch-up policy")
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown timezone") from exc
        if (window_start is None) != (window_end is None):
            raise ValueError("both window boundaries are required")
        if window_start is not None:
            if _clock_time(window_start) == _clock_time(window_end):
                raise ValueError("window cannot have zero duration")
        if not weekdays or any(type(day) is not int or day not in range(7) for day in weekdays):
            raise ValueError("weekdays must be 0..6")
        due = utc_text(next_due_at)
        policy = {"workflow": workflow, "interval_seconds": interval_seconds,
                  "next_due_at_utc": due, "timezone": timezone_name, "catch_up": catch_up,
                  "owner_role": owner_role, "max_overlap": max_overlap,
                  "window_start": window_start, "window_end": window_end,
                  "weekdays": sorted(set(weekdays))}
        version = hashlib.sha256(_encoded(policy).encode()).hexdigest()[:24]
        now = utc_text(self.clock())
        with self.database.transaction() as c:
            old = c.execute("SELECT * FROM schedule_definitions WHERE id=?", (schedule_id,)).fetchone()
            if old is not None:
                if old["lane"] != "operations":
                    raise ConflictError("schedule ID belongs to another department")
                if expected_version != old["config_version"]:
                    raise ConflictError("schedule changed; supply current expected_version")
                c.execute("UPDATE schedule_definitions SET workflow=?,lane='operations',interval_seconds=?,"
                          "timezone=?,catch_up_policy=?,active=?,last_due_at_utc=NULL,"
                          "next_due_at_utc=?,config_version=?,owner_role=?,max_overlap=?,"
                          "window_start_local=?,window_end_local=?,weekdays_json=?,updated_at_utc=? WHERE id=?",
                          (workflow, interval_seconds, timezone_name, catch_up, int(active), due,
                           version, owner_role, max_overlap, window_start, window_end,
                           _encoded(sorted(set(weekdays))), now, schedule_id))
            else:
                if expected_version is not None:
                    raise ConflictError("schedule does not exist")
                c.execute("INSERT INTO schedule_definitions(id,workflow,lane,interval_seconds,timezone,"
                          "catch_up_policy,active,next_due_at_utc,config_version,owner_role,max_overlap,"
                          "window_start_local,window_end_local,weekdays_json,updated_at_utc) "
                          "VALUES (?,?, 'operations',?,?,?,?,?,?,?,?,?,?,?,?)",
                          (schedule_id, workflow, interval_seconds, timezone_name, catch_up,
                           int(active), due, version, owner_role, max_overlap, window_start,
                           window_end, _encoded(sorted(set(weekdays))), now))
            append_audit(c, actor=owner_role, event_type="schedule.configured",
                         entity_type="schedule", entity_id=schedule_id,
                         details_json=_encoded({"version": version, "active": active}),
                         occurred_at_utc=now)
        return version

    def set_active(self, schedule_id: str, *, active: bool, actor: str) -> None:
        if not actor.strip():
            raise ValueError("actor is required")
        now = utc_text(self.clock())
        with self.database.transaction() as c:
            if not c.execute("UPDATE schedule_definitions SET active=?,updated_at_utc=? "
                             "WHERE id=? AND lane='operations'",
                             (int(active), now, schedule_id)).rowcount:
                raise ValueError("schedule not found")
            append_audit(c, actor=actor, event_type="schedule.activated" if active else "schedule.deactivated",
                         entity_type="schedule", entity_id=schedule_id, occurred_at_utc=now)

    @staticmethod
    def _in_window(row: sqlite3.Row, due: datetime) -> bool:
        local = due.astimezone(ZoneInfo(row["timezone"]))
        if local.weekday() not in json.loads(row["weekdays_json"]):
            return False
        start, end = row["window_start_local"], row["window_end_local"]
        if start is None:
            return True
        minute = local.hour * 60 + local.minute
        a, b = _clock_time(start), _clock_time(end)
        return a <= minute < b if a < b else minute >= a or minute < b

    def tick(self, *, max_catch_up_per_schedule: int = 24) -> list[dict]:
        if not 1 <= max_catch_up_per_schedule <= 1000:
            raise ValueError("invalid catch-up cap")
        self.pause.guard("operations")
        now_dt = self.clock()
        now = utc_text(now_dt)
        decisions: list[dict] = []
        with self.database.transaction() as c:
            # Recheck under the write lock: a pause must win over job creation.
            if self.pause.pause_file.is_file() or c.execute(
                "SELECT 1 FROM system_controls WHERE paused=1 AND scope IN ('global','lane:operations')"
            ).fetchone():
                return decisions
            rows = c.execute("SELECT * FROM schedule_definitions WHERE lane='operations' AND "
                             "active=1 AND next_due_at_utc<=? "
                             "ORDER BY next_due_at_utc,id", (now,)).fetchall()
            for row in rows:
                first = parse_utc(row["next_due_at_utc"])
                count = int((now_dt - first).total_seconds() // row["interval_seconds"]) + 1
                if row["catch_up_policy"] == "all":
                    due_times = [first + timedelta(seconds=i * row["interval_seconds"])
                                 for i in range(min(count, max_catch_up_per_schedule))]
                    next_due = first + timedelta(seconds=len(due_times) * row["interval_seconds"])
                else:
                    due_times = [first + timedelta(seconds=(count - 1) * row["interval_seconds"])]
                    next_due = first + timedelta(seconds=count * row["interval_seconds"])
                for due in due_times:
                    occurrence = utc_text(due)
                    if row["catch_up_policy"] == "skip" and count > 1:
                        decision = "catch_up_skipped"
                    elif not self._in_window(row, due):
                        decision = "outside_window"
                    else:
                        outstanding = c.execute(
                            "SELECT COUNT(*) FROM schedule_occurrences o JOIN jobs j ON j.id=o.job_id "
                            "WHERE o.schedule_id=? AND j.state IN ('pending','leased','retry_wait')",
                            (row["id"],)).fetchone()[0]
                        decision = "overlap_skipped" if outstanding >= row["max_overlap"] else "enqueued"
                    job_id = None
                    if decision == "enqueued":
                        job_id = uuid4().hex
                        key = f"schedule:{row['id']}:{occurrence}"
                        payload = _encoded({"operation": row["workflow"], "schedule_id": row["id"],
                                            "scheduled_occurrence_utc": occurrence})
                        c.execute("INSERT INTO jobs(id,workflow,lane,effect,payload_json,logical_key,"
                                  "priority,due_at_utc,state,attempt_count,max_attempts,created_at_utc,updated_at_utc) "
                                  "VALUES (?,'operations.dispatch','operations','internal',?,?,0,?,'pending',0,3,?,?)",
                                  (job_id, payload, key, now, now, now))
                    c.execute("INSERT OR IGNORE INTO schedule_occurrences(schedule_id,due_at_utc,"
                              "config_version,decision,job_id,recorded_at_utc) VALUES (?,?,?,?,?,?)",
                              (row["id"], occurrence, row["config_version"], decision, job_id, now))
                    decisions.append({"schedule_id": row["id"], "due_at_utc": occurrence,
                                      "decision": decision, "job_id": job_id})
                c.execute("UPDATE schedule_definitions SET last_due_at_utc=?,next_due_at_utc=? WHERE id=?",
                          (utc_text(due_times[-1]), utc_text(next_due), row["id"]))
            c.execute("INSERT INTO operations_heartbeats(process_id,role,heartbeat_at_utc,details_json) "
                      "VALUES ('scheduler','scheduler',?,?) ON CONFLICT(process_id) DO UPDATE SET "
                      "heartbeat_at_utc=excluded.heartbeat_at_utc,details_json=excluded.details_json",
                      (now, _encoded({"decisions": len(decisions)})))
        return decisions

    def work_once(self, handlers: dict[str, Callable[[], object]], *, worker_id: str = "operations-local") -> dict | None:
        job = self.queue.claim(worker_id=worker_id, lane="operations", lease_seconds=1800)
        if job is None:
            return None
        operation = job.payload.get("operation")
        try:
            if job.workflow != "operations.dispatch" or operation not in WORKFLOWS or operation not in handlers:
                raise ValueError("unrecognized scheduled operation")
            result = handlers[operation]()
            self.queue.complete(job_id=job.id, worker_id=worker_id)
            return {"job_id": job.id, "operation": operation, "result": result, "state": "succeeded"}
        except Exception as exc:
            self.queue.fail(job_id=job.id, worker_id=worker_id, error_class=type(exc).__name__,
                            retryable=not isinstance(exc, ValueError),
                            redacted_diagnostic="scheduled operation failed; inspect local logs")
            return {"job_id": job.id, "operation": operation, "state": "failed",
                    "error_class": type(exc).__name__}

    def report(self) -> dict:
        with self.database.read() as c:
            schedules = [dict(row) for row in c.execute(
                "SELECT * FROM schedule_definitions WHERE lane='operations' ORDER BY id")]
            occurrences = [dict(row) for row in c.execute(
                "SELECT * FROM schedule_occurrences ORDER BY recorded_at_utc DESC LIMIT 50")]
            heartbeats = [dict(row) for row in c.execute("SELECT * FROM operations_heartbeats")]
        return {"schedules": schedules, "recent_occurrences": occurrences, "heartbeats": heartbeats}

    def health(self) -> dict:
        now = self.clock()
        issues: list[str] = []
        with self.database.read() as c:
            active = c.execute("SELECT COUNT(*) FROM schedule_definitions WHERE lane='operations' "
                               "AND active=1").fetchone()[0]
            beat = c.execute("SELECT heartbeat_at_utc FROM operations_heartbeats "
                             "WHERE process_id='scheduler'").fetchone()
            backup = c.execute("SELECT created_at_utc FROM backup_records "
                               "ORDER BY created_at_utc DESC LIMIT 1").fetchone()
            drill = c.execute("SELECT performed_at_utc FROM restore_drills WHERE result='passed' "
                              "ORDER BY performed_at_utc DESC LIMIT 1").fetchone()
            bad_jobs = c.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('needs_review','dead_letter')").fetchone()[0]
            unknown_usage = c.execute("SELECT COUNT(*) FROM usage_ledger WHERE status='unknown'").fetchone()[0]
            unmetered = c.execute("SELECT COUNT(*) FROM qualification_provider_calls q "
                                  "WHERE NOT EXISTS(SELECT 1 FROM usage_ledger u "
                                  "WHERE u.usage_key=q.request_key)").fetchone()[0]
        if active and (beat is None or (now - parse_utc(beat["heartbeat_at_utc"])) > timedelta(minutes=10)):
            issues.append("scheduler_heartbeat_stale")
        if backup is None or (now - parse_utc(backup["created_at_utc"])) > timedelta(hours=36):
            issues.append("backup_missing_or_stale")
        if drill is None or (now - parse_utc(drill["performed_at_utc"])) > timedelta(days=8):
            issues.append("restore_drill_missing_or_stale")
        if bad_jobs:
            issues.append("jobs_need_review")
        if unknown_usage:
            issues.append("usage_unpriced_or_incomplete")
        if unmetered:
            issues.append("qualification_provider_calls_not_metered")
        return {"ok": not issues, "issues": issues, "active_schedules": active,
                "jobs_needing_review": bad_jobs, "unknown_usage_events": unknown_usage,
                "unmetered_qualification_calls": unmetered}

    def run_loop(self, handlers: dict[str, Callable[[], object]], *, poll_seconds: int = 30) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        while True:
            try:
                self.tick()
                while self.work_once(handlers) is not None:
                    pass
            except PausedError:
                # A deliberate pause stops new work, not the local supervisor itself.
                pass
            time.sleep(poll_seconds)
