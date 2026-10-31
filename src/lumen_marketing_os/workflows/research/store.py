"""Idempotent evidence and candidate projections for the research department."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import urllib.parse
from dataclasses import dataclass, field
from uuid import uuid4

from lumen_marketing_os.adapters.research import SourceItem
from lumen_marketing_os.core.time import utc_now, utc_text
from lumen_marketing_os.db.database import append_audit


@dataclass(frozen=True)
class FindingDraft:
    kind: str
    title: str
    summary: str
    evidence_excerpt: str
    subject_name: str | None = None
    subject_domain: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SourceResult:
    key: str
    kind: str
    items: tuple[SourceItem, ...] = ()
    error_class: str | None = None


@dataclass(frozen=True)
class StoreResult:
    observations_new: int
    findings_new: int
    organization_ids: tuple[str, ...]
    finding_ids: tuple[str, ...]


def _fingerprint(item: SourceItem) -> str:
    content = f"{item.title}\n{item.excerpt}".encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower().removeprefix("www.")
    if host in {"news.ycombinator.com", "techcrunch.com", "reddit.com",
                "github.com", "youtube.com", "x.com", "twitter.com"}:
        return None
    return host


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


class ResearchStore:
    def __init__(self, connection: sqlite3.Connection, *, now: str | None = None) -> None:
        self.connection = connection
        self.now = now or utc_text(utc_now())

    def record_source(self, *, agent_id: str, item: SourceItem) -> tuple[str, bool, str | None]:
        source_type = f"{agent_id}:{item.source_kind}"
        digest = _fingerprint(item)
        previous = self.connection.execute(
            "SELECT excerpt,content_hash FROM source_observations WHERE source_type=? "
            "AND canonical_url=? ORDER BY observed_at_utc DESC, rowid DESC LIMIT 1",
            (source_type, item.url),
        ).fetchone()
        row = self.connection.execute(
            "SELECT id FROM source_observations WHERE source_type=? AND canonical_url=? "
            "AND content_hash=?", (source_type, item.url, digest),
        ).fetchone()
        if row:
            return row["id"], False, previous["excerpt"] if previous else None
        observation_id = uuid4().hex
        self.connection.execute(
            "INSERT INTO source_observations(id,source_type,external_id,canonical_url,"
            "content_hash,excerpt,permitted_use,observed_at_utc,fetched_at_utc,created_at_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (observation_id, source_type,
             f"{item.external_id}:{digest[:16]}" if item.external_id else None,
             item.url, digest, item.excerpt[:20_000], "internal_research",
             self.now, self.now, self.now),
        )
        append_audit(self.connection, actor=agent_id, event_type="research.observed",
                     entity_type="source_observation", entity_id=observation_id,
                     occurred_at_utc=self.now)
        return observation_id, True, previous["excerpt"] if previous else None

    def record_finding(self, *, agent_id: str, observation_id: str,
                       url: str, draft: FindingDraft) -> str | None:
        row = self.connection.execute(
            "SELECT id FROM research_findings WHERE agent_id=? AND source_observation_id=? "
            "AND finding_type=?", (agent_id, observation_id, draft.kind),
        ).fetchone()
        if row:
            return None
        finding_id = uuid4().hex
        self.connection.execute(
            "INSERT INTO research_findings(id,agent_id,source_observation_id,finding_type,"
            "title,summary,subject_name,subject_domain,evidence_url,evidence_excerpt,"
            "metadata_json,created_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (finding_id, agent_id, observation_id, draft.kind, draft.title[:500],
             draft.summary[:3000], draft.subject_name, draft.subject_domain,
             url, draft.evidence_excerpt[:3000],
             json.dumps(draft.metadata, sort_keys=True, ensure_ascii=False), self.now),
        )
        append_audit(self.connection, actor=agent_id, event_type="research.finding_created",
                     entity_type="research_finding", entity_id=finding_id,
                     occurred_at_utc=self.now)
        return finding_id

    def candidate_organization(self, *, name: str, domain: str | None,
                               observation_id: str) -> str | None:
        normalized = _normalize_name(name)
        if not domain or not normalized or len(normalized) < 3:
            return None
        row = self.connection.execute(
            "SELECT id FROM organizations WHERE canonical_domain=? AND identity_status!='merged' "
            "ORDER BY created_at_utc LIMIT 1", (domain,),
        ).fetchone()
        if row:
            return None
        org_id = uuid4().hex
        self.connection.execute(
            "INSERT INTO organizations(id,display_name,normalized_name,canonical_domain,"
            "identity_status,created_at_utc,updated_at_utc) VALUES (?,?,?,?, 'candidate',?,?)",
            (org_id, name[:250], normalized, domain, self.now, self.now),
        )
        self.connection.execute(
            "INSERT INTO observation_links(source_observation_id,entity_type,entity_id,relation) "
            "VALUES (?,?,?,?)", (observation_id, "organization", org_id, "discovery_source"),
        )
        append_audit(self.connection, actor="1E", event_type="research.organization_candidate",
                     entity_type="organization", entity_id=org_id, occurred_at_utc=self.now)
        return org_id

    def health(self, *, agent_id: str, result: SourceResult) -> None:
        previous = self.connection.execute(
            "SELECT last_success_at_utc FROM research_source_health WHERE source_key=?",
            (result.key,),
        ).fetchone()
        status = "ok" if not result.error_class else (
            "blocked" if result.error_class in {"access_denied", "invalid_public_url"}
            else "degraded")
        last_success = self.now if not result.error_class else (
            previous["last_success_at_utc"] if previous else None)
        self.connection.execute(
            "INSERT INTO research_source_health(source_key,agent_id,source_kind,"
            "last_checked_at_utc,last_success_at_utc,status,error_class,item_count) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET "
            "last_checked_at_utc=excluded.last_checked_at_utc,"
            "last_success_at_utc=excluded.last_success_at_utc,status=excluded.status,"
            "error_class=excluded.error_class,item_count=excluded.item_count",
            (result.key, agent_id, result.kind, self.now, last_success,
             status, result.error_class, len(result.items)),
        )
