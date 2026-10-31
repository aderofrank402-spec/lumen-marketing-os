"""Department 4: fail-closed email, inbox, response and calendar orchestration.

Provider credentials are intentionally outside this module. A gateway is injected only
after its account and scopes have been verified; fixture gateways exercise the same gate.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Callable, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lumen_marketing_os.core.errors import InvalidStateError, NotFoundError, PausedError
from lumen_marketing_os.core.time import parse_utc, utc_now, utc_text
from lumen_marketing_os.db.database import Database, append_audit
from lumen_marketing_os.policy.pause import PauseControl
from lumen_marketing_os.workflows.content.model import content_hash


def _email(value: str) -> str:
    parsed = parseaddr(value)[1]
    if parsed != value.strip() or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", parsed):
        raise ValueError("a single valid email address is required")
    return parsed.lower()


def _required(value: str, name: str, limit: int = 12000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must contain 1..{limit} characters")
    return value.strip()


def _header(value: str, name: str) -> str:
    text = _required(value, name, 300)
    if "\r" in text or "\n" in text:
        raise ValueError(f"{name} must be a single line")
    return text


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _zone(name: str):
    return timezone.utc if name == "UTC" else ZoneInfo(name)


@dataclass(frozen=True)
class IncomingMail:
    provider_id: str
    thread_id: str
    sender: str
    recipient: str
    subject: str
    body: str
    received_at_utc: str


@dataclass(frozen=True)
class ProviderResult:
    status: str  # accepted, rejected, unknown
    provider_id: str | None = None
    diagnostic: str | None = None


class MailGateway(Protocol):
    def read_page(self, account: str, cursor: str | None) -> tuple[list[IncomingMail], str | None]: ...
    def send(self, *, account: str, recipient: str, subject: str, body: str,
             thread_id: str | None, action_key: str) -> ProviderResult: ...
    def reconcile(self, *, account: str, action_key: str,
                  provider_request_id: str) -> ProviderResult: ...


class CalendarGateway(Protocol):
    def available(self, *, calendar_id: str, start_at_utc: str,
                  end_at_utc: str) -> bool: ...
    def book(self, *, calendar_id: str, attendee: str, title: str,
             start_at_utc: str, end_at_utc: str, action_key: str) -> ProviderResult: ...
    def reconcile(self, *, calendar_id: str, action_key: str,
                  provider_request_id: str) -> ProviderResult: ...


class UnavailableGateway:
    """Fail closed until the operator completes Google authorization."""

    def __getattr__(self, name: str):
        raise InvalidStateError(f"provider gateway is not configured: {name}")


def classify(mail: IncomingMail) -> tuple[str, float, str]:
    """Conservative first pass; uncertainty is review, never automatic response."""
    text = f"{mail.subject}\n{mail.body}".lower()
    sender = mail.sender.lower()
    if any(x in sender for x in ("mailer-daemon", "postmaster")) or re.search(
            r"\b(delivery (failed|status notification)|undeliverable|address not found)\b", text):
        return "bounce", .98, "delivery failure pattern"
    if re.search(r"\b(unsubscribe|remove me|do not (email|contact)|stop emailing|opt[ -]out)\b", text):
        return "opt_out", .98, "explicit no-contact request"
    if re.search(r"\b(spam complaint|reported as spam)\b", text):
        return "complaint", .95, "complaint pattern"
    if re.search(r"\b(out of office|automatic reply|on vacation)\b", text):
        return "out_of_office", .92, "automatic response pattern"
    if re.search(r"\b(bug|error|does not work|support|help with my account)\b", text):
        return "support", .72, "support request pattern; review required"
    if re.search(r"\b(schedule|meeting|call|book a time|available (on|at))\b", text):
        return "meeting", .70, "meeting intent pattern; review required"
    if re.search(r"\b(interested|tell me more|sounds useful|let.?s talk)\b", text):
        return "warm", .68, "positive intent pattern; review required"
    if "?" in text:
        return "question", .55, "question detected; review required"
    return "needs_review", .20, "no reliable deterministic classification"


class OutreachRuntime:
    def __init__(self, *, database: Database, pause: PauseControl, suppression_key: bytes,
                 mail: MailGateway | None = None, calendar: CalendarGateway | None = None,
                 external_actions_enabled: bool = False, clock: Callable = utc_now) -> None:
        if len(suppression_key) < 32:
            raise ValueError("suppression key must contain at least 32 bytes")
        self.db, self.pause, self.clock = database, pause, clock
        self.suppression_key = suppression_key
        self.mail = mail
        self.calendar = calendar
        self.external_actions_enabled = external_actions_enabled

    def _now(self) -> str:
        return utc_text(self.clock())

    def _digest(self, subject_type: str, value: str) -> str:
        return hmac.new(self.suppression_key, f"{subject_type}:{value.lower()}".encode(),
                        hashlib.sha256).hexdigest()

    def register_account(self, *, email: str, policy_version: str, actor: str,
                         calendar_id: str | None = None) -> str:
        address = _email(email)
        _required(policy_version, "policy version", 100)
        _required(actor, "actor", 200)
        now, account_id = self._now(), uuid4().hex
        with self.db.transaction() as c:
            c.execute("INSERT INTO outreach_accounts(id,email_address,provider,enabled,calendar_id,"
                      "policy_version,approved_by,created_at_utc,updated_at_utc) "
                      "VALUES (?,?,'gmail',0,?,?,?,?,?)",
                      (account_id, address, calendar_id, policy_version, actor, now, now))
            append_audit(c, actor=actor, event_type="outreach.account_registered",
                         entity_type="outreach_account", entity_id=account_id, occurred_at_utc=now)
        return account_id

    def enable_account(self, *, account_id: str, actor: str, confirmed: bool) -> None:
        if not confirmed:
            raise InvalidStateError("explicit provider authorization confirmation required")
        _required(actor, "actor", 200)
        now = self._now()
        with self.db.transaction() as c:
            if c.execute("UPDATE outreach_accounts SET enabled=1,approved_by=?,updated_at_utc=? "
                         "WHERE id=?", (actor, now, account_id)).rowcount != 1:
                raise NotFoundError("outreach account not found")
            append_audit(c, actor=actor, event_type="outreach.account_enabled",
                         entity_type="outreach_account", entity_id=account_id, occurred_at_utc=now)

    def disable_account(self, *, account_id: str, actor: str, reason: str) -> None:
        _required(actor, "actor", 200)
        _required(reason, "reason", 1000)
        now = self._now()
        with self.db.transaction() as c:
            self._invalidate_account_actions(c, account_id, now)
            if c.execute("UPDATE outreach_accounts SET enabled=0,updated_at_utc=? WHERE id=?",
                         (now, account_id)).rowcount != 1:
                raise NotFoundError("outreach account not found")
            append_audit(c, actor=actor, event_type="outreach.account_disabled",
                         entity_type="outreach_account", entity_id=account_id,
                         details_json=json.dumps({"reason": reason}), occurred_at_utc=now)

    def set_send_window(self, *, account_id: str, timezone_name: str,
                        start_local: str, end_local: str, weekdays: list[int],
                        actor: str) -> None:
        try:
            _zone(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("valid IANA timezone required") from exc
        for value in (start_local, end_local):
            if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
                raise ValueError("window times must be HH:MM")
        if start_local >= end_local or not weekdays or any(type(day) is not int or day not in range(7)
                                                            for day in weekdays):
            raise ValueError("window must have end after start and valid weekdays 0..6")
        _required(actor, "actor", 200)
        now = self._now()
        with self.db.transaction() as c:
            self._invalidate_account_actions(c, account_id, now)
            if c.execute("UPDATE outreach_accounts SET send_timezone=?,send_start_local=?,"
                         "send_end_local=?,send_weekdays_json=?,updated_at_utc=? WHERE id=?",
                         (timezone_name, start_local, end_local,
                          json.dumps(sorted(set(weekdays))), now, account_id)).rowcount != 1:
                raise NotFoundError("outreach account not found")
            append_audit(c, actor=actor, event_type="outreach.send_window_approved",
                         entity_type="outreach_account", entity_id=account_id,
                         details_json=json.dumps({"timezone": timezone_name,
                                                  "start": start_local, "end": end_local,
                                                  "weekdays": sorted(set(weekdays))}),
                         occurred_at_utc=now)

    def set_quota(self, *, account_id: str, max_units: int, period_seconds: int,
                  policy_version: str, actor: str) -> str:
        if max_units < 1 or period_seconds < 60:
            raise ValueError("quota requires positive units and a period of at least 60 seconds")
        _required(policy_version, "policy version", 100)
        _required(actor, "actor", 200)
        now, policy_id = self._now(), uuid4().hex
        with self.db.transaction() as c:
            if not c.execute("SELECT 1 FROM outreach_accounts WHERE id=?", (account_id,)).fetchone():
                raise NotFoundError("outreach account not found")
            self._invalidate_account_actions(c, account_id, now)
            c.execute("UPDATE quota_policies SET active=0 WHERE account_id=? AND channel='email'",
                      (account_id,))
            c.execute("INSERT INTO quota_policies(id,channel,account_id,period_seconds,max_units,"
                      "policy_version,approved_by,approved_at_utc,active) "
                      "VALUES (?,'email',?,?,?,?,?, ?,1)",
                      (policy_id, account_id, period_seconds, max_units, policy_version, actor, now))
            append_audit(c, actor=actor, event_type="outreach.quota_approved",
                         entity_type="quota_policy", entity_id=policy_id, occurred_at_utc=now)
        return policy_id

    @staticmethod
    def _invalidate_account_actions(c: sqlite3.Connection, account_id: str, now: str) -> None:
        rows = c.execute("SELECT id,approval_id FROM actions WHERE state IN "
                         "('review_pending','approved','queued') AND id IN ("
                         "SELECT action_id FROM outreach_action_bindings WHERE account_id=? "
                         "UNION SELECT action_id FROM calendar_booking_bindings WHERE account_id=?)",
                         (account_id, account_id)).fetchall()
        for row in rows:
            c.execute("UPDATE actions SET state='cancelled',updated_at_utc=? WHERE id=?",
                      (now, row["id"]))
            if row["approval_id"]:
                c.execute("UPDATE approvals SET decision='revoked',reason='account policy changed' "
                          "WHERE id=?", (row["approval_id"],))

    def suppress(self, *, subject_type: str, value: str, reason: str, actor: str,
                 source_event_id: str | None = None) -> str:
        if subject_type not in {"address", "domain", "organization", "account"}:
            raise ValueError("invalid suppression subject type")
        _required(reason, "reason", 500)
        _required(actor, "actor", 200)
        normalized = _email(value) if subject_type == "address" else _required(value, "value", 300).lower()
        digest, now, rule_id = self._digest(subject_type, normalized), self._now(), uuid4().hex
        with self.db.transaction() as c:
            c.execute("INSERT OR IGNORE INTO suppression_rules(id,subject_type,keyed_value,scope,"
                      "reason,source_event_id,created_at_utc) VALUES (?,?,?,'global',?,?,?)",
                      (rule_id, subject_type, digest, reason, source_event_id, now))
            row = c.execute("SELECT id FROM suppression_rules WHERE subject_type=? AND keyed_value=? "
                            "AND scope='global'", (subject_type, digest)).fetchone()
            # Hold anything still dispatchable; already-started provider effects require reconciliation.
            if subject_type == "address":
                c.execute("UPDATE actions SET state='cancelled',updated_at_utc=? WHERE id IN ("
                          "SELECT action_id FROM outreach_action_bindings WHERE recipient=? "
                          "UNION SELECT action_id FROM calendar_booking_bindings WHERE attendee=?) "
                          "AND state IN ('review_pending','approved','queued','reserved')",
                          (now, normalized, normalized))
            append_audit(c, actor=actor, event_type="outreach.suppressed",
                         entity_type="suppression_rule", entity_id=row["id"],
                         details_json=json.dumps({"subject_type": subject_type, "reason": reason}),
                         occurred_at_utc=now)
            c.execute("INSERT INTO compliance_events(id,event_type,subject_type,suppression_rule_id,"
                      "result,details_json,occurred_at_utc,actor) VALUES (?,'suppression_added',?,?,"
                      "'active',?,?,?)", (uuid4().hex, subject_type, row["id"],
                      json.dumps({"reason": reason}), now, actor))
        return row["id"]

    def _suppressed(self, c: sqlite3.Connection, *, recipient: str,
                    account_id: str, organization_id: str | None) -> bool:
        domain = recipient.split("@", 1)[1]
        checks = [("address", recipient), ("domain", domain), ("account", account_id)]
        if organization_id:
            checks.append(("organization", organization_id))
        now = self._now()
        return any(c.execute("SELECT 1 FROM suppression_rules WHERE subject_type=? AND "
                             "keyed_value=? AND (expires_at_utc IS NULL OR expires_at_utc>?)",
                             (kind, self._digest(kind, value), now)).fetchone()
                   for kind, value in checks)

    def ingest_page(self, *, account_id: str, messages: list[IncomingMail],
                    next_cursor: str | None) -> dict[str, int]:
        """Commit inbox rows and cursor together; a repeated provider page is idempotent."""
        self.pause.guard("inbox")
        now, counts = self._now(), {"new": 0, "duplicates": 0, "suppressed": 0}
        with self.db.transaction() as c:
            account = c.execute("SELECT * FROM outreach_accounts WHERE id=?", (account_id,)).fetchone()
            if not account or not account["enabled"]:
                raise InvalidStateError("inbox account is not enabled")
            for mail in messages:
                provider_id = _required(mail.provider_id, "provider message ID", 500)
                thread_id = _required(mail.thread_id, "provider thread ID", 500)
                sender, recipient = _email(mail.sender), _email(mail.recipient)
                if recipient != account["email_address"]:
                    raise InvalidStateError("inbound recipient does not match authorized account")
                if c.execute("SELECT 1 FROM messages WHERE channel='email' AND provider_message_id=?",
                             (provider_id,)).fetchone():
                    counts["duplicates"] += 1
                    continue
                received = utc_text(parse_utc(mail.received_at_utc))
                conversation = c.execute("SELECT id FROM conversations WHERE channel='email' "
                                         "AND provider_thread_id=?", (thread_id,)).fetchone()
                conversation_id = conversation["id"] if conversation else uuid4().hex
                if not conversation:
                    c.execute("INSERT INTO conversations(id,channel,provider_thread_id,intent_status,"
                              "last_activity_at_utc) VALUES (?,'email',?,'unclassified',?)",
                              (conversation_id, thread_id, received))
                else:
                    c.execute("UPDATE conversations SET last_activity_at_utc=? WHERE id=?",
                              (received, conversation_id))
                message_id, event_id = uuid4().hex, uuid4().hex
                c.execute("INSERT INTO messages(id,conversation_id,provider_message_id,direction,"
                          "channel,body_reference,delivery_status,occurred_at_utc) "
                          "VALUES (?,?,?,'inbound','email',?,'received',?)",
                          (message_id, conversation_id, provider_id, message_id, received))
                c.execute("INSERT INTO inbound_message_content(message_id,subject,body,sender,recipient,"
                          "stored_at_utc) VALUES (?,?,?,?,?,?)",
                          (message_id, mail.subject[:300], mail.body[:50000], sender, recipient, now))
                label, confidence, rationale = classify(mail)
                c.execute("INSERT INTO inbound_events(id,provider,provider_event_id,conversation_id,"
                          "event_type,classification,confidence,processing_status,received_at_utc) "
                          "VALUES (?,'gmail',?,?,'message',?,?,'classified',?)",
                          (event_id, provider_id, conversation_id, label, confidence, received))
                c.execute("INSERT INTO inbound_classifications(event_id,label,confidence,rationale,"
                          "classifier_version) VALUES (?,?,?,?, 'rules-v1')",
                          (event_id, label, confidence, rationale))
                c.execute("UPDATE conversations SET intent_status=? WHERE id=?", (label, conversation_id))
                if label in {"opt_out", "complaint"}:
                    self._suppress_locked(c, sender, event_id, now)
                    counts["suppressed"] += 1
                append_audit(c, actor="4B", event_type="inbox.message_classified",
                             entity_type="inbound_event", entity_id=event_id,
                             details_json=json.dumps({"label": label, "confidence": confidence}),
                             occurred_at_utc=now)
                destination = "support" if label == "support" else "analytics"
                c.execute("INSERT INTO outbox_events(id,event_type,schema_version,aggregate_type,"
                          "aggregate_id,destination,payload_json,deduplication_key,state,due_at_utc,"
                          "attempt_count,created_at_utc,updated_at_utc) "
                          "VALUES (?, 'inbox.classified',1,'inbound_event',?,?,?,?, 'pending',?,0,?,?)",
                          (uuid4().hex, event_id, destination,
                           json.dumps({"event_id": event_id, "label": label, "account_id": account_id}),
                           f"inbox:{account_id}:{provider_id}", now, now, now))
                counts["new"] += 1
            c.execute("INSERT INTO inbox_cursors(account_id,cursor,checked_at_utc) VALUES (?,?,?) "
                      "ON CONFLICT(account_id) DO UPDATE SET cursor=excluded.cursor,"
                      "checked_at_utc=excluded.checked_at_utc", (account_id, next_cursor, now))
        return counts

    def _suppress_locked(self, c: sqlite3.Connection, address: str,
                         event_id: str, now: str) -> None:
        digest = self._digest("address", address)
        c.execute("INSERT OR IGNORE INTO suppression_rules(id,subject_type,keyed_value,scope,"
                  "reason,source_event_id,created_at_utc) VALUES (?,'address',?,'global',"
                  "'inbound opt-out or complaint',?,?)", (uuid4().hex, digest, event_id, now))
        c.execute("UPDATE actions SET state='cancelled',updated_at_utc=? WHERE id IN ("
                  "SELECT action_id FROM outreach_action_bindings WHERE recipient=? "
                  "UNION SELECT action_id FROM calendar_booking_bindings WHERE attendee=?) "
                  "AND state IN ('review_pending','approved','queued','reserved')",
                  (now, address, address))

    def poll_once(self, account_id: str) -> dict[str, int]:
        if self.mail is None:
            raise InvalidStateError("Gmail authorization has not been integrated")
        with self.db.read() as c:
            row = c.execute("SELECT cursor FROM inbox_cursors WHERE account_id=?", (account_id,)).fetchone()
            account = c.execute("SELECT email_address,enabled FROM outreach_accounts WHERE id=?",
                                (account_id,)).fetchone()
        if not account or not account["enabled"]:
            raise InvalidStateError("inbox account is not enabled")
        self.pause.guard("inbox")
        page, cursor = self.mail.read_page(account["email_address"], row["cursor"] if row else None)
        return self.ingest_page(account_id=account_id, messages=page, next_cursor=cursor)

    def review_classification(self, *, event_id: str, label: str, actor: str,
                              rationale: str) -> None:
        allowed = {"warm", "meeting", "question", "objection", "support", "opt_out", "bounce",
                   "complaint", "out_of_office", "unrelated", "needs_review"}
        if label not in allowed:
            raise ValueError("invalid classification")
        _required(actor, "actor", 200)
        _required(rationale, "rationale", 1000)
        now = self._now()
        with self.db.transaction() as c:
            row = c.execute("SELECT i.conversation_id,m.sender FROM inbound_events i "
                            "JOIN messages msg ON msg.provider_message_id=i.provider_event_id "
                            "JOIN inbound_message_content m ON m.message_id=msg.id "
                            "WHERE i.id=? AND i.provider='gmail'", (event_id,)).fetchone()
            if not row:
                raise NotFoundError("inbound event not found")
            c.execute("UPDATE inbound_classifications SET label=?,confidence=1,rationale=?,"
                      "reviewed_by=?,reviewed_at_utc=? WHERE event_id=?",
                      (label, rationale, actor, now, event_id))
            c.execute("UPDATE inbound_events SET classification=?,confidence=1 WHERE id=?",
                      (label, event_id))
            c.execute("UPDATE conversations SET intent_status=? WHERE id=?",
                      (label, row["conversation_id"]))
            if label in {"opt_out", "complaint"}:
                self._suppress_locked(c, row["sender"], event_id, now)
            append_audit(c, actor=actor, event_type="inbox.classification_reviewed",
                         entity_type="inbound_event", entity_id=event_id,
                         details_json=json.dumps({"label": label, "rationale": rationale}),
                         occurred_at_utc=now)

    def propose_response(self, *, event_id: str, account_id: str, subject: str,
                         body: str, author: str, slots: list[dict] | None = None) -> str:
        _header(subject, "subject")
        _required(body, "body")
        _required(author, "author", 200)
        slots = slots or []
        if len(slots) > 20:
            raise ValueError("too many proposed slots")
        if slots and self.calendar is None:
            raise InvalidStateError("cannot propose unverified calendar availability")
        for slot in slots:
            start, end = parse_utc(slot["start_at_utc"]), parse_utc(slot["end_at_utc"])
            if end <= start or start <= self.clock():
                raise ValueError("proposed slot must be a future positive interval")
            with self.db.read() as c:
                calendar_row = c.execute("SELECT calendar_id FROM outreach_accounts WHERE id=?",
                                         (account_id,)).fetchone()
            if not calendar_row or not calendar_row["calendar_id"] or not self.calendar.available(
                    calendar_id=calendar_row["calendar_id"], start_at_utc=utc_text(start),
                    end_at_utc=utc_text(end)):
                raise InvalidStateError("calendar slot is not verified available")
        now, proposal_id, draft_id = self._now(), uuid4().hex, uuid4().hex
        with self.db.transaction() as c:
            row = c.execute("SELECT i.classification,msg.id AS message_id,msg.provider_message_id,"
                            "msg.conversation_id,m.sender,m.recipient FROM inbound_events i "
                            "JOIN messages msg ON msg.provider_message_id=i.provider_event_id "
                            "JOIN inbound_message_content m ON m.message_id=msg.id "
                            "WHERE i.id=? AND i.provider='gmail'", (event_id,)).fetchone()
            account = c.execute("SELECT * FROM outreach_accounts WHERE id=?", (account_id,)).fetchone()
            if not row or not account or row["recipient"] != account["email_address"]:
                raise InvalidStateError("event/account mismatch")
            if row["classification"] in {"opt_out", "bounce", "complaint", "out_of_office", "unrelated"}:
                raise InvalidStateError("event is not eligible for a reply")
            if self._suppressed(c, recipient=row["sender"], account_id=account_id,
                                organization_id=None):
                raise InvalidStateError("recipient is suppressed")
            digest = content_hash(subject, body)
            c.execute("INSERT INTO drafts(id,channel,subject,body,content_hash,version,evidence_json,"
                      "author_role,status,created_at_utc) VALUES (?,'email',?,?,?,1,?,'4C',"
                      "'review_pending',?)",
                      (draft_id, subject, body, digest, json.dumps({"inbound_event_id": event_id}), now))
            c.execute("INSERT INTO response_proposals(id,draft_id,event_id,reply_to_message_id,"
                      "account_id,recipient,subject,body,content_hash,proposed_slots_json,state,author,"
                      "created_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,'draft',?,?)",
                      (proposal_id, draft_id, event_id, row["message_id"], account_id,
                       row["sender"], subject, body, digest, json.dumps(slots), author, now))
            append_audit(c, actor=author, event_type="outreach.response_proposed",
                         entity_type="response_proposal", entity_id=proposal_id, occurred_at_utc=now)
        return proposal_id

    def review_response(self, *, proposal_id: str, reviewer: str) -> None:
        _required(reviewer, "reviewer", 200)
        now = self._now()
        with self.db.transaction() as c:
            proposal = c.execute("SELECT * FROM response_proposals WHERE id=?", (proposal_id,)).fetchone()
            if not proposal or proposal["state"] != "draft":
                raise InvalidStateError("only a pending proposal can be reviewed")
            c.execute("UPDATE response_proposals SET state='reviewed' WHERE id=?", (proposal_id,))
            c.execute("UPDATE drafts SET status='review_passed' WHERE id=?", (proposal["draft_id"],))
            c.execute("INSERT INTO content_reviews(id,draft_id,content_hash,input_hash,rule_version,"
                      "review_round,decision,reviewer_role,reviewed_at_utc) "
                      "VALUES (?,?,?,?, 'human-reply-review-v1',1,'passed',?,?)",
                      (uuid4().hex, proposal["draft_id"], proposal["content_hash"],
                       proposal["content_hash"], reviewer, now))
            append_audit(c, actor=reviewer, event_type="outreach.response_reviewed",
                         entity_type="response_proposal", entity_id=proposal_id, occurred_at_utc=now)

    def prepare_send(self, *, draft_id: str, account_id: str, recipient: str,
                     actor: str, due_at_utc: str | None = None,
                     source_message_id: str | None = None,
                     recipient_timezone: str | None = None,
                     timezone_source: str | None = None) -> str:
        recipient = _email(recipient)
        _required(actor, "actor", 200)
        now, action_id = self._now(), uuid4().hex
        due = utc_text(parse_utc(due_at_utc)) if due_at_utc else now
        with self.db.transaction() as c:
            draft = c.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
            account = c.execute("SELECT * FROM outreach_accounts WHERE id=?", (account_id,)).fetchone()
            if not draft or not account or draft["channel"] != "email" or draft["status"] != "review_passed":
                raise InvalidStateError("review-passed email draft and registered account required")
            _header(draft["subject"], "subject")
            review = c.execute("SELECT 1 FROM content_reviews WHERE draft_id=? AND content_hash=? "
                               "AND decision='passed' ORDER BY reviewed_at_utc DESC LIMIT 1",
                               (draft_id, draft["content_hash"])).fetchone()
            if not review or content_hash(draft["subject"], draft["body"]) != draft["content_hash"]:
                raise InvalidStateError("exact draft has no valid passing review")
            thread_id = None
            target_id = draft["contact_id"]
            if source_message_id:
                proposal = c.execute("SELECT p.*,m.provider_message_id,conv.provider_thread_id "
                                     "FROM response_proposals p JOIN messages m ON m.id=p.reply_to_message_id "
                                     "JOIN conversations conv ON conv.id=m.conversation_id "
                                     "WHERE p.draft_id=? AND p.state='reviewed'", (draft_id,)).fetchone()
                if not proposal or proposal["provider_message_id"] != source_message_id or \
                        proposal["recipient"] != recipient or proposal["account_id"] != account_id:
                    raise InvalidStateError("reply is not bound to reviewed inbound message")
                target_id, thread_id = proposal["event_id"], proposal["provider_thread_id"]
                kind = "email_reply"
                recipient_timezone = recipient_timezone or account["send_timezone"]
                timezone_source = timezone_source or "approved_fallback"
            else:
                contact = c.execute("SELECT ct.*,o.identity_status,o.id AS organization_id "
                                    "FROM contacts ct JOIN organizations o ON o.id=ct.organization_id "
                                    "WHERE ct.id=?", (target_id,)).fetchone()
                if not contact or contact["verification_status"] != "verified" or \
                        contact["identity_status"] != "verified" or \
                        contact["normalized_address"] != recipient or \
                        not draft["prospect_id"]:
                    raise InvalidStateError("verified identity and exact contact address required")
                if not c.execute("SELECT 1 FROM prospects WHERE id=? AND organization_id=? "
                                 "AND lifecycle_state IN ('qualified','drafted','review_pending',"
                                 "'approved','action_queued')", (draft["prospect_id"],
                                 contact["organization_id"])).fetchone():
                    raise InvalidStateError("prospect is not qualified for outreach")
                kind = "cold_email"
                if not recipient_timezone or timezone_source not in {"verified", "approved_fallback"}:
                    raise InvalidStateError("cold outreach requires recipient IANA timezone and source")
            try:
                _zone(recipient_timezone)
            except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
                raise ValueError("valid recipient IANA timezone required") from exc
            if timezone_source == "approved_fallback" and recipient_timezone != account["send_timezone"]:
                raise InvalidStateError("fallback timezone must match approved account fallback")
            if self._suppressed(c, recipient=recipient, account_id=account_id,
                                organization_id=contact["organization_id"] if not source_message_id else None):
                raise InvalidStateError("recipient is suppressed")
            key = _hash({"kind": kind, "draft": draft_id, "account": account_id,
                         "recipient": recipient, "source_message": source_message_id,
                         "recipient_timezone": recipient_timezone})
            existing = c.execute("SELECT id FROM actions WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                return existing["id"]
            c.execute("INSERT INTO actions(id,kind,target_id,channel,campaign_id,draft_id,due_at_utc,"
                      "state,idempotency_key,policy_version,created_at_utc,updated_at_utc) "
                      "VALUES (?,?,?,'email',?,?,?,'review_pending',?,?,?,?)",
                      (action_id, kind, target_id, draft["campaign_id"], draft_id, due,
                       key, account["policy_version"], now, now))
            c.execute("INSERT INTO outreach_action_bindings(action_id,account_id,recipient,subject,"
                      "body,content_hash,thread_id,source_message_id,created_at_utc,"
                      "recipient_timezone,timezone_source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (action_id, account_id, recipient, draft["subject"] or "", draft["body"],
                       draft["content_hash"], thread_id, source_message_id, now,
                       recipient_timezone, timezone_source))
            append_audit(c, actor=actor, event_type="outreach.send_prepared",
                         entity_type="action", entity_id=action_id, occurred_at_utc=now)
        return action_id

    def approve_action(self, *, action_id: str, actor: str, expires_at_utc: str,
                       reason: str) -> str:
        _required(actor, "actor", 200)
        _required(reason, "reason", 1000)
        expiry = utc_text(parse_utc(expires_at_utc))
        now, approval_id = self._now(), uuid4().hex
        if expiry <= now:
            raise ValueError("approval expiry must be in the future")
        with self.db.transaction() as c:
            action = c.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if not action or action["state"] != "review_pending":
                raise InvalidStateError("action must await approval")
            if action["channel"] == "email":
                binding = c.execute("SELECT content_hash FROM outreach_action_bindings WHERE action_id=?",
                                    (action_id,)).fetchone()
                if not binding:
                    raise InvalidStateError("email action has no exact binding")
                digest = binding["content_hash"]
                if not c.execute("SELECT 1 FROM content_reviews WHERE draft_id=? "
                                 "AND content_hash=? AND reviewer_role='7A' AND decision='passed'",
                                 (action["draft_id"], digest)).fetchone():
                    raise InvalidStateError("Department 7A guard pass required before email approval")
            elif action["channel"] == "calendar":
                draft = c.execute("SELECT content_hash FROM drafts WHERE id=?", (action["draft_id"],)).fetchone()
                if not draft:
                    raise InvalidStateError("booking has no exact draft")
                digest = draft["content_hash"]
            else:
                raise InvalidStateError("unsupported action channel")
            c.execute("INSERT INTO approvals(id,draft_id,content_hash,target_id,channel,action_type,actor,"
                      "policy_version,decision,decided_at_utc,expires_at_utc,reason) "
                      "VALUES (?,?,?,?,?,?,?,?,'approved',?,?,?)",
                      (approval_id, action["draft_id"], digest, action["target_id"],
                       action["channel"], action["kind"], actor, action["policy_version"],
                       now, expiry, reason))
            c.execute("UPDATE actions SET approval_id=?,state='queued',updated_at_utc=? WHERE id=?",
                      (approval_id, now, action_id))
            append_audit(c, actor=actor, event_type="outreach.action_approved",
                         entity_type="action", entity_id=action_id,
                         details_json=json.dumps({"approval_id": approval_id}), occurred_at_utc=now)
        return approval_id

    def revoke_action(self, *, action_id: str, actor: str, reason: str) -> None:
        _required(actor, "actor", 200)
        _required(reason, "reason", 1000)
        now = self._now()
        with self.db.transaction() as c:
            row = c.execute("SELECT approval_id,state FROM actions WHERE id=?", (action_id,)).fetchone()
            if not row or row["state"] not in {"review_pending", "queued", "approved"}:
                raise InvalidStateError("only an unsent action can be cancelled")
            c.execute("UPDATE actions SET state='cancelled',updated_at_utc=? WHERE id=?", (now, action_id))
            if row["approval_id"]:
                c.execute("UPDATE approvals SET decision='revoked',reason=? WHERE id=?",
                          (reason, row["approval_id"]))
            append_audit(c, actor=actor, event_type="outreach.action_cancelled",
                         entity_type="action", entity_id=action_id,
                         details_json=json.dumps({"reason": reason}), occurred_at_utc=now)

    def _gate_email(self, c: sqlite3.Connection, action: sqlite3.Row,
                    binding: sqlite3.Row, now: str) -> tuple[str, str]:
        account = c.execute("SELECT * FROM outreach_accounts WHERE id=?",
                            (binding["account_id"],)).fetchone()
        approval = c.execute("SELECT * FROM approvals WHERE id=?", (action["approval_id"],)).fetchone()
        draft = c.execute("SELECT * FROM drafts WHERE id=?", (action["draft_id"],)).fetchone()
        if not account or not account["enabled"] or not approval or not draft:
            raise InvalidStateError("account, approval or draft missing/disabled")
        if account["policy_version"] != action["policy_version"] or \
                approval["policy_version"] != action["policy_version"]:
            raise InvalidStateError("policy version changed after approval")
        if not all(account[field] for field in ("send_timezone", "send_start_local",
                                                "send_end_local", "send_weekdays_json")):
            raise InvalidStateError("approved send window is missing")
        if not binding["recipient_timezone"] or binding["timezone_source"] not in {
                "verified", "approved_fallback"}:
            raise InvalidStateError("recipient timezone evidence missing")
        if binding["timezone_source"] == "approved_fallback" and \
                binding["recipient_timezone"] != account["send_timezone"]:
            raise InvalidStateError("approved fallback timezone changed")
        local = self.clock().astimezone(_zone(binding["recipient_timezone"]))
        local_hhmm = local.strftime("%H:%M")
        if local.weekday() not in json.loads(account["send_weekdays_json"]) or not (
                account["send_start_local"] <= local_hhmm < account["send_end_local"]):
            raise InvalidStateError("outside approved local send window")
        if approval["decision"] != "approved" or approval["expires_at_utc"] <= now or \
                approval["draft_id"] != draft["id"] or \
                approval["content_hash"] != binding["content_hash"] or \
                approval["target_id"] != action["target_id"] or \
                approval["channel"] != "email" or approval["action_type"] != action["kind"]:
            raise InvalidStateError("approval does not match exact action")
        if draft["status"] != "review_passed" or \
                content_hash(binding["subject"] or None, binding["body"]) != binding["content_hash"] or \
                draft["content_hash"] != binding["content_hash"]:
            raise InvalidStateError("draft/binding changed or review invalid")
        if not c.execute("SELECT 1 FROM content_reviews WHERE draft_id=? AND content_hash=? "
                         "AND decision='passed'", (draft["id"], draft["content_hash"])).fetchone():
            raise InvalidStateError("no passing content review")
        if not c.execute("SELECT 1 FROM content_reviews WHERE draft_id=? AND content_hash=? "
                         "AND reviewer_role='7A' AND decision='passed'",
                         (draft["id"], draft["content_hash"])).fetchone():
            raise InvalidStateError("Department 7A guard pass missing or invalid")
        organization_id = None
        if action["kind"] == "cold_email":
            contact = c.execute("SELECT ct.*,o.identity_status,o.id AS organization_id "
                                "FROM contacts ct JOIN organizations o ON o.id=ct.organization_id "
                                "WHERE ct.id=?", (action["target_id"],)).fetchone()
            if not contact or contact["verification_status"] != "verified" or \
                    contact["identity_status"] != "verified" or \
                    contact["normalized_address"] != binding["recipient"]:
                raise InvalidStateError("contact identity or email no longer verified")
            organization_id = contact["organization_id"]
        elif action["kind"] == "email_reply":
            event = c.execute("SELECT i.classification,m.sender FROM inbound_events i "
                              "JOIN response_proposals p ON p.event_id=i.id "
                              "JOIN inbound_message_content m ON m.message_id=p.reply_to_message_id "
                              "WHERE p.draft_id=?", (draft["id"],)).fetchone()
            if not event or event["sender"] != binding["recipient"] or \
                    event["classification"] in {"opt_out", "complaint", "bounce", "out_of_office", "unrelated"}:
                raise InvalidStateError("reply context changed or is no-contact")
        else:
            raise InvalidStateError("unsupported email action")
        if self._suppressed(c, recipient=binding["recipient"], account_id=account["id"],
                            organization_id=organization_id):
            raise InvalidStateError("recipient is suppressed")
        policy = c.execute("SELECT * FROM quota_policies WHERE account_id=? AND channel='email' "
                           "AND active=1", (account["id"],)).fetchone()
        if not policy:
            raise InvalidStateError("no active approved email quota")
        if policy["policy_version"] != action["policy_version"]:
            raise InvalidStateError("quota policy version mismatch")
        health = c.execute("SELECT * FROM provider_health_snapshots WHERE channel='email' "
                           "AND account_id=? ORDER BY observed_at_utc DESC,rowid DESC LIMIT 1",
                           (account["id"],)).fetchone()
        effective_limit = policy["max_units"]
        if health:
            health_status = health["status"]
            if health["expires_at_utc"] and health["expires_at_utc"] <= now:
                health_status = "unknown"
            if health_status in {"unhealthy", "unknown"}:
                raise InvalidStateError("provider health does not permit dispatch")
            if health_status == "degraded":
                effective_limit = max(1, policy["max_units"] // 2)
        period = int(self.clock().timestamp()) // policy["period_seconds"] * policy["period_seconds"]
        period_start = utc_text(datetime.fromtimestamp(period, tz=timezone.utc))
        used = c.execute("SELECT COALESCE(SUM(units),0) FROM quota_ledger WHERE policy_id=? "
                         "AND period_start_utc=? AND state IN ('reserved','committed','unknown')",
                         (policy["id"], period_start)).fetchone()[0]
        if used >= effective_limit:
            raise InvalidStateError("email quota exhausted")
        return policy["id"], period_start

    def dispatch_once(self) -> dict | None:
        """One due email/booking; never retry an unknown effect automatically."""
        if not self.external_actions_enabled:
            raise InvalidStateError("external actions are disabled in this environment")
        self.pause.guard("outreach")
        now = self._now()
        with self.db.transaction() as c:
            action = c.execute("SELECT * FROM actions WHERE state='queued' AND due_at_utc<=? "
                               "AND channel IN ('email','calendar') ORDER BY due_at_utc,id LIMIT 1",
                               (now,)).fetchone()
            if not action:
                return None
            if self.pause.pause_file.is_file() or c.execute("SELECT 1 FROM system_controls WHERE paused=1 "
                                                             "AND scope IN ('global','lane:outreach')").fetchone():
                raise PausedError("outreach paused")
            attempt_id = uuid4().hex
            if action["channel"] == "email":
                if self.mail is None:
                    raise InvalidStateError("Gmail authorization has not been integrated")
                binding = c.execute("SELECT * FROM outreach_action_bindings WHERE action_id=?",
                                    (action["id"],)).fetchone()
                if not binding:
                    raise InvalidStateError("email binding missing")
                policy_id, period_start = self._gate_email(c, action, binding, now)
                c.execute("INSERT INTO quota_ledger(id,policy_id,action_id,period_start_utc,units,state,"
                          "created_at_utc) VALUES (?,?,?,?,1,'reserved',?)",
                          (uuid4().hex, policy_id, action["id"], period_start, now))
                account = c.execute("SELECT email_address FROM outreach_accounts WHERE id=?",
                                    (binding["account_id"],)).fetchone()[0]
                payload = dict(account=account, recipient=binding["recipient"],
                               subject=binding["subject"], body=binding["body"],
                               thread_id=binding["thread_id"], action_key=action["idempotency_key"])
            else:
                if self.calendar is None:
                    raise InvalidStateError("Calendar authorization has not been integrated")
                payload = self._gate_booking(c, action, now)
            c.execute("UPDATE actions SET state='dispatching',updated_at_utc=? WHERE id=?",
                      (now, action["id"]))
            c.execute("INSERT INTO delivery_attempts(id,action_id,provider_request_id,started_at_utc,"
                      "status) VALUES (?,?,?,?,'started')",
                      (attempt_id, action["id"], action["idempotency_key"], now))
            append_audit(c, actor="4A" if action["channel"] == "email" else "4C",
                         event_type="outreach.dispatch_started", entity_type="action",
                         entity_id=action["id"], occurred_at_utc=now)
        # Final local pause/suppression check just before the external call. An in-flight
        # provider request cannot be recalled; ambiguous effects stay unknown.
        preflight_error = None
        try:
            self.pause.guard("outreach")
            if action["channel"] == "email":
                with self.db.read() as c:
                    if self._suppressed(c, recipient=payload["recipient"],
                                        account_id=binding["account_id"],
                                        organization_id=None):
                        raise InvalidStateError("recipient suppressed before dispatch")
            else:
                if not self.calendar.available(calendar_id=payload["calendar_id"],
                                               start_at_utc=payload["start_at_utc"],
                                               end_at_utc=payload["end_at_utc"]):
                    raise InvalidStateError("calendar slot is no longer available")
        except (PausedError, InvalidStateError) as exc:
            preflight_error = str(exc)
        except Exception:
            preflight_error = "pre-provider check failed"
        if preflight_error is not None:
            result = ProviderResult("rejected", diagnostic=preflight_error)
        else:
            try:
                result = self.mail.send(**payload) if action["channel"] == "email" else \
                         self.calendar.book(**payload)
            except Exception:
                # Once a provider call begins, even a local-looking exception may follow
                # remote acceptance. Never infer that resending is safe.
                result = ProviderResult("unknown", diagnostic="provider outcome unknown")
        if result.status not in {"accepted", "rejected", "unknown"}:
            result = ProviderResult("unknown", diagnostic="invalid provider result")
        if result.status == "accepted" and not result.provider_id:
            result = ProviderResult("unknown", diagnostic="provider accepted without stable ID")
        final_state = {"accepted": "accepted", "rejected": "rejected", "unknown": "unknown"}[result.status]
        finished = self._now()
        with self.db.transaction() as c:
            c.execute("UPDATE actions SET state=?,updated_at_utc=? WHERE id=? "
                      "AND state IN ('dispatching','unknown')",
                      (final_state, finished, action["id"]))
            c.execute("UPDATE delivery_attempts SET status=?,provider_response_id=?,"
                      "finished_at_utc=?,redacted_diagnostic=? WHERE id=?",
                      (result.status, result.provider_id, finished,
                       (result.diagnostic or "")[:300], attempt_id))
            if action["channel"] == "email":
                ledger_state = {"accepted": "committed", "rejected": "released", "unknown": "unknown"}[result.status]
                c.execute("UPDATE quota_ledger SET state=? WHERE action_id=?",
                          (ledger_state, action["id"]))
            if result.status == "accepted":
                conversation_id = None
                if action["channel"] == "email" and payload["thread_id"]:
                    conversation = c.execute("SELECT id FROM conversations WHERE channel='email' "
                                             "AND provider_thread_id=?", (payload["thread_id"],)).fetchone()
                    conversation_id = conversation["id"] if conversation else None
                c.execute("INSERT INTO messages(id,conversation_id,action_id,provider_message_id,"
                          "direction,channel,delivery_status,occurred_at_utc) "
                          "VALUES (?,?,?,?,'outbound',?,'accepted',?)",
                          (uuid4().hex, conversation_id, action["id"], result.provider_id,
                           action["channel"], finished))
            append_audit(c, actor="4A" if action["channel"] == "email" else "4C",
                         event_type="outreach.dispatch_" + result.status,
                         entity_type="action", entity_id=action["id"], occurred_at_utc=finished)
        return {"action_id": action["id"], "state": final_state,
                "provider_id": result.provider_id}

    def recover_stale_dispatches(self, *, older_than_seconds: int = 600) -> int:
        """Move abandoned in-flight actions to unknown; never replay them."""
        if older_than_seconds < 60:
            raise ValueError("recovery threshold must be at least 60 seconds")
        cutoff = utc_text(self.clock() - timedelta(seconds=older_than_seconds))
        now = self._now()
        with self.db.transaction() as c:
            rows = c.execute("SELECT id FROM actions WHERE state='dispatching' "
                             "AND updated_at_utc<=?", (cutoff,)).fetchall()
            for row in rows:
                c.execute("UPDATE actions SET state='unknown',updated_at_utc=? WHERE id=?",
                          (now, row["id"]))
                c.execute("UPDATE delivery_attempts SET status='unknown',finished_at_utc=? "
                          "WHERE action_id=? AND status='started'", (now, row["id"]))
                c.execute("UPDATE quota_ledger SET state='unknown' WHERE action_id=? "
                          "AND state='reserved'", (row["id"],))
                append_audit(c, actor="4A", event_type="outreach.dispatch_recovered_unknown",
                             entity_type="action", entity_id=row["id"], occurred_at_utc=now)
        return len(rows)

    def _gate_booking(self, c: sqlite3.Connection, action: sqlite3.Row, now: str) -> dict:
        binding = c.execute("SELECT * FROM calendar_booking_bindings WHERE action_id=?",
                            (action["id"],)).fetchone()
        approval = c.execute("SELECT * FROM approvals WHERE id=?", (action["approval_id"],)).fetchone()
        draft = c.execute("SELECT * FROM drafts WHERE id=?", (action["draft_id"],)).fetchone()
        account = c.execute("SELECT * FROM outreach_accounts WHERE id=?",
                            (binding["account_id"],)).fetchone() if binding else None
        if not binding or not approval or not draft or not account or not account["enabled"]:
            raise InvalidStateError("booking binding, approval, draft or account missing")
        if account["policy_version"] != action["policy_version"] or \
                approval["policy_version"] != action["policy_version"] or \
                account["calendar_id"] != binding["calendar_id"]:
            raise InvalidStateError("booking policy or calendar changed")
        if approval["decision"] != "approved" or approval["expires_at_utc"] <= now or \
                approval["draft_id"] != draft["id"] or approval["content_hash"] != draft["content_hash"] or \
                approval["target_id"] != action["target_id"] or approval["channel"] != "calendar" or \
                approval["action_type"] != "calendar_booking" or \
                content_hash(draft["subject"], draft["body"]) != draft["content_hash"]:
            raise InvalidStateError("booking approval does not match exact slot/action")
        if parse_utc(binding["start_at_utc"]) <= self.clock() or \
                parse_utc(binding["end_at_utc"]) <= parse_utc(binding["start_at_utc"]):
            raise InvalidStateError("booking slot expired or invalid")
        if self._suppressed(c, recipient=binding["attendee"], account_id=account["id"],
                            organization_id=None):
            raise InvalidStateError("attendee is suppressed")
        return dict(calendar_id=binding["calendar_id"], attendee=binding["attendee"],
                    title=binding["title"], start_at_utc=binding["start_at_utc"],
                    end_at_utc=binding["end_at_utc"], action_key=action["idempotency_key"])

    def prepare_booking(self, *, account_id: str, attendee: str, title: str,
                        start_at_utc: str, end_at_utc: str, actor: str,
                        source_event_id: str | None = None) -> str:
        attendee = _email(attendee)
        _header(title, "title")
        _required(actor, "actor", 200)
        start, end = parse_utc(start_at_utc), parse_utc(end_at_utc)
        if start <= self.clock() or end <= start or end - start > timedelta(hours=4):
            raise ValueError("booking must be a future interval of at most four hours")
        now, action_id, draft_id = self._now(), uuid4().hex, uuid4().hex
        start_text, end_text = utc_text(start), utc_text(end)
        body = f"Meeting with {attendee} from {start_text} to {end_text}"
        digest = content_hash(title, body)
        with self.db.transaction() as c:
            account = c.execute("SELECT * FROM outreach_accounts WHERE id=?", (account_id,)).fetchone()
            if not account or not account["calendar_id"]:
                raise InvalidStateError("account has no approved calendar")
            key = _hash({"calendar": account["calendar_id"], "attendee": attendee,
                         "start": start_text, "end": end_text, "source_event": source_event_id})
            existing = c.execute("SELECT id FROM actions WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                return existing["id"]
            if source_event_id:
                source = c.execute("SELECT m.sender,i.classification FROM inbound_events i "
                                   "JOIN messages msg ON msg.provider_message_id=i.provider_event_id "
                                   "JOIN inbound_message_content m ON m.message_id=msg.id "
                                   "WHERE i.id=?", (source_event_id,)).fetchone()
                if not source or source["sender"] != attendee or source["classification"] != "meeting":
                    raise InvalidStateError("booking is not tied to a matching meeting inquiry")
                if c.execute("SELECT 1 FROM calendar_booking_bindings b JOIN actions a "
                             "ON a.id=b.action_id WHERE b.source_event_id=? AND "
                             "a.state IN ('review_pending','approved','queued','reserved',"
                             "'dispatching','accepted','unknown')", (source_event_id,)).fetchone():
                    raise InvalidStateError("inquiry already has an active or uncertain booking")
            if self._suppressed(c, recipient=attendee, account_id=account_id,
                                organization_id=None):
                raise InvalidStateError("attendee is suppressed")
            c.execute("INSERT INTO drafts(id,channel,subject,body,content_hash,version,evidence_json,"
                      "author_role,status,created_at_utc) VALUES (?,'calendar',?,?,?,1,?,'4C',"
                      "'review_passed',?)",
                      (draft_id, title, body, digest,
                       json.dumps({"source_event_id": source_event_id}), now))
            c.execute("INSERT INTO actions(id,kind,target_id,channel,draft_id,due_at_utc,state,"
                      "idempotency_key,policy_version,created_at_utc,updated_at_utc) "
                      "VALUES (?,'calendar_booking',?,'calendar',?,?,'review_pending',?,?,?,?)",
                      (action_id, source_event_id or attendee, draft_id, now, key,
                       account["policy_version"], now, now))
            c.execute("INSERT INTO calendar_booking_bindings(action_id,account_id,calendar_id,"
                      "attendee,title,start_at_utc,end_at_utc,source_event_id,created_at_utc) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (action_id, account_id, account["calendar_id"], attendee, title,
                       start_text, end_text, source_event_id, now))
            append_audit(c, actor=actor, event_type="outreach.booking_prepared",
                         entity_type="action", entity_id=action_id, occurred_at_utc=now)
        return action_id

    def reconcile_unknown(self, *, action_id: str, actor: str) -> dict:
        """Provider lookup is read-only; never re-dispatches an uncertain action."""
        _required(actor, "actor", 200)
        with self.db.read() as c:
            action = c.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if not action or action["state"] != "unknown":
                raise InvalidStateError("only unknown actions can be reconciled")
            attempt = c.execute("SELECT * FROM delivery_attempts WHERE action_id=? "
                                "ORDER BY started_at_utc DESC LIMIT 1", (action_id,)).fetchone()
            if not attempt:
                raise InvalidStateError("unknown action has no attempt")
            if action["channel"] == "email":
                binding = c.execute("SELECT b.account_id,a.email_address FROM outreach_action_bindings b "
                                    "JOIN outreach_accounts a ON a.id=b.account_id WHERE b.action_id=?",
                                    (action_id,)).fetchone()
                gateway = self.mail
                kwargs = {"account": binding["email_address"]} if binding else {}
            else:
                binding = c.execute("SELECT calendar_id FROM calendar_booking_bindings WHERE action_id=?",
                                    (action_id,)).fetchone()
                gateway = self.calendar
                kwargs = {"calendar_id": binding["calendar_id"]} if binding else {}
            if gateway is None or not binding:
                raise InvalidStateError("provider reconciliation gateway unavailable")
        result = gateway.reconcile(**kwargs, action_key=action["idempotency_key"],
                                   provider_request_id=attempt["provider_request_id"])
        if result.status not in {"accepted", "rejected", "unknown"}:
            raise InvalidStateError("invalid reconciliation result")
        if result.status == "accepted" and not result.provider_id:
            raise InvalidStateError("reconciliation lacks a stable provider ID")
        now = self._now()
        with self.db.transaction() as c:
            if result.status != "unknown":
                c.execute("UPDATE actions SET state=?,updated_at_utc=? WHERE id=? AND state='unknown'",
                          (result.status, now, action_id))
                if action["channel"] == "email":
                    c.execute("UPDATE quota_ledger SET state=? WHERE action_id=? AND state='unknown'",
                              ("committed" if result.status == "accepted" else "released", action_id))
                c.execute("UPDATE delivery_attempts SET status=?,provider_response_id=?,"
                          "finished_at_utc=? WHERE id=?",
                          (result.status, result.provider_id, now, attempt["id"]))
                if result.status == "accepted" and not c.execute(
                        "SELECT 1 FROM messages WHERE action_id=? AND direction='outbound'",
                        (action_id,)).fetchone():
                    c.execute("INSERT INTO messages(id,action_id,provider_message_id,direction,"
                              "channel,delivery_status,occurred_at_utc) VALUES (?,?,?,'outbound',?,"
                              "'accepted',?)", (uuid4().hex, action_id, result.provider_id,
                                                action["channel"], now))
            append_audit(c, actor=actor, event_type="outreach.reconciled_" + result.status,
                         entity_type="action", entity_id=action_id, occurred_at_utc=now)
        return {"action_id": action_id, "state": result.status,
                "provider_id": result.provider_id}

    def report(self, limit: int = 30) -> dict:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        with self.db.read() as c:
            return {
                "accounts": [dict(r) for r in c.execute("SELECT id,email_address,provider,enabled,"
                                                        "calendar_id,policy_version FROM outreach_accounts")],
                "actions": [dict(r) for r in c.execute("SELECT a.id,a.kind,a.channel,a.state,a.due_at_utc,"
                                                       "a.created_at_utc FROM actions a WHERE a.channel IN "
                                                       "('email','calendar') ORDER BY a.created_at_utc DESC LIMIT ?",
                                                       (limit,))],
                "inbound": [dict(r) for r in c.execute("SELECT i.id,i.classification,i.confidence,"
                                                       "i.processing_status,i.received_at_utc FROM inbound_events i "
                                                       "WHERE i.provider='gmail' ORDER BY i.received_at_utc DESC LIMIT ?",
                                                       (limit,))],
                "unknown_actions": c.execute("SELECT COUNT(*) FROM actions WHERE state='unknown' "
                                             "AND channel IN ('email','calendar')").fetchone()[0],
            }

    def action_detail(self, action_id: str) -> dict:
        with self.db.read() as c:
            action = c.execute("SELECT * FROM actions WHERE id=? AND channel IN ('email','calendar')",
                               (action_id,)).fetchone()
            if not action:
                raise NotFoundError("outreach action not found")
            if action["channel"] == "email":
                binding = c.execute("SELECT b.recipient,b.subject,b.body,b.content_hash,b.thread_id,"
                                    "b.recipient_timezone,b.timezone_source,a.email_address AS sender "
                                    "FROM outreach_action_bindings b "
                                    "JOIN outreach_accounts a ON a.id=b.account_id WHERE b.action_id=?",
                                    (action_id,)).fetchone()
            else:
                binding = c.execute("SELECT attendee,title,start_at_utc,end_at_utc,calendar_id "
                                    "FROM calendar_booking_bindings WHERE action_id=?", (action_id,)).fetchone()
            approval = c.execute("SELECT id,decision,actor,decided_at_utc,expires_at_utc "
                                 "FROM approvals WHERE id=?", (action["approval_id"],)).fetchone() \
                                 if action["approval_id"] else None
            attempts = [dict(r) for r in c.execute("SELECT id,started_at_utc,finished_at_utc,"
                                                   "status,provider_response_id,redacted_diagnostic "
                                                   "FROM delivery_attempts WHERE action_id=? "
                                                   "ORDER BY started_at_utc", (action_id,))]
        return {"action": dict(action), "binding": dict(binding) if binding else None,
                "approval": dict(approval) if approval else None, "attempts": attempts}

    def inbound_detail(self, event_id: str) -> dict:
        with self.db.read() as c:
            row = c.execute("SELECT i.id,i.classification,i.confidence,i.processing_status,"
                            "i.received_at_utc,m.subject,m.body,m.sender,m.recipient,"
                            "msg.provider_message_id,conv.provider_thread_id,cl.rationale,"
                            "cl.reviewed_by,cl.reviewed_at_utc FROM inbound_events i "
                            "JOIN messages msg ON msg.provider_message_id=i.provider_event_id "
                            "JOIN inbound_message_content m ON m.message_id=msg.id "
                            "JOIN conversations conv ON conv.id=msg.conversation_id "
                            "JOIN inbound_classifications cl ON cl.event_id=i.id "
                            "WHERE i.id=? AND i.provider='gmail'", (event_id,)).fetchone()
        if not row:
            raise NotFoundError("inbound event not found")
        return dict(row)
