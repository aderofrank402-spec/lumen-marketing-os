from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from lumen_marketing_os.core.errors import PausedError
from lumen_marketing_os.core.time import Clock, utc_now, utc_text
from lumen_marketing_os.db.database import Database, append_audit


@dataclass(frozen=True)
class PauseStatus:
    paused: bool
    reasons: tuple[str, ...]
    file_present: bool


class PauseControl:
    def __init__(self, database: Database, pause_file: Path, *, clock: Clock = utc_now) -> None:
        self.database = database
        self.pause_file = Path(pause_file).resolve()
        self.clock = clock

    @staticmethod
    def _scope(lane: str | None) -> str:
        if lane is None:
            return "global"
        if not lane or any(c.isspace() for c in lane):
            raise ValueError("lane must be a non-empty name without spaces")
        return f"lane:{lane}"

    def status(self, lane: str | None = None) -> PauseStatus:
        scopes = ["global"]
        if lane is not None:
            scopes.append(self._scope(lane))
        placeholders = ",".join("?" for _ in scopes)
        with self.database.read() as connection:
            rows = connection.execute(
                f"SELECT scope, reason FROM system_controls WHERE paused = 1 "
                f"AND scope IN ({placeholders}) ORDER BY scope", scopes,
            ).fetchall()
        file_present = self.pause_file.is_file()
        reasons = (["PAUSE_ALL.lock"] if file_present else []) + [
            f"{row['scope']}: {row['reason'] or 'paused'}" for row in rows
        ]
        return PauseStatus(bool(reasons), tuple(reasons), file_present)

    def guard(self, lane: str | None = None) -> None:
        state = self.status(lane)
        if state.paused:
            raise PausedError("paused: " + "; ".join(state.reasons))

    def active_scopes(self) -> list[dict[str, str]]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT scope,reason,actor,changed_at_utc FROM system_controls "
                "WHERE paused=1 ORDER BY scope"
            ).fetchall()
        return [dict(row) for row in rows]

    def pause(self, *, actor: str, reason: str, lane: str | None = None) -> PauseStatus:
        if not actor.strip() or not reason.strip():
            raise ValueError("actor and reason are required")
        scope = self._scope(lane)
        now = utc_text(self.clock())
        with self.database.transaction() as connection:
            if lane is None:
                self.pause_file.parent.mkdir(parents=True, exist_ok=True)
                self.pause_file.touch(exist_ok=True)
            connection.execute(
                "INSERT INTO system_controls(scope, paused, reason, actor, changed_at_utc) "
                "VALUES (?, 1, ?, ?, ?) ON CONFLICT(scope) DO UPDATE SET paused=1, "
                "reason=excluded.reason, actor=excluded.actor, changed_at_utc=excluded.changed_at_utc",
                (scope, reason, actor, now),
            )
            append_audit(connection, actor=actor, event_type="control.paused",
                         entity_type="control", entity_id=scope,
                         details_json=json.dumps({"reason": reason}), occurred_at_utc=now)
        return self.status(lane)

    def resume(self, *, actor: str, reason: str, lane: str | None = None) -> PauseStatus:
        if not actor.strip() or not reason.strip():
            raise ValueError("actor and reason are required")
        scope = self._scope(lane)
        now = utc_text(self.clock())
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO system_controls(scope, paused, reason, actor, changed_at_utc) "
                "VALUES (?, 0, ?, ?, ?) ON CONFLICT(scope) DO UPDATE SET paused=0, "
                "reason=excluded.reason, actor=excluded.actor, changed_at_utc=excluded.changed_at_utc",
                (scope, reason, actor, now),
            )
            append_audit(connection, actor=actor, event_type="control.resumed",
                         entity_type="control", entity_id=scope,
                         details_json=json.dumps({"reason": reason}), occurred_at_utc=now)
            if lane is None:
                self.pause_file.unlink(missing_ok=True)
        return self.status(lane)
