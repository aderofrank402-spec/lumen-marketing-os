"""Department 9: durable feedback intake, triage, dashboard projection and reply review.

Google Workspace access in Codex is not an OAuth credential for this local process.
The sheet bridge exports versioned projections and accepts exact acknowledgements;
only the existing Department 4 action gate can send an approved email reply.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Callable
from uuid import uuid4

from lumen_marketing_os.core.errors import InvalidStateError, NotFoundError
from lumen_marketing_os.core.time import parse_utc, utc_now, utc_text
from lumen_marketing_os.db.database import Database, append_audit
from lumen_marketing_os.policy.pause import PauseControl
from lumen_marketing_os.workflows.outreach.runtime import OutreachRuntime


def _text(value: str, label: str, maximum: int = 5000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must contain 1..{maximum} characters")
    return value.strip()


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                      separators=(",", ":")).encode("utf-8")).hexdigest()


def classify_text(subject: str, body: str) -> tuple[str, str, float, str]:
    """Conservative routing hints; low-confidence items require human review."""
    text = f"{subject}\n{body}".lower()
    if re.search(r"\b(charged twice|unauthori[sz]ed charge|refund|invoice|payment failed)\b", text):
        return "billing", "high", .86, "billing terms present; financial review required"
    if re.search(r"\b(data leak|breach|security vulnerability|exposed (?:api )?(?:data|key)|account takeover)\b", text):
        return "support", "urgent", .90, "possible security incident; escalate without automated reply"
    if re.search(r"\b(bug|error|crash(?:es|ed)?|broken|does not work|failed to load)\b", text):
        return "bug", "normal", .78, "failure report terms present"
    if re.search(r"\b(feature request|please add|would be useful if|integration request)\b", text):
        return "feature_request", "normal", .78, "feature request terms present"
    if re.search(r"\b(pricing|demo|purchase|buy|sales call|trial)\b", text):
        return "sales", "normal", .72, "commercial inquiry terms present"
    if re.search(r"\b(help|how do i|how can i|support)\b", text):
        return "support", "normal", .65, "help request terms present"
    return "other", "normal", .25, "no reliable intent signal; manual triage required"


class SupportRuntime:
    def __init__(self, db: Database, pause: PauseControl, *, clock: Callable = utc_now) -> None:
        self.db, self.pause, self.clock = db, pause, clock

    def _now(self) -> str:
        return utc_text(self.clock())

    def _guard(self) -> None:
        self.pause.guard("support")

    def tick(self, *, limit: int = 50) -> dict:
        """Bounded local triage; provider polling and public replies are separate."""
        self._guard()
        if not 1 <= limit <= 500:
            raise ValueError("limit must be 1..500")
        with self.db.read() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM support_tickets WHERE category='unclassified' "
                                               "AND state='new' ORDER BY received_at_utc,id LIMIT ?", (limit,))]
        results = []
        for ticket_id in ids:
            results.append(self.triage(ticket_id=ticket_id, actor="scheduled-9B"))
        return {"scanned": len(ids), "triaged": len(results), "tickets": results}

    def register_source(self, *, key: str, kind: str, external_reference: str,
                        actor: str, account_id: str | None = None) -> str:
        self._guard()
        key, external_reference, actor = (_text(key, "source key", 120),
                                          _text(external_reference, "external reference", 500),
                                          _text(actor, "actor", 150))
        if kind not in {"gmail", "form", "product_event", "manual"}:
            raise ValueError("unsupported support source kind")
        if kind == "gmail" and not account_id:
            raise ValueError("gmail source requires an existing outreach account")
        now = self._now()
        with self.db.transaction() as c:
            if account_id and not c.execute("SELECT 1 FROM outreach_accounts WHERE id=?", (account_id,)).fetchone():
                raise NotFoundError("outreach account not found")
            row = c.execute("SELECT id,kind,external_reference,account_id FROM support_sources WHERE source_key=?",
                            (key,)).fetchone()
            if row:
                if (row["kind"], row["external_reference"], row["account_id"]) != (kind, external_reference, account_id):
                    raise InvalidStateError("source key already has a different configuration")
                return row["id"]
            source_id = uuid4().hex
            c.execute("INSERT INTO support_sources(id,source_key,kind,account_id,external_reference,"
                      "created_at_utc,updated_at_utc) VALUES (?,?,?,?,?,?,?)",
                      (source_id, key, kind, account_id, external_reference, now, now))
            append_audit(c, actor=actor, event_type="support.source_registered", entity_type="support_source",
                         entity_id=source_id, occurred_at_utc=now)
            return source_id

    def enable_source(self, *, source_id: str, actor: str, verified: bool) -> None:
        self._guard()
        if not verified:
            raise InvalidStateError("source access must be verified before enabling")
        actor, now = _text(actor, "actor", 150), self._now()
        with self.db.transaction() as c:
            row = c.execute("SELECT kind,account_id FROM support_sources WHERE id=?", (source_id,)).fetchone()
            if not row:
                raise NotFoundError("support source not found")
            if row["kind"] == "gmail":
                account = c.execute("SELECT enabled FROM outreach_accounts WHERE id=?", (row["account_id"],)).fetchone()
                if not account or not account["enabled"]:
                    raise InvalidStateError("gmail outreach account is not enabled")
            c.execute("UPDATE support_sources SET enabled=1,verified_by=?,updated_at_utc=? WHERE id=?",
                      (actor, now, source_id))
            append_audit(c, actor=actor, event_type="support.source_enabled", entity_type="support_source",
                         entity_id=source_id, occurred_at_utc=now)

    def ingest_batch(self, *, source_id: str, items: list[dict], actor: str,
                     next_cursor: str | None = None) -> dict:
        """Atomically ingest an authorized connector page and advance its cursor."""
        self._guard()
        actor = _text(actor, "actor", 150)
        if not isinstance(items, list) or len(items) > 500:
            raise ValueError("items must be a JSON array of at most 500 events")
        now, ids, duplicates = self._now(), [], 0
        with self.db.transaction() as c:
            source = c.execute("SELECT * FROM support_sources WHERE id=?", (source_id,)).fetchone()
            if not source or not source["enabled"]:
                raise InvalidStateError("support source is not enabled")
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("each event must be an object")
                provider_id = _text(item.get("provider_event_id"), "provider event ID", 500)
                existing = c.execute("SELECT id FROM support_tickets WHERE source_id=? AND provider_event_id=?",
                                     (source_id, provider_id)).fetchone()
                if existing:
                    ids.append(existing["id"])
                    duplicates += 1
                    continue
                inbound_id = item.get("inbound_event_id")
                if source["kind"] == "gmail":
                    if not inbound_id:
                        raise ValueError("gmail ticket requires a locally ingested inbound event")
                    inbound = c.execute(
                        "SELECT i.provider_event_id,m.sender,m.subject,m.body,m.recipient "
                        "FROM inbound_events i JOIN messages msg ON msg.provider_message_id=i.provider_event_id "
                        "JOIN inbound_message_content m ON m.message_id=msg.id WHERE i.id=? AND i.provider='gmail'",
                        (inbound_id,)).fetchone()
                    account = c.execute("SELECT email_address FROM outreach_accounts WHERE id=?",
                                        (source["account_id"],)).fetchone()
                    if not inbound or not account or inbound["recipient"] != account["email_address"] or \
                            inbound["provider_event_id"] != provider_id:
                        raise InvalidStateError("gmail event does not match the authorized source account")
                    subject, body, sender = inbound["subject"] or "(no subject)", inbound["body"], inbound["sender"]
                else:
                    if inbound_id:
                        raise ValueError("non-gmail event cannot link a gmail inbound event")
                    subject = _text(item.get("subject"), "subject", 300)
                    body = _text(item.get("body"), "body", 50000)
                    sender = item.get("sender")
                    if sender is not None:
                        sender = _text(sender, "sender", 500)
                received = utc_text(parse_utc(_text(item.get("received_at_utc"), "received time", 100)))
                ticket_id = uuid4().hex
                c.execute("INSERT INTO support_tickets(id,source_event_id,category,severity,status,source_id,"
                          "provider_event_id,inbound_event_id,subject,body,sender,received_at_utc,created_at_utc,"
                          "updated_at_utc) VALUES (? ,?,'unclassified','normal','new',?,?,?,?,?,?,?,?,?)",
                          (ticket_id, inbound_id, source_id, provider_id, inbound_id, subject, body,
                           sender, received, now, now))
                c.execute("INSERT INTO support_ticket_events(id,ticket_id,event_key,event_type,payload_json,actor,"
                          "occurred_at_utc) VALUES (?,?,?,?,?,?,?)",
                          (uuid4().hex, ticket_id, f"ingest:{source_id}:{provider_id}", "received",
                           json.dumps({"source": source["source_key"]}), actor, now))
                append_audit(c, actor=actor, event_type="support.ticket_received", entity_type="support_ticket",
                             entity_id=ticket_id, occurred_at_utc=now)
                ids.append(ticket_id)
            if next_cursor is not None:
                c.execute("UPDATE support_sources SET cursor_value=?,updated_at_utc=? WHERE id=?",
                          (_text(next_cursor, "cursor", 2000), now, source_id))
        return {"ticket_ids": ids, "created": len(ids) - duplicates, "duplicates": duplicates,
                "cursor_committed": next_cursor is not None}

    def triage(self, *, ticket_id: str, actor: str, classifier_version: str = "rules-9b-v1") -> dict:
        self._guard()
        actor, classifier_version = _text(actor, "actor", 150), _text(classifier_version, "classifier version", 100)
        now = self._now()
        with self.db.transaction() as c:
            ticket = c.execute("SELECT * FROM support_tickets WHERE id=?", (ticket_id,)).fetchone()
            if not ticket:
                raise NotFoundError("support ticket not found")
            prior = c.execute("SELECT * FROM support_classifications WHERE ticket_id=? AND ticket_version=? "
                              "AND classifier_version=?", (ticket_id, ticket["version"], classifier_version)).fetchone()
            if prior:
                return dict(prior)
            if ticket["category"] != "unclassified":
                raise InvalidStateError("ticket already triaged; use human review to change category")
            category, priority, confidence, rationale = classify_text(ticket["subject"], ticket["body"])
            state = "needs_review" if confidence < .8 or category == "billing" or priority == "urgent" else "open"
            record_id = uuid4().hex
            c.execute("INSERT INTO support_classifications(id,ticket_id,ticket_version,category,priority,confidence,"
                      "rationale,classifier_version,created_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                      (record_id, ticket_id, ticket["version"], category, priority, confidence,
                       rationale, classifier_version, now))
            c.execute("UPDATE support_tickets SET category=?,severity=?,status=?,priority=?,state=?,"
                      "version=version+1,updated_at_utc=? WHERE id=?",
                      (category, priority, state, priority, state, now, ticket_id))
            c.execute("UPDATE support_sheet_sync SET state='pending',updated_at_utc=? WHERE ticket_id=?",
                      (now, ticket_id))
            self._event(c, ticket_id, f"triage:{ticket_id}:{classifier_version}", "classified",
                        {"category": category, "priority": priority, "confidence": confidence}, actor, now)
            return {"id": record_id, "ticket_id": ticket_id, "category": category,
                    "priority": priority, "confidence": confidence, "state": state}

    @staticmethod
    def _event(c, ticket_id: str, key: str, kind: str, payload: dict, actor: str, now: str) -> None:
        c.execute("INSERT INTO support_ticket_events(id,ticket_id,event_key,event_type,payload_json,actor,"
                  "occurred_at_utc) VALUES (?,?,?,?,?,?,?)",
                  (uuid4().hex, ticket_id, key, kind, json.dumps(payload, sort_keys=True), actor, now))
        append_audit(c, actor=actor, event_type=f"support.{kind}", entity_type="support_ticket",
                     entity_id=ticket_id, details_json=json.dumps(payload, sort_keys=True), occurred_at_utc=now)

    def review(self, *, ticket_id: str, category: str, priority: str, state: str,
               actor: str, rationale: str) -> dict:
        self._guard()
        if category not in {"bug", "feature_request", "sales", "support", "billing", "other"} or \
                priority not in {"low", "normal", "high", "urgent"} or \
                state not in {"needs_review", "open", "waiting_customer", "resolved", "closed"}:
            raise ValueError("invalid reviewed ticket values")
        actor, rationale, now = _text(actor, "actor", 150), _text(rationale, "rationale", 1000), self._now()
        with self.db.transaction() as c:
            ticket = c.execute("SELECT * FROM support_tickets WHERE id=?", (ticket_id,)).fetchone()
            if not ticket:
                raise NotFoundError("support ticket not found")
            version = ticket["version"] + 1
            c.execute("UPDATE support_tickets SET category=?,severity=?,status=?,priority=?,state=?,version=?,"
                      "updated_at_utc=? WHERE id=?", (category, priority, state, priority, state,
                                                    version, now, ticket_id))
            c.execute("UPDATE support_sheet_sync SET state='pending',updated_at_utc=? WHERE ticket_id=?",
                      (now, ticket_id))
            c.execute("UPDATE support_response_drafts SET state='retired' WHERE ticket_id=? AND state='approved'",
                      (ticket_id,))
            self._event(c, ticket_id, f"review:{ticket_id}:{version}", "reviewed",
                        {"category": category, "priority": priority, "state": state,
                         "rationale": rationale}, actor, now)
            return {"ticket_id": ticket_id, "version": version, "state": state}

    def draft_response(self, *, ticket_id: str, subject: str, body: str, author: str) -> str:
        self._guard()
        subject, body, author = (_text(subject, "subject", 300), _text(body, "body", 12000),
                                 _text(author, "author", 150))
        if "\r" in subject or "\n" in subject:
            raise ValueError("reply subject must be one line")
        now, digest = self._now(), _hash({"subject": subject, "body": body})
        with self.db.transaction() as c:
            ticket = c.execute("SELECT * FROM support_tickets WHERE id=?", (ticket_id,)).fetchone()
            if not ticket or ticket["state"] not in {"open", "waiting_customer"}:
                raise InvalidStateError("ticket needs human review and an open state before drafting")
            existing = c.execute("SELECT id FROM support_response_drafts WHERE ticket_id=? AND ticket_version=? "
                                 "AND body_sha256=?", (ticket_id, ticket["version"], digest)).fetchone()
            if existing:
                return existing["id"]
            draft_id = uuid4().hex
            c.execute("INSERT INTO support_response_drafts(id,ticket_id,ticket_version,subject,body,body_sha256,"
                      "state,author,created_at_utc) VALUES (?,?,?,?,?,?,'needs_review',?,?)",
                      (draft_id, ticket_id, ticket["version"], subject, body, digest, author, now))
            self._event(c, ticket_id, f"draft:{draft_id}", "response_drafted",
                        {"draft_id": draft_id, "hash": digest}, author, now)
            return draft_id

    def approve_response(self, *, draft_id: str, reviewer: str, expected_hash: str) -> None:
        self._guard()
        reviewer = _text(reviewer, "reviewer", 150)
        now = self._now()
        with self.db.transaction() as c:
            row = c.execute("SELECT d.*,t.version,t.state AS ticket_state FROM support_response_drafts d "
                            "JOIN support_tickets t ON t.id=d.ticket_id WHERE d.id=?", (draft_id,)).fetchone()
            if not row or row["state"] != "needs_review" or row["ticket_version"] != row["version"] or \
                    row["body_sha256"] != expected_hash or row["ticket_state"] in {"resolved", "closed"}:
                raise InvalidStateError("draft is stale, changed, or not reviewable")
            if row["author"] == reviewer:
                raise InvalidStateError("response requires an independent reviewer")
            c.execute("UPDATE support_response_drafts SET state='approved',approved_by=?,approved_at_utc=? WHERE id=?",
                      (reviewer, now, draft_id))
            self._event(c, row["ticket_id"], f"approve:{draft_id}", "response_approved",
                        {"draft_id": draft_id, "hash": expected_hash}, reviewer, now)

    def handoff_email(self, *, draft_id: str, outreach: OutreachRuntime, actor: str) -> dict:
        """Create a Department 4 reply proposal only; never approve or send the action."""
        self._guard()
        actor = _text(actor, "actor", 150)
        with self.db.read() as c:
            row = c.execute("SELECT d.*,t.inbound_event_id,t.version,t.state AS ticket_state,"
                            "s.account_id FROM support_response_drafts d "
                            "JOIN support_tickets t ON t.id=d.ticket_id "
                            "JOIN support_sources s ON s.id=t.source_id WHERE d.id=?", (draft_id,)).fetchone()
            if not row or row["state"] != "approved" or row["ticket_version"] != row["version"] or \
                    row["ticket_state"] in {"resolved", "closed"} or not row["inbound_event_id"] or \
                    not row["account_id"]:
                raise InvalidStateError("only a current approved Gmail response can be handed to outreach")
            prior = c.execute("SELECT id,subject,body FROM response_proposals WHERE event_id=?",
                              (row["inbound_event_id"],)).fetchone()
        if prior:
            if (prior["subject"], prior["body"]) != (row["subject"], row["body"]):
                raise InvalidStateError("inbound event already has a different response proposal")
            proposal_id = prior["id"]
        else:
            proposal_id = outreach.propose_response(event_id=row["inbound_event_id"],
                                                    account_id=row["account_id"],
                                                    subject=row["subject"], body=row["body"],
                                                    slots=[], author=actor)
        with self.db.transaction() as c:
            c.execute("INSERT OR IGNORE INTO support_response_handoffs(support_draft_id,outreach_proposal_id,"
                      "created_at_utc) VALUES (?,?,?)", (draft_id, proposal_id, self._now()))
        return {"support_draft_id": draft_id, "outreach_proposal_id": proposal_id,
                "state": "outreach_review_pending", "sent": False}

    def sheet_projection(self, *, ticket_id: str, spreadsheet_id: str, actor: str) -> dict:
        self._guard()
        spreadsheet_id, actor, now = (_text(spreadsheet_id, "spreadsheet ID", 300),
                                      _text(actor, "actor", 150), self._now())
        with self.db.transaction() as c:
            row = c.execute("SELECT id,subject,category,priority,state,version,received_at_utc,updated_at_utc "
                            "FROM support_tickets WHERE id=?", (ticket_id,)).fetchone()
            if not row:
                raise NotFoundError("support ticket not found")
            fields = dict(row)
            digest = _hash(fields)
            existing = c.execute("SELECT spreadsheet_id,synced_version,synced_hash FROM support_sheet_sync "
                                 "WHERE ticket_id=?", (ticket_id,)).fetchone()
            if existing and existing["spreadsheet_id"] != spreadsheet_id:
                raise InvalidStateError("ticket already bound to a different support sheet")
            c.execute("INSERT INTO support_sheet_sync(ticket_id,spreadsheet_id,row_key,target_version,target_hash,"
                      "state,updated_at_utc) VALUES (?,?,?,?,?,'pending',?) ON CONFLICT(ticket_id) DO UPDATE SET "
                      "target_version=excluded.target_version,target_hash=excluded.target_hash,"
                      "state=CASE WHEN support_sheet_sync.synced_version=excluded.target_version AND "
                      "support_sheet_sync.synced_hash=excluded.target_hash THEN 'synced' ELSE 'pending' END,"
                      "updated_at_utc=excluded.updated_at_utc",
                      (ticket_id, spreadsheet_id, ticket_id, row["version"], digest, now))
            return {"ticket_id": ticket_id, "spreadsheet_id": spreadsheet_id, "row_key": ticket_id,
                    "version": row["version"], "hash": digest, "fields": fields,
                    "state": "synced" if existing and existing["synced_version"] == row["version"]
                    and existing["synced_hash"] == digest else "pending"}

    def acknowledge_sheet(self, *, ticket_id: str, spreadsheet_id: str, version: int,
                          digest: str, actor: str) -> None:
        self._guard()
        actor, now = _text(actor, "actor", 150), self._now()
        with self.db.transaction() as c:
            row = c.execute("SELECT * FROM support_sheet_sync WHERE ticket_id=?", (ticket_id,)).fetchone()
            current = c.execute("SELECT version FROM support_tickets WHERE id=?", (ticket_id,)).fetchone()
            if not row or row["spreadsheet_id"] != spreadsheet_id or row["target_version"] != version or \
                    row["target_hash"] != digest or not current or current["version"] != version:
                raise InvalidStateError("sheet acknowledgement does not match current projection")
            if row["state"] == "synced" and row["synced_version"] == version and row["synced_hash"] == digest:
                return
            c.execute("UPDATE support_sheet_sync SET synced_version=?,synced_hash=?,state='synced',"
                      "last_error=NULL,updated_at_utc=? WHERE ticket_id=?", (version, digest, now, ticket_id))
            self._event(c, ticket_id, f"sheet:{ticket_id}:{version}:{digest}", "sheet_synced",
                        {"spreadsheet_id": spreadsheet_id, "version": version}, actor, now)

    def ticket(self, ticket_id: str) -> dict:
        with self.db.read() as c:
            row = c.execute("SELECT * FROM support_tickets WHERE id=?", (ticket_id,)).fetchone()
            if not row:
                raise NotFoundError("support ticket not found")
            result = dict(row)
            result["classifications"] = [dict(x) for x in c.execute(
                "SELECT * FROM support_classifications WHERE ticket_id=? ORDER BY created_at_utc", (ticket_id,))]
            result["drafts"] = [dict(x) for x in c.execute(
                "SELECT * FROM support_response_drafts WHERE ticket_id=? ORDER BY created_at_utc", (ticket_id,))]
            result["sheet"] = dict(x) if (x := c.execute(
                "SELECT * FROM support_sheet_sync WHERE ticket_id=?", (ticket_id,)).fetchone()) else None
            return result

    def report(self, limit: int = 50) -> dict:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be 1..500")
        with self.db.read() as c:
            return {"sources": [dict(x) for x in c.execute("SELECT id,source_key,kind,external_reference,"
                    "enabled,cursor_value,verified_by FROM support_sources ORDER BY source_key")],
                    "tickets": [dict(x) for x in c.execute("SELECT id,subject,category,priority,state,version,"
                    "received_at_utc FROM support_tickets ORDER BY received_at_utc DESC LIMIT ?", (limit,))],
                    "pending_sheet": [dict(x) for x in c.execute("SELECT ticket_id,spreadsheet_id,"
                    "target_version,target_hash,state FROM support_sheet_sync WHERE state!='synced' ORDER BY updated_at_utc LIMIT ?",
                    (limit,))]}
