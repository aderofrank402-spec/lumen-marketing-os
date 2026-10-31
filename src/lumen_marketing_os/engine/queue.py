from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable
from uuid import uuid4

from lumen_marketing_os.core.errors import (ConflictError, InvalidStateError,
                                             LeaseLostError, NotFoundError, PausedError)
from lumen_marketing_os.core.time import Clock, utc_now, utc_text
from lumen_marketing_os.db.database import Database, append_audit
from lumen_marketing_os.policy.pause import PauseControl


@dataclass(frozen=True)
class Job:
    id: str
    workflow: str
    lane: str
    effect: str
    payload: dict[str, Any]
    logical_key: str
    priority: int
    due_at_utc: str
    state: str
    lease_owner: str | None
    lease_expires_at_utc: str | None
    attempt_count: int
    max_attempts: int
    last_error_class: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(row["id"], row["workflow"], row["lane"], row["effect"],
                   json.loads(row["payload_json"]), row["logical_key"],
                   row["priority"], row["due_at_utc"], row["state"],
                   row["lease_owner"], row["lease_expires_at_utc"],
                   row["attempt_count"], row["max_attempts"], row["last_error_class"])


@dataclass(frozen=True)
class OutgoingEvent:
    event_type: str
    aggregate_type: str
    aggregate_id: str
    destination: str
    payload: dict[str, Any]
    deduplication_key: str
    schema_version: int = 1


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _required(value: str, name: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{name} is required")
    return value


class JobQueue:
    def __init__(self, database: Database, pause: PauseControl, *, clock: Clock = utc_now,
                 retry_base_seconds: int = 30, retry_max_seconds: int = 300,
                 external_actions_enabled: bool = False) -> None:
        if retry_base_seconds <= 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("invalid retry interval")
        self.database = database
        self.pause = pause
        self.clock = clock
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.external_actions_enabled = external_actions_enabled

    def enqueue(self, *, workflow: str, lane: str, payload: dict[str, Any],
                logical_key: str, due_at: datetime | None = None, priority: int = 0,
                max_attempts: int = 3, effect: str = "internal") -> Job:
        for name, value in (("workflow", workflow), ("lane", lane), ("logical_key", logical_key)):
            _required(value, name)
        if effect not in {"internal", "external"} or max_attempts <= 0:
            raise ValueError("invalid effect or max_attempts")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        encoded = _json(payload)
        now = utc_text(self.clock())
        due = utc_text(due_at or self.clock())
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE logical_key = ?", (logical_key,)
            ).fetchone()
            if existing is not None:
                if (existing["workflow"], existing["lane"], existing["effect"],
                    existing["payload_json"]) != (workflow, lane, effect, encoded):
                    raise ConflictError("logical job key already has different content")
                return Job.from_row(existing)
            job_id = uuid4().hex
            connection.execute(
                "INSERT INTO jobs(id,workflow,lane,effect,payload_json,logical_key,priority,"
                "due_at_utc,state,attempt_count,max_attempts,created_at_utc,updated_at_utc) "
                "VALUES (?,?,?,?,?,?,?,?, 'pending',0,?,?,?)",
                (job_id, workflow, lane, effect, encoded, logical_key, priority,
                 due, max_attempts, now, now),
            )
            append_audit(connection, actor="queue", event_type="job.enqueued",
                         entity_type="job", entity_id=job_id,
                         details_json=_json({"workflow": workflow, "lane": lane,
                                             "effect": effect, "logical_key": logical_key}),
                         occurred_at_utc=now)
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.from_row(row)

    def get(self, job_id: str) -> Job:
        with self.database.read() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"job not found: {job_id}")
        return Job.from_row(row)

    def list_jobs(self, *, state: str | None = None, limit: int = 50) -> list[Job]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self.database.read() as connection:
            if state is None:
                rows = connection.execute(
                    "SELECT * FROM jobs ORDER BY created_at_utc DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE state=? ORDER BY created_at_utc DESC LIMIT ?",
                    (state, limit),
                ).fetchall()
        return [Job.from_row(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS n FROM jobs GROUP BY state"
            ).fetchall()
        return {row["state"]: row["n"] for row in rows}

    def recover_expired(self) -> dict[str, int]:
        now = utc_text(self.clock())
        with self.database.transaction() as connection:
            return self._recover_expired_locked(connection, now)

    def _recover_expired_locked(self, connection: sqlite3.Connection, now: str) -> dict[str, int]:
        expired = connection.execute(
            "SELECT * FROM jobs WHERE state='leased' AND lease_expires_at_utc <= ?",
            (now,),
        ).fetchall()
        counts = {"retried": 0, "review": 0, "dead_letter": 0}
        for row in expired:
            if row["effect"] == "external":
                state, bucket = "needs_review", "review"
            elif row["attempt_count"] >= row["max_attempts"]:
                state, bucket = "dead_letter", "dead_letter"
            else:
                state, bucket = "retry_wait", "retried"
            connection.execute(
                "UPDATE jobs SET state=?, due_at_utc=?, lease_owner=NULL,"
                "lease_expires_at_utc=NULL,last_error_class='lease_expired',updated_at_utc=? "
                "WHERE id=?", (state, now, now, row["id"]),
            )
            connection.execute(
                "UPDATE job_attempts SET result='lease_expired',finished_at_utc=?,"
                "error_class='lease_expired' WHERE job_id=? AND attempt_number=? "
                "AND result='running'", (now, row["id"], row["attempt_count"]),
            )
            append_audit(connection, actor="queue", event_type="job.lease_expired",
                         entity_type="job", entity_id=row["id"],
                         details_json=_json({"new_state": state}), occurred_at_utc=now)
            counts[bucket] += 1
        return counts

    def claim(self, *, worker_id: str, lease_seconds: int = 300,
              lane: str | None = None) -> Job | None:
        _required(worker_id, "worker_id")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.pause.guard(lane)
        now_dt = self.clock()
        now = utc_text(now_dt)
        expires = utc_text(now_dt + timedelta(seconds=lease_seconds))
        with self.database.transaction() as connection:
            if self.pause.pause_file.is_file() or connection.execute(
                "SELECT 1 FROM system_controls WHERE scope='global' AND paused=1"
            ).fetchone():
                raise PausedError("global pause is active")
            if lane is not None and connection.execute(
                "SELECT 1 FROM system_controls WHERE scope=? AND paused=1", (f"lane:{lane}",)
            ).fetchone():
                raise PausedError(f"lane {lane} is paused")
            self._recover_expired_locked(connection, now)
            query = (
                "SELECT * FROM jobs WHERE state IN ('pending','retry_wait') "
                "AND due_at_utc <= ? AND attempt_count < max_attempts "
                "AND NOT EXISTS(SELECT 1 FROM system_controls c WHERE "
                "c.scope='lane:' || jobs.lane AND c.paused=1)"
            )
            params: list[Any] = [now]
            if not self.external_actions_enabled:
                query += " AND effect='internal'"
            if lane is not None:
                query += " AND lane=?"
                params.append(lane)
            query += " ORDER BY priority DESC,due_at_utc,created_at_utc LIMIT 1"
            row = connection.execute(query, params).fetchone()
            if row is None:
                return None
            attempt = row["attempt_count"] + 1
            connection.execute(
                "UPDATE jobs SET state='leased',lease_owner=?,lease_expires_at_utc=?,"
                "attempt_count=?,updated_at_utc=? WHERE id=?",
                (worker_id, expires, attempt, now, row["id"]),
            )
            connection.execute(
                "INSERT INTO job_attempts(id,job_id,attempt_number,worker_id,started_at_utc,result) "
                "VALUES (?,?,?,?,?,'running')",
                (uuid4().hex, row["id"], attempt, worker_id, now),
            )
            append_audit(connection, actor=worker_id, event_type="job.claimed",
                         entity_type="job", entity_id=row["id"],
                         details_json=_json({"attempt": attempt}), occurred_at_utc=now)
            claimed = connection.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        return Job.from_row(claimed)

    def _owned(self, connection: sqlite3.Connection, job_id: str, worker_id: str,
               now: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"job not found: {job_id}")
        if (row["state"] != "leased" or row["lease_owner"] != worker_id
                or row["lease_expires_at_utc"] <= now):
            raise LeaseLostError(f"worker {worker_id} does not own a live lease for {job_id}")
        return row

    def heartbeat(self, *, job_id: str, worker_id: str, lease_seconds: int) -> Job:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now_dt = self.clock()
        now = utc_text(now_dt)
        expires = utc_text(now_dt + timedelta(seconds=lease_seconds))
        with self.database.transaction() as connection:
            self._owned(connection, job_id, worker_id, now)
            connection.execute(
                "UPDATE jobs SET lease_expires_at_utc=?,updated_at_utc=? WHERE id=?",
                (expires, now, job_id),
            )
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job.from_row(row)

    def complete(self, *, job_id: str, worker_id: str,
                 events: Iterable[OutgoingEvent] = (),
                 mutate: Callable[[sqlite3.Connection], None] | None = None) -> Job:
        """Commit domain writes, outbox handoffs, and job success in one transaction.

        ``mutate`` may only do short local database work; never call a provider in it.
        """
        now = utc_text(self.clock())
        with self.database.transaction() as connection:
            row = self._owned(connection, job_id, worker_id, now)
            if mutate is not None:
                mutate(connection)
            for event in events:
                for name in ("event_type", "aggregate_type", "aggregate_id",
                             "destination", "deduplication_key"):
                    _required(getattr(event, name), name)
                if event.schema_version <= 0:
                    raise ValueError("schema_version must be positive")
                connection.execute(
                    "INSERT INTO outbox_events(id,event_type,schema_version,aggregate_type,"
                    "aggregate_id,destination,payload_json,deduplication_key,state,due_at_utc,"
                    "created_at_utc,updated_at_utc) VALUES (?,?,?,?,?,?,?,?, 'pending',?,?,?)",
                    (uuid4().hex, event.event_type, event.schema_version,
                     event.aggregate_type, event.aggregate_id, event.destination,
                     _json(event.payload), event.deduplication_key, now, now, now),
                )
            connection.execute(
                "UPDATE jobs SET state='succeeded',lease_owner=NULL,"
                "lease_expires_at_utc=NULL,updated_at_utc=? WHERE id=?", (now, job_id),
            )
            connection.execute(
                "UPDATE job_attempts SET result='succeeded',finished_at_utc=? "
                "WHERE job_id=? AND attempt_number=?",
                (now, job_id, row["attempt_count"]),
            )
            append_audit(connection, actor=worker_id, event_type="job.succeeded",
                         entity_type="job", entity_id=job_id, occurred_at_utc=now)
            completed = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job.from_row(completed)

    def fail(self, *, job_id: str, worker_id: str, error_class: str,
             retryable: bool, unknown_effect: bool = False,
             redacted_diagnostic: str | None = None,
             retry_after_seconds: int | None = None) -> Job:
        _required(error_class, "error_class")
        if retry_after_seconds is not None and retry_after_seconds <= 0:
            raise ValueError("retry_after_seconds must be positive")
        now_dt = self.clock()
        now = utc_text(now_dt)
        with self.database.transaction() as connection:
            row = self._owned(connection, job_id, worker_id, now)
            if unknown_effect or row["effect"] == "external" and retryable:
                state, result, due = "needs_review", "needs_review", now
            elif retryable and row["attempt_count"] < row["max_attempts"]:
                seconds = (retry_after_seconds if retry_after_seconds is not None else
                           min(self.retry_max_seconds,
                               self.retry_base_seconds * 2 ** (row["attempt_count"] - 1)))
                state, result, due = "retry_wait", "retry", utc_text(
                    now_dt + timedelta(seconds=seconds))
            elif retryable:
                state, result, due = "dead_letter", "dead_letter", now
            else:
                state, result, due = "needs_review", "needs_review", now
            connection.execute(
                "UPDATE jobs SET state=?,due_at_utc=?,lease_owner=NULL,"
                "lease_expires_at_utc=NULL,last_error_class=?,updated_at_utc=? WHERE id=?",
                (state, due, error_class, now, job_id),
            )
            connection.execute(
                "UPDATE job_attempts SET result=?,finished_at_utc=?,error_class=?,"
                "redacted_diagnostic=? WHERE job_id=? AND attempt_number=?",
                (result, now, error_class, redacted_diagnostic,
                 job_id, row["attempt_count"]),
            )
            append_audit(connection, actor=worker_id, event_type="job.failed",
                         entity_type="job", entity_id=job_id,
                         details_json=_json({"error_class": error_class, "state": state,
                                             "unknown_effect": unknown_effect}),
                         occurred_at_utc=now)
            failed = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job.from_row(failed)

    def replay(self, *, job_id: str, actor: str, reason: str,
               external_effect_ruled_out: bool = False) -> Job:
        _required(actor, "actor")
        _required(reason, "reason")
        now = utc_text(self.clock())
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"job not found: {job_id}")
            if row["state"] not in {"needs_review", "dead_letter"}:
                raise InvalidStateError("only review or dead-letter jobs can be replayed")
            if row["effect"] == "external" and not external_effect_ruled_out:
                raise InvalidStateError("external job needs explicit no-effect reconciliation")
            connection.execute(
                "UPDATE jobs SET state='pending',due_at_utc=?,max_attempts=?,"
                "last_error_class=NULL,updated_at_utc=? WHERE id=?",
                (now, row["attempt_count"] + row["max_attempts"], now, job_id),
            )
            append_audit(connection, actor=actor, event_type="job.replayed",
                         entity_type="job", entity_id=job_id,
                         details_json=_json({"reason": reason,
                                             "external_effect_ruled_out": external_effect_ruled_out}),
                         occurred_at_utc=now)
            updated = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job.from_row(updated)
