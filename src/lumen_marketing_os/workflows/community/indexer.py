"""Review-gated import of sourced organization candidates for Department 6E.

This module does not scrape sources, infer people, verify identities, create
prospects, or queue outreach. An approved operator supplies source-backed rows.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from lumen_marketing_os.core.time import utc_now, utc_text
from lumen_marketing_os.db.database import Database, append_audit
from lumen_marketing_os.policy.pause import PauseControl


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _text(value: object, *, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _source_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
                parsed.username or parsed.password or parsed.port not in {None, 80, 443}):
            return None
        hostname = parsed.hostname.lower().rstrip(".")
        if "." not in hostname or any(not label for label in hostname.split(".")):
            return None
        netloc = hostname + (f":{parsed.port}" if parsed.port else "")
        return urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))
    except ValueError:
        return None


def _domain(value: str) -> str | None:
    try:
        if "://" in value:
            value = urlsplit(value).hostname or ""
        value = value.strip().lower().rstrip(".").removeprefix("www.")
        if len(value) > 253 or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", value):
            return None
        if len(value.rsplit(".", 1)[1]) < 2:
            return None
        return value
    except ValueError:
        return None


class ProspectListIndexer:
    """Import JSON rows atomically, preserving source evidence and review gates."""

    def __init__(self, database: Database, *, pause: PauseControl | None = None,
                 clock: Callable[[], datetime] = utc_now) -> None:
        self.db = database
        self.pause = pause
        self.clock = clock

    def import_rows(self, *, source_name: str, source_url: str, source_version: str,
                    approved_by: str, rows: Sequence[Mapping[str, object]],
                    task_id: str | None = None, worker_id: str | None = None) -> dict:
        if self.pause is not None:
            self.pause.guard("community")
        source_name = _text(source_name, limit=200)
        source_version = _text(source_version, limit=100)
        approved_by = _text(approved_by, limit=200)
        source_url = _source_url(_text(source_url, limit=2000)) or ""
        if not all((source_name, source_version, approved_by, source_url)):
            raise ValueError("source name, public URL, version, and approver are required")
        if (task_id is None) != (worker_id is None):
            raise ValueError("task ID and worker ID must be supplied together")
        if not isinstance(rows, (list, tuple)) or not rows or any(
                not isinstance(row, Mapping) for row in rows):
            raise ValueError("rows must be a non-empty list of JSON objects")
        # Reject non-JSON values before entering the transaction. The complete
        # submitted content is never copied to audit records.
        submitted = _json({"source_url": source_url, "rows": list(rows)})
        input_hash = hashlib.sha256(submitted.encode("utf-8")).hexdigest()
        now = utc_text(self.clock())
        with self.db.transaction() as connection:
            if task_id is not None:
                task = connection.execute(
                    "SELECT agent_id,state,lease_owner,lease_expires_at_utc,input_json "
                    "FROM community_tasks WHERE id=?", (task_id,),
                ).fetchone()
                if (task is None or task["agent_id"] != "6E" or task["state"] != "leased" or
                        task["lease_owner"] != worker_id or not task["lease_expires_at_utc"] or
                        task["lease_expires_at_utc"] <= now):
                    raise ValueError("6E task must have a current lease owned by this worker")
                scope = json.loads(task["input_json"])
                if (scope.get("source_name") != source_name or
                        _source_url(scope.get("source_url", "")) != source_url):
                    raise ValueError("import source differs from claimed 6E task")
            previous = connection.execute(
                "SELECT id FROM community_import_batches WHERE source_name=? AND "
                "source_version=? AND input_sha256=?",
                (source_name, source_version, input_hash),
            ).fetchone()
            if previous:
                if task_id is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO community_task_import_links "
                        "(task_id,batch_id,linked_at_utc) VALUES (?,?,?)",
                        (task_id, previous["id"], now),
                    )
                result = self._batch_report(connection, previous["id"])
                result["reimported"] = True
                return result
            batch_id = uuid4().hex
            connection.execute(
                "INSERT INTO community_import_batches(id,source_name,source_url,source_version,"
                "input_sha256,approved_by,created_at_utc,submitted_rows) VALUES (?,?,?,?,?,?,?,?)",
                (batch_id, source_name, source_url, source_version, input_hash,
                 approved_by, now, len(rows)),
            )
            if task_id is not None:
                connection.execute(
                    "INSERT INTO community_task_import_links "
                    "(task_id,batch_id,linked_at_utc) VALUES (?,?,?)",
                    (task_id, batch_id, now),
                )
            seen: set[str] = set()
            for raw in rows:
                row = dict(raw)
                raw_url = _text(row.get("source_url"), limit=2000)
                url = _source_url(raw_url)
                company = _text(row.get("company_name"), limit=250)
                raw_domain = _text(row.get("domain"), limit=300)
                domain = _domain(raw_domain)
                country = _text(row.get("country"), limit=16).upper()
                normalized = _name(company)
                # A source row is identified by the source URL plus the source's
                # named company. Other field changes remain separate evidence.
                row_key = _hash({"source_url": url, "company_name": normalized,
                                 "domain": domain, "country": country}) if (
                                     url and normalized and domain and country) else _hash(row)
                if row_key in seen:
                    continue
                seen.add(row_key)
                reason = None
                if not url:
                    reason = "invalid_source_url"
                elif len(normalized) < 2:
                    reason = "invalid_company_name"
                elif not domain:
                    reason = "invalid_domain"
                elif not re.fullmatch(r"[A-Z]{2}", country):
                    reason = "invalid_country_code"
                org_id = intake_id = None
                state = "quarantined" if reason else "sent_to_qualification"
                if reason is None:
                    field_provenance = row.get("field_provenance", {})
                    if not isinstance(field_provenance, Mapping) or any(
                            key not in {"company_name", "domain", "country"} or
                            not isinstance(value, str) or len(value) > 200
                            for key, value in field_provenance.items()):
                        raise ValueError("field_provenance must map known fields to short source references")
                    observation_id = self._observation(
                        connection, url=url, company=company, domain=domain,
                        country=country, now=now,
                        source_row_id=_text(row.get("source_row_id"), limit=100),
                        field_provenance=dict(field_provenance),
                    )
                    candidates = connection.execute(
                        "SELECT id,normalized_name,country_code,identity_status FROM organizations "
                        "WHERE canonical_domain=? AND identity_status!='merged'",
                        (domain,),
                    ).fetchall()
                    if len(candidates) > 1:
                        reason = "ambiguous_domain_match"
                    elif candidates:
                        match = candidates[0]
                        if match["identity_status"] != "candidate":
                            reason = "existing_non_candidate_identity"
                        elif match["normalized_name"] != normalized or (
                                match["country_code"] and match["country_code"] != country):
                            reason = "conflicting_existing_identity"
                        else:
                            org_id = match["id"]
                    else:
                        conflicting = connection.execute(
                            "SELECT 1 FROM organizations WHERE normalized_name=? AND country_code=? "
                            "AND canonical_domain IS NOT NULL AND canonical_domain!=? "
                            "AND identity_status!='merged' LIMIT 1",
                            (normalized, country, domain),
                        ).fetchone()
                        if conflicting:
                            reason = "ambiguous_name_country_match"
                    if reason:
                        state = "quarantined"
                    else:
                        if org_id is None:
                            org_id = uuid4().hex
                            connection.execute(
                                "INSERT INTO organizations(id,display_name,normalized_name,"
                                "country_code,canonical_domain,identity_status,created_at_utc,"
                                "updated_at_utc) VALUES (?,?,?,?,?,'candidate',?,?)",
                                (org_id, company, normalized, country, domain, now, now),
                            )
                        connection.execute(
                            "INSERT OR IGNORE INTO observation_links(source_observation_id,"
                            "entity_type,entity_id,relation) VALUES (?,'organization',?,'import_source')",
                            (observation_id, org_id),
                        )
                        # finding_id is intentionally NULL. Existing intake
                        # pathways only admit *reviewed* research findings; this
                        # record remains a separate human-review intake.
                        intake = connection.execute(
                            "SELECT id,state FROM qualification_intakes WHERE organization_id=? "
                            "AND finding_id IS NULL ORDER BY created_at_utc LIMIT 1",
                            (org_id,),
                        ).fetchone()
                        if intake:
                            if intake["state"] != "needs_review":
                                state = "quarantined"
                                reason = "prior_review_decision"
                                org_id = None
                            else:
                                intake_id = intake["id"]
                        else:
                            intake_id = uuid4().hex
                            connection.execute(
                                "INSERT INTO qualification_intakes(id,organization_id,"
                                "source_observation_id,finding_id,state,created_at_utc) "
                                "VALUES (?,?,?,NULL,'needs_review',?)",
                                (intake_id, org_id, observation_id, now),
                            )
                connection.execute(
                    "INSERT INTO community_import_rows(id,batch_id,row_key,source_url,company_name,"
                    "domain,country_code,state,reason,organization_id,intake_id,created_at_utc) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid4().hex, batch_id, row_key, url or raw_url or None,
                     company or None, domain or raw_domain or None,
                     country or None, state, reason, org_id, intake_id, now),
                )
            append_audit(connection, actor="6E", event_type="community.import_completed",
                         entity_type="community_import_batch", entity_id=batch_id,
                         details_json=_json({"source_name": source_name,
                                             "source_version": source_version,
                                             "submitted_rows": len(rows),
                                             "distinct_rows": len(seen)}),
                         occurred_at_utc=now)
            result = self._batch_report(connection, batch_id)
            result["reimported"] = False
            return result

    @staticmethod
    def _observation(connection, *, url: str, company: str, domain: str,
                     country: str, now: str, source_row_id: str,
                     field_provenance: dict[str, str]) -> str:
        payload = {"company_name": company, "domain": domain, "country": country,
                   "source_row_id": source_row_id, "field_provenance": field_provenance}
        digest = _hash(payload)
        previous = connection.execute(
            "SELECT id FROM source_observations WHERE source_type='community:directory' "
            "AND canonical_url=? AND content_hash=?", (url, digest),
        ).fetchone()
        if previous:
            return previous["id"]
        observation_id = uuid4().hex
        connection.execute(
            "INSERT INTO source_observations(id,source_type,external_id,canonical_url,"
            "content_hash,excerpt,permitted_use,observed_at_utc,fetched_at_utc,"
            "created_at_utc) VALUES (?,'community:directory',NULL,?,?,?,"
            "'internal_research',?,?,?)",
            (observation_id, url, digest, _json(payload), now, now, now),
        )
        return observation_id

    def report(self, *, batch_id: str | None = None) -> dict:
        with self.db.read() as connection:
            if batch_id is not None:
                if not connection.execute("SELECT 1 FROM community_import_batches WHERE id=?",
                                          (batch_id,)).fetchone():
                    raise ValueError("unknown import batch")
                return self._batch_report(connection, batch_id)
            batches = connection.execute(
                "SELECT COUNT(*) FROM community_import_batches").fetchone()[0]
            counts = {row["state"]: row["n"] for row in connection.execute(
                "SELECT state,COUNT(*) AS n FROM community_import_rows GROUP BY state")}
            return {"batches": batches, "rows": sum(counts.values()),
                    "states": counts}

    @staticmethod
    def _batch_report(connection, batch_id: str) -> dict:
        counts = {row["state"]: row["n"] for row in connection.execute(
            "SELECT state,COUNT(*) AS n FROM community_import_rows WHERE batch_id=? "
            "GROUP BY state", (batch_id,))}
        reasons = {row["reason"]: row["n"] for row in connection.execute(
            "SELECT reason,COUNT(*) AS n FROM community_import_rows WHERE batch_id=? "
            "AND reason IS NOT NULL GROUP BY reason", (batch_id,))}
        submitted = connection.execute(
            "SELECT submitted_rows FROM community_import_batches WHERE id=?", (batch_id,),
        ).fetchone()["submitted_rows"]
        items = [dict(row) for row in connection.execute(
            "SELECT source_url,company_name,domain,country_code,state,reason,"
            "organization_id,intake_id FROM community_import_rows WHERE batch_id=? "
            "ORDER BY source_url,company_name,domain", (batch_id,),
        )]
        task_ids = [row["task_id"] for row in connection.execute(
            "SELECT task_id FROM community_task_import_links WHERE batch_id=? ORDER BY task_id",
            (batch_id,),
        )]
        return {"batch_id": batch_id, "rows": sum(counts.values()),
                "submitted_rows": submitted, "duplicate_rows": max(0, submitted - sum(counts.values())),
                "states": counts, "quarantine_reasons": reasons,
                "task_ids": task_ids, "items": items}
