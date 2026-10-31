from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from lumen_marketing_os.core.errors import LeaseLostError, NotFoundError, PausedError
from lumen_marketing_os.core.time import Clock, utc_now, utc_text
from lumen_marketing_os.db.database import Database, append_audit
from lumen_marketing_os.policy.pause import PauseControl


@dataclass(frozen=True)
class OutboxItem:
    id: str
    event_type: str
    schema_version: int
    aggregate_type: str
    aggregate_id: str
    destination: str
    payload: dict[str, Any]
    deduplication_key: str
    state: str
    attempt_count: int
    lease_owner: str | None
    lease_expires_at_utc: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> OutboxItem:
        return cls(row["id"], row["event_type"], row["schema_version"],
                   row["aggregate_type"], row["aggregate_id"], row["destination"],
                   json.loads(row["payload_json"]), row["deduplication_key"],
                   row["state"], row["attempt_count"], row["lease_owner"],
                   row["lease_expires_at_utc"])


class Outbox:
    """At-least-once delivery for internal handoffs and idempotent projections only.

    Non-idempotent email, posts, and bookings use action/delivery_attempt records instead.
    """

    def __init__(self, database: Database, pause: PauseControl, *, clock: Clock = utc_now,
                 max_attempts: int = 3) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.database = database
        self.pause = pause
        self.clock = clock
        self.max_attempts = max_attempts

    def _recover_locked(self, connection: sqlite3.Connection, now: str) -> int:
        rows = connection.execute(
            "SELECT id,attempt_count FROM outbox_events WHERE state='leased' "
            "AND lease_expires_at_utc <= ?", (now,),
        ).fetchall()
        for row in rows:
            state = "needs_review" if row["attempt_count"] >= self.max_attempts else "retry_wait"
            connection.execute(
                "UPDATE outbox_events SET state=?,lease_owner=NULL,lease_expires_at_utc=NULL,"
                "due_at_utc=?,last_error_class='lease_expired',updated_at_utc=? WHERE id=?",
                (state, now, now, row["id"]),
            )
            append_audit(connection, actor="outbox", event_type="outbox.lease_expired",
                         entity_type="outbox_event", entity_id=row["id"], occurred_at_utc=now)
        return len(rows)

    def recover_expired(self) -> int:
        with self.database.transaction() as connection:
            return self._recover_locked(connection, utc_text(self.clock()))

    def claim(self, *, worker_id: str, lease_seconds: int = 60,
              destination: str | None = None) -> OutboxItem | None:
        if not worker_id.strip() or lease_seconds <= 0:
            raise ValueError("worker_id and positive lease_seconds are required")
        self.pause.guard()
        now_dt = self.clock()
        now = utc_text(now_dt)
        expiry = utc_text(now_dt + timedelta(seconds=lease_seconds))
        with self.database.transaction() as connection:
            if self.pause.pause_file.is_file() or connection.execute(
                "SELECT 1 FROM system_controls WHERE scope='global' AND paused=1"
            ).fetchone():
                raise PausedError("global pause is active")
            self._recover_locked(connection, now)
            query = "SELECT * FROM outbox_events WHERE state IN ('pending','retry_wait') AND due_at_utc <= ?"
            params: list[Any] = [now]
            if destination is not None:
                query += " AND destination=?"
                params.append(destination)
            query += " ORDER BY due_at_utc,created_at_utc LIMIT 1"
            row = connection.execute(query, params).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE outbox_events SET state='leased',lease_owner=?,lease_expires_at_utc=?,"
                "attempt_count=attempt_count+1,updated_at_utc=? WHERE id=?",
                (worker_id, expiry, now, row["id"]),
            )
            claimed = connection.execute("SELECT * FROM outbox_events WHERE id=?", (row["id"],)).fetchone()
        return OutboxItem.from_row(claimed)

    @staticmethod
    def _owned(connection: sqlite3.Connection, item_id: str, worker_id: str,
               now: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM outbox_events WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"outbox item not found: {item_id}")
        if (row["state"] != "leased" or row["lease_owner"] != worker_id
                or row["lease_expires_at_utc"] <= now):
            raise LeaseLostError(f"worker {worker_id} does not own a live outbox lease")
        return row

    def acknowledge(self, *, item_id: str, worker_id: str) -> OutboxItem:
        now = utc_text(self.clock())
        with self.database.transaction() as connection:
            self._owned(connection, item_id, worker_id, now)
            connection.execute(
                "UPDATE outbox_events SET state='delivered',lease_owner=NULL,"
                "lease_expires_at_utc=NULL,updated_at_utc=? WHERE id=?", (now, item_id),
            )
            append_audit(connection, actor=worker_id, event_type="outbox.delivered",
                         entity_type="outbox_event", entity_id=item_id, occurred_at_utc=now)
            row = connection.execute("SELECT * FROM outbox_events WHERE id=?", (item_id,)).fetchone()
        return OutboxItem.from_row(row)

    def fail(self, *, item_id: str, worker_id: str, error_class: str,
             retry_after_seconds: int = 30) -> OutboxItem:
        if not error_class.strip() or retry_after_seconds <= 0:
            raise ValueError("error_class and positive retry_after_seconds are required")
        now_dt = self.clock()
        now = utc_text(now_dt)
        with self.database.transaction() as connection:
            current = self._owned(connection, item_id, worker_id, now)
            state = "needs_review" if current["attempt_count"] >= self.max_attempts else "retry_wait"
            due = utc_text(now_dt + timedelta(seconds=retry_after_seconds))
            connection.execute(
                "UPDATE outbox_events SET state=?,due_at_utc=?,lease_owner=NULL,"
                "lease_expires_at_utc=NULL,last_error_class=?,updated_at_utc=? WHERE id=?",
                (state, due, error_class, now, item_id),
            )
            append_audit(connection, actor=worker_id, event_type="outbox.failed",
                         entity_type="outbox_event", entity_id=item_id,
                         details_json=json.dumps({"error_class": error_class, "state": state}),
                         occurred_at_utc=now)
            row = connection.execute("SELECT * FROM outbox_events WHERE id=?", (item_id,)).fetchone()
        return OutboxItem.from_row(row)

    def counts(self) -> dict[str, int]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT state,COUNT(*) AS n FROM outbox_events GROUP BY state"
            ).fetchall()
        return {row["state"]: row["n"] for row in rows}
