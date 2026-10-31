"""Versioned provider prices, idempotent usage and verified backups (Department 5B)."""
from __future__ import annotations

import hashlib
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from lumen_marketing_os.core.errors import ConflictError
from lumen_marketing_os.core.time import Clock, utc_now, utc_text
from lumen_marketing_os.db.database import Database, append_audit


class OperationsLedger:
    def __init__(self, database: Database, *, clock: Clock = utc_now) -> None:
        self.database, self.clock = database, clock

    def approve_price(self, *, provider: str, model: str, currency: str,
                      prompt_per_million_minor: int, completion_per_million_minor: int,
                      request_minor: int, effective_at: datetime, actor: str) -> str:
        if any(not x or not x.strip() for x in (provider, model, actor)):
            raise ValueError("provider, model and actor are required")
        if len(currency) != 3 or not currency.isalpha() or currency != currency.upper():
            raise ValueError("currency must be a three-letter uppercase code")
        if any(type(x) is not int or x < 0 for x in
               (prompt_per_million_minor, completion_per_million_minor, request_minor)):
            raise ValueError("price amounts must be nonnegative integer minor units")
        effective, now = utc_text(effective_at), utc_text(self.clock())
        price_id = uuid4().hex
        with self.database.transaction() as c:
            if c.execute("SELECT 1 FROM provider_prices WHERE provider=? AND model=? "
                         "AND currency!=?", (provider, model, currency)).fetchone():
                raise ConflictError("provider/model currency changed; use an explicit migration")
            c.execute("INSERT INTO provider_prices VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (price_id, provider, model, currency, prompt_per_million_minor,
                       completion_per_million_minor, request_minor, effective, actor, now))
            append_audit(c, actor=actor, event_type="price.approved", entity_type="provider_price",
                         entity_id=price_id, occurred_at_utc=now)
        return price_id

    def record_usage(self, *, usage_key: str, provider: str, model: str,
                     prompt_tokens: int | None, completion_tokens: int | None,
                     requests: int | None, occurred_at: datetime,
                     job_id: str | None = None) -> dict:
        if any(not x or not x.strip() for x in (usage_key, provider, model)):
            raise ValueError("usage key, provider and model are required")
        if any(x is not None and (type(x) is not int or x < 0)
               for x in (prompt_tokens, completion_tokens, requests)):
            raise ValueError("usage counts must be nonnegative integers or unknown")
        occurred, now = utc_text(occurred_at), utc_text(self.clock())
        with self.database.transaction() as c:
            existing = c.execute("SELECT * FROM usage_ledger WHERE usage_key=?", (usage_key,)).fetchone()
            if existing is not None:
                if (existing["provider"], existing["model"], existing["job_id"],
                    existing["prompt_tokens"], existing["completion_tokens"],
                    existing["requests"], existing["occurred_at_utc"]) != (
                        provider, model, job_id, prompt_tokens, completion_tokens, requests, occurred):
                    raise ConflictError("usage key already records different usage")
                return dict(existing)
            price = c.execute("SELECT * FROM provider_prices WHERE provider=? AND model=? "
                              "AND effective_at_utc<=? ORDER BY effective_at_utc DESC LIMIT 1",
                              (provider, model, occurred)).fetchone()
            # Unknown data or unknown price remains unknown, never free by default.
            known = price is not None and all(x is not None for x in
                                             (prompt_tokens, completion_tokens, requests))
            estimate = None
            if known:
                numerator = (prompt_tokens * price["prompt_per_million_minor"]
                             + completion_tokens * price["completion_per_million_minor"])
                estimate = (numerator + 999_999) // 1_000_000 + requests * price["request_minor"]
            entry_id = uuid4().hex
            c.execute("INSERT INTO usage_ledger(id,usage_key,provider,model,job_id,prompt_tokens,"
                      "completion_tokens,requests,price_id,estimated_minor,actual_minor,currency,status,"
                      "occurred_at_utc,recorded_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?)",
                      (entry_id, usage_key, provider, model, job_id, prompt_tokens,
                       completion_tokens, requests, price["id"] if price else None,
                       estimate, price["currency"] if price else None,
                       "estimated" if known else "unknown", occurred, now))
            append_audit(c, actor="usage-ledger", event_type="usage.recorded", entity_type="usage",
                         entity_id=entry_id, occurred_at_utc=now)
            return dict(c.execute("SELECT * FROM usage_ledger WHERE id=?", (entry_id,)).fetchone())

    def reconcile(self, *, usage_key: str, actual_minor: int, currency: str,
                  actor: str) -> dict:
        if type(actual_minor) is not int or actual_minor < 0 or not actor.strip():
            raise ValueError("nonnegative actual cost and actor are required")
        if len(currency) != 3 or not currency.isalpha() or currency != currency.upper():
            raise ValueError("currency must be a three-letter uppercase code")
        now = utc_text(self.clock())
        with self.database.transaction() as c:
            row = c.execute("SELECT * FROM usage_ledger WHERE usage_key=?", (usage_key,)).fetchone()
            if row is None:
                raise ValueError("usage key not found")
            if row["currency"] is not None and row["currency"] != currency:
                raise ConflictError("billing currency differs from approved price")
            if row["status"] == "reconciled":
                if row["actual_minor"] != actual_minor or row["currency"] != currency:
                    raise ConflictError("usage already reconciled to different amount")
                return dict(row)
            c.execute("UPDATE usage_ledger SET actual_minor=?,currency=?,status='reconciled',"
                      "reconciled_at_utc=?,reconciled_by=? WHERE id=?",
                      (actual_minor, currency, now, actor, row["id"]))
            append_audit(c, actor=actor, event_type="usage.reconciled", entity_type="usage",
                         entity_id=row["id"], occurred_at_utc=now)
            return dict(c.execute("SELECT * FROM usage_ledger WHERE id=?", (row["id"],)).fetchone())

    def report(self) -> dict:
        with self.database.read() as c:
            rows = [dict(row) for row in c.execute(
                "SELECT provider,model,currency,status,COUNT(*) AS events,"
                "SUM(prompt_tokens) AS prompt_tokens,SUM(completion_tokens) AS completion_tokens,"
                "SUM(requests) AS requests,SUM(estimated_minor) AS estimated_minor,"
                "SUM(actual_minor) AS actual_minor FROM usage_ledger "
                "GROUP BY provider,model,currency,status ORDER BY provider,model,status")]
            unpriced = c.execute("SELECT COUNT(*) FROM usage_ledger WHERE status='unknown'").fetchone()[0]
            unmetered = [dict(row) for row in c.execute(
                "SELECT q.provider,COUNT(*) AS calls FROM qualification_provider_calls q "
                "WHERE NOT EXISTS(SELECT 1 FROM usage_ledger u WHERE u.usage_key=q.request_key) "
                "GROUP BY q.provider ORDER BY q.provider")]
            backups = [dict(row) for row in c.execute(
                "SELECT * FROM backup_records ORDER BY created_at_utc DESC LIMIT 10")]
            drills = [dict(row) for row in c.execute(
                "SELECT * FROM restore_drills ORDER BY performed_at_utc DESC LIMIT 10")]
        return {"usage": rows, "unknown_usage_events": unpriced,
                "qualification_calls_without_usage_record": unmetered,
                "backups": backups, "restore_drills": drills}

    def backup(self, destination: Path) -> dict:
        saved = self.database.backup(destination)
        digest = self._sha256(saved)
        check = Database(saved).integrity_check()
        if not check["ok"]:
            raise RuntimeError("backup failed verification")
        now, backup_id = utc_text(self.clock()), uuid4().hex
        with self.database.transaction() as c:
            c.execute("INSERT INTO backup_records VALUES (?,?,?,?,?,?,?)",
                      (backup_id, str(saved), digest, saved.stat().st_size,
                       len(check["migrations"]), now, now))
            append_audit(c, actor="operations", event_type="backup.verified", entity_type="backup",
                         entity_id=backup_id, occurred_at_utc=now)
        return {"backup_id": backup_id, "path": str(saved), "sha256": digest,
                "size_bytes": saved.stat().st_size}

    def scheduled_backup(self, directory: Path) -> dict:
        destination = Path(directory) / f"lmos-{self.clock().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}.db"
        return self.backup(destination)

    def restore_latest(self) -> dict:
        with self.database.read() as c:
            row = c.execute("SELECT id FROM backup_records ORDER BY created_at_utc DESC,rowid DESC LIMIT 1").fetchone()
        if row is None:
            raise ValueError("no verified backup to drill")
        return self.restore_drill(backup_id=row["id"])

    def restore_drill(self, *, backup_id: str) -> dict:
        with self.database.read() as c:
            row = c.execute("SELECT * FROM backup_records WHERE id=?", (backup_id,)).fetchone()
        if row is None:
            raise ValueError("backup record not found")
        source = Path(row["path"])
        if not source.is_file():
            raise FileNotFoundError(source)
        digest = self._sha256(source)
        passed, diagnostic = False, None
        if digest != row["sha256"]:
            diagnostic = "backup checksum mismatch"
        else:
            with tempfile.TemporaryDirectory(prefix="lmos-restore-") as directory:
                isolated = Path(directory) / "restored.db"
                shutil.copy2(source, isolated)
                check = Database(isolated).integrity_check()
                passed = bool(check["ok"] and len(check["migrations"]) == row["migration_count"])
                if not passed:
                    diagnostic = "isolated restore integrity or migration check failed"
        now, drill_id = utc_text(self.clock()), uuid4().hex
        with self.database.transaction() as c:
            c.execute("INSERT INTO restore_drills VALUES (?,?,?,?,?)",
                      (drill_id, backup_id, "passed" if passed else "failed", diagnostic, now))
            append_audit(c, actor="operations", event_type="restore.drilled",
                         entity_type="restore_drill", entity_id=drill_id, occurred_at_utc=now)
        return {"drill_id": drill_id, "backup_id": backup_id, "passed": passed,
                "diagnostic": diagnostic}

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
