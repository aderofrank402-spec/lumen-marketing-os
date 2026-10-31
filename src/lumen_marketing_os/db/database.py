from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from lumen_marketing_os.core.errors import MigrationError
from lumen_marketing_os.core.time import utc_now, utc_text

MIGRATION_PACKAGE = "lumen_marketing_os.db.migrations"
MIGRATION_NAME = re.compile(r"^[0-9]{4}_[a-z0-9_]+\.sql$")


class Database:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 5000) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self.path = Path(path).resolve()
        self.busy_timeout_ms = busy_timeout_ms

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000,
                                     isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def initialize(self) -> list[str]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.read() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at_utc TEXT NOT NULL)"
            )
            existing = {
                row["version"]: row["checksum"]
                for row in connection.execute("SELECT version, checksum FROM schema_migrations")
            }
        resources = files(MIGRATION_PACKAGE)
        migrations = sorted(r for r in resources.iterdir() if MIGRATION_NAME.match(r.name))
        if not migrations:
            raise MigrationError("no packaged SQL migrations found")
        known = {r.name for r in migrations}
        unknown = set(existing) - known
        if unknown:
            raise MigrationError(f"database has unknown migrations: {', '.join(sorted(unknown))}")
        applied: list[str] = []
        for resource in migrations:
            sql = resource.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if resource.name in existing:
                if existing[resource.name] != checksum:
                    raise MigrationError(f"migration checksum changed: {resource.name}")
                continue
            with self.transaction() as connection:
                # Another initializer may have applied this migration while we read the ledger.
                row = connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = ?", (resource.name,)
                ).fetchone()
                if row is not None:
                    if row["checksum"] != checksum:
                        raise MigrationError(f"migration checksum changed: {resource.name}")
                    continue
                statement = ""
                for line in sql.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        if statement.strip():
                            connection.execute(statement)
                        statement = ""
                if statement.strip():
                    raise MigrationError(f"incomplete SQL in migration {resource.name}")
                connection.execute(
                    "INSERT INTO schema_migrations(version, checksum, applied_at_utc) VALUES (?, ?, ?)",
                    (resource.name, checksum, utc_text(utc_now())),
                )
            applied.append(resource.name)
        return applied

    def integrity_check(self) -> dict[str, object]:
        with self.read() as connection:
            messages = [row[0] for row in connection.execute("PRAGMA integrity_check")]
            fk_violations = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
            versions = [row[0] for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version")]
        return {"ok": messages == ["ok"] and not fk_violations,
                "messages": messages, "foreign_key_violations": fk_violations,
                "migrations": versions}

    def backup(self, destination: Path) -> Path:
        target = Path(destination).resolve()
        if target == self.path:
            raise ValueError("backup destination must differ from database")
        if target.exists():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with self.read() as source:
                backup = sqlite3.connect(temporary)
                try:
                    source.backup(backup)
                finally:
                    backup.close()
            verification = Database(temporary, busy_timeout_ms=self.busy_timeout_ms).integrity_check()
            if not verification["ok"]:
                raise RuntimeError(f"backup failed integrity check: {verification['messages']}")
            # On Windows, rename fails if another process created the target meanwhile.
            os.rename(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target


def append_audit(connection: sqlite3.Connection, *, actor: str, event_type: str,
                 entity_type: str | None = None, entity_id: str | None = None,
                 correlation_id: str | None = None, details_json: str = "{}",
                 occurred_at_utc: str | None = None) -> str:
    event_id = uuid4().hex
    connection.execute(
        "INSERT INTO audit_events(id, actor, event_type, entity_type, entity_id, "
        "correlation_id, details_json, occurred_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (event_id, actor, event_type, entity_type, entity_id, correlation_id,
         details_json, occurred_at_utc or utc_text(utc_now())),
    )
    return event_id
