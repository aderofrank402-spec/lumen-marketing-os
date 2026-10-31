from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from lumen_marketing_os.core.errors import InvalidStateError, PausedError
from lumen_marketing_os.core.time import utc_text
from lumen_marketing_os.workflows.content.model import content_hash
from lumen_marketing_os.workflows.outreach.runtime import IncomingMail, OutreachRuntime, ProviderResult
from lumen_marketing_os.workflows.audit.runtime import AuditRuntime


class FakeMail:
    def __init__(self, result: ProviderResult = ProviderResult("accepted", "provider-1")):
        self.result = result
        self.sent = []
        self.page = []

    def read_page(self, account, cursor):
        return self.page, "cursor-2"

    def send(self, **kwargs):
        self.sent.append(kwargs)
        if self.result == ProviderResult("accepted", "provider-1"):
            return ProviderResult("accepted", f"provider-{len(self.sent)}")
        return self.result

    def reconcile(self, **kwargs):
        return self.result


class FakeCalendar:
    def __init__(self):
        self.available_now = True
        self.booked = []

    def available(self, **kwargs):
        return self.available_now

    def book(self, **kwargs):
        self.booked.append(kwargs)
        return ProviderResult("accepted", "calendar-1")

    def reconcile(self, **kwargs):
        return ProviderResult("accepted", "calendar-1")


def runtime(foundation, *, live=False, mail=None, calendar=None):
    clock, db, pause, _, _ = foundation
    return OutreachRuntime(database=db, pause=pause, suppression_key=b"x" * 32,
                           mail=mail, calendar=calendar,
                           external_actions_enabled=live, clock=clock)


def account(foundation, rt):
    aid = rt.register_account(email="lumen@example.com", policy_version="p1",
                              calendar_id="primary", actor="operator")
    rt.enable_account(account_id=aid, actor="operator", confirmed=True)
    rt.set_quota(account_id=aid, max_units=2, period_seconds=86400,
                 policy_version="p1", actor="operator")
    rt.set_send_window(account_id=aid, timezone_name="UTC", start_local="00:00",
                       end_local="23:59", weekdays=list(range(7)), actor="operator")
    return aid


def reviewed_cold_draft(foundation):
    clock, db, _, _, _ = foundation
    now = utc_text(clock())
    digest = content_hash("Hello", "A sourced note")
    with db.transaction() as c:
        c.execute("INSERT INTO organizations(id,display_name,normalized_name,identity_status,"
                  "created_at_utc,updated_at_utc) VALUES ('org','Org','org','verified',?,?)", (now, now))
        c.execute("INSERT INTO contacts(id,organization_id,channel,address,normalized_address,"
                  "verification_status,verified_at_utc,created_at_utc,updated_at_utc) "
                  "VALUES ('contact','org','email','buyer@example.org','buyer@example.org',"
                  "'verified',?,?,?)", (now, now, now))
        c.execute("INSERT INTO prospects(id,organization_id,segment,lifecycle_state,created_at_utc,"
                  "updated_at_utc) VALUES ('prospect','org','test','qualified',?,?)", (now, now))
        c.execute("INSERT INTO drafts(id,prospect_id,contact_id,channel,subject,body,content_hash,"
                  "version,evidence_json,author_role,status,created_at_utc) VALUES "
                  "('draft','prospect','contact','email','Hello','A sourced note',?,1,'{}','3A',"
                  "'review_passed',?)", (digest, now))
        c.execute("INSERT INTO content_reviews(id,draft_id,content_hash,input_hash,rule_version,"
                  "review_round,decision,reviewer_role,reviewed_at_utc) VALUES "
                  "('review','draft',?,?,'test',1,'passed','3D',?)", (digest, digest, now))
    return "draft"


def approve(foundation, rt, action_id, *, hours=1):
    clock = foundation[0]
    with foundation[1].transaction() as c:
        action = c.execute("SELECT channel,draft_id FROM actions WHERE id=?", (action_id,)).fetchone()
        if action["channel"] == "email":
            draft = c.execute("SELECT content_hash,input_hash FROM drafts WHERE id=?",
                              (action["draft_id"],)).fetchone()
            review_round = c.execute("SELECT COALESCE(MAX(review_round),0)+1 FROM content_reviews "
                                     "WHERE draft_id=?", (action["draft_id"],)).fetchone()[0]
            c.execute("INSERT INTO content_reviews(id,draft_id,content_hash,input_hash,rule_version,"
                      "review_round,decision,reviewer_role,reviewed_at_utc) "
                      "VALUES (?,?,?,?,?,?,'passed','7A',?)",
                      (uuid4().hex, action["draft_id"], draft["content_hash"],
                       draft["input_hash"] or draft["content_hash"], "test-guard-v1",
                       review_round, utc_text(clock())))
    return rt.approve_action(action_id=action_id, actor="operator", reason="exact action reviewed",
                             expires_at_utc=utc_text(clock() + timedelta(hours=hours)))


def test_email_gate_approval_quota_idempotency_and_no_implicit_send(foundation):
    mail = FakeMail()
    rt = runtime(foundation, live=True, mail=mail)
    aid = account(foundation, rt)
    draft = reviewed_cold_draft(foundation)
    action = rt.prepare_send(draft_id=draft, account_id=aid, recipient="buyer@example.org",
                             actor="operator", recipient_timezone="UTC", timezone_source="verified")
    assert rt.prepare_send(draft_id=draft, account_id=aid, recipient="buyer@example.org",
                           actor="operator", recipient_timezone="UTC",
                           timezone_source="verified") == action
    assert mail.sent == []
    assert rt.dispatch_once() is None  # unapproved is not eligible
    with pytest.raises(InvalidStateError, match="7A guard"):
        rt.approve_action(action_id=action, actor="operator", reason="premature",
                          expires_at_utc=utc_text(foundation[0]() + timedelta(hours=1)))
    approve(foundation, rt, action)
    assert rt.dispatch_once()["state"] == "accepted"
    assert len(mail.sent) == 1
    assert rt.dispatch_once() is None
    with foundation[1].read() as c:
        assert c.execute("SELECT state FROM quota_ledger WHERE action_id=?", (action,)).fetchone()[0] == "committed"


def test_7b_provider_health_blocks_then_allows_exact_approved_action(foundation):
    mail = FakeMail()
    rt = runtime(foundation, live=True, mail=mail)
    aid = account(foundation, rt)
    draft_id = reviewed_cold_draft(foundation)
    action = rt.prepare_send(draft_id=draft_id, account_id=aid, recipient="buyer@example.org",
                             actor="operator", recipient_timezone="UTC", timezone_source="verified")
    approve(foundation, rt, action)
    audit = AuditRuntime(foundation[1], foundation[2], suppression_key=b"x" * 32,
                         clock=foundation[0])
    audit.record_provider_health(channel="email", account_id=aid, provider="gmail",
                                 status="unhealthy", reason="controlled fixture", actor="7B")
    with pytest.raises(InvalidStateError, match="provider health"):
        rt.dispatch_once()
    assert mail.sent == []
    audit.record_provider_health(channel="email", account_id=aid, provider="gmail",
                                 status="healthy", reason="controlled recovery", actor="7B")
    assert rt.dispatch_once()["state"] == "accepted"
    assert len(mail.sent) == 1


def test_suppression_pause_and_disabled_live_gate(foundation):
    mail = FakeMail()
    rt = runtime(foundation, live=True, mail=mail)
    aid = account(foundation, rt)
    draft = reviewed_cold_draft(foundation)
    action = rt.prepare_send(draft_id=draft, account_id=aid, recipient="buyer@example.org",
                             actor="operator", recipient_timezone="UTC", timezone_source="verified")
    approve(foundation, rt, action)
    meeting = rt.prepare_booking(account_id=aid, attendee="buyer@example.org",
                                 title="Demo", actor="operator",
                                 start_at_utc=utc_text(foundation[0]() + timedelta(hours=2)),
                                 end_at_utc=utc_text(foundation[0]() + timedelta(hours=3)))
    foundation[2].pause(actor="operator", reason="test", lane="outreach")
    with pytest.raises(PausedError):
        rt.dispatch_once()
    foundation[2].resume(actor="operator", reason="test", lane="outreach")
    rt.suppress(subject_type="address", value="buyer@example.org", reason="no contact",
                actor="operator")
    assert rt.dispatch_once() is None
    assert mail.sent == []
    with foundation[1].read() as c:
        assert c.execute("SELECT state FROM actions WHERE id=?", (meeting,)).fetchone()[0] == "cancelled"
    offline = runtime(foundation, live=False, mail=mail)
    with pytest.raises(InvalidStateError):
        offline.dispatch_once()


def test_inbox_dedup_classification_optout_and_reply_review(foundation):
    rt = runtime(foundation)
    aid = account(foundation, rt)
    now = utc_text(foundation[0]())
    mail = IncomingMail("m1", "t1", "buyer@example.org", "lumen@example.com",
                        "Meeting?", "Can we schedule a call?", now)
    assert rt.ingest_page(account_id=aid, messages=[mail], next_cursor="c1")["new"] == 1
    assert rt.ingest_page(account_id=aid, messages=[mail], next_cursor="c2")["duplicates"] == 1
    with foundation[1].read() as c:
        event_id = c.execute("SELECT id FROM inbound_events WHERE provider_event_id='m1'").fetchone()[0]
        assert c.execute("SELECT cursor FROM inbox_cursors WHERE account_id=?", (aid,)).fetchone()[0] == "c2"
    proposal = rt.propose_response(event_id=event_id, account_id=aid,
                                   subject="Re: Meeting", body="Thanks; I can discuss this.",
                                   author="4C")
    with foundation[1].read() as c:
        assert c.execute("SELECT state FROM response_proposals WHERE id=?", (proposal,)).fetchone()[0] == "draft"
        draft_id = c.execute("SELECT draft_id FROM response_proposals WHERE id=?", (proposal,)).fetchone()[0]
    with pytest.raises(InvalidStateError):
        rt.prepare_send(draft_id=draft_id, account_id=aid, recipient="buyer@example.org",
                        source_message_id="m1", actor="operator")
    rt.review_response(proposal_id=proposal, reviewer="operator")
    action = rt.prepare_send(draft_id=draft_id, account_id=aid,
                             recipient="buyer@example.org", source_message_id="m1", actor="operator")
    approve(foundation, rt, action)
    optout = IncomingMail("m2", "t1", "buyer@example.org", "lumen@example.com",
                          "Stop", "Please unsubscribe me", now)
    rt.ingest_page(account_id=aid, messages=[optout], next_cursor="c3")
    with foundation[1].read() as c:
        assert c.execute("SELECT state FROM actions WHERE id=?", (action,)).fetchone()[0] == "cancelled"


def test_unknown_effect_is_not_resent_and_can_be_reconciled(foundation):
    mail = FakeMail(ProviderResult("unknown"))
    rt = runtime(foundation, live=True, mail=mail)
    aid = account(foundation, rt)
    draft = reviewed_cold_draft(foundation)
    action = rt.prepare_send(draft_id=draft, account_id=aid, recipient="buyer@example.org",
                             actor="operator", recipient_timezone="UTC", timezone_source="verified")
    approve(foundation, rt, action)
    assert rt.dispatch_once()["state"] == "unknown"
    assert rt.dispatch_once() is None
    assert len(mail.sent) == 1
    mail.result = ProviderResult("accepted", "provider-reconciled")
    assert rt.reconcile_unknown(action_id=action, actor="operator")["state"] == "accepted"
    assert len(mail.sent) == 1


def test_provider_exception_is_unknown_not_safe_rejection(foundation):
    class ExplodingMail(FakeMail):
        def send(self, **kwargs):
            self.sent.append(kwargs)
            raise InvalidStateError("provider response lost")

    mail = ExplodingMail()
    rt = runtime(foundation, live=True, mail=mail)
    aid = account(foundation, rt)
    draft = reviewed_cold_draft(foundation)
    action = rt.prepare_send(draft_id=draft, account_id=aid,
                             recipient="buyer@example.org", actor="operator",
                             recipient_timezone="UTC", timezone_source="verified")
    approve(foundation, rt, action)
    assert rt.dispatch_once()["state"] == "unknown"
    assert rt.dispatch_once() is None
    assert len(mail.sent) == 1


def test_calendar_booking_requires_approval_and_fresh_slot(foundation):
    calendar = FakeCalendar()
    rt = runtime(foundation, live=True, calendar=calendar)
    aid = account(foundation, rt)
    clock = foundation[0]
    start = utc_text(clock() + timedelta(hours=2))
    end = utc_text(clock() + timedelta(hours=2, minutes=30))
    action = rt.prepare_booking(account_id=aid, attendee="buyer@example.org",
                                title="Lumen demo", start_at_utc=start, end_at_utc=end,
                                actor="operator")
    assert rt.dispatch_once() is None
    approve(foundation, rt, action)
    calendar.available_now = False
    assert rt.dispatch_once()["state"] == "rejected"
    assert calendar.booked == []


def test_stale_dispatch_recovery_never_replays(foundation):
    rt = runtime(foundation)
    clock, db = foundation[0], foundation[1]
    now = utc_text(clock())
    with db.transaction() as c:
        c.execute("INSERT INTO actions(id,kind,target_id,channel,state,idempotency_key,policy_version,"
                  "created_at_utc,updated_at_utc) VALUES ('stale','email','x','email','dispatching',"
                  "'k','p1',?,?)", (now, now))
    clock.advance(601)
    assert rt.recover_stale_dispatches() == 1
    with db.read() as c:
        assert c.execute("SELECT state FROM actions WHERE id='stale'").fetchone()[0] == "unknown"


def test_policy_change_invalidates_queued_approval(foundation):
    mail = FakeMail()
    rt = runtime(foundation, live=True, mail=mail)
    aid = account(foundation, rt)
    draft = reviewed_cold_draft(foundation)
    action = rt.prepare_send(draft_id=draft, account_id=aid,
                             recipient="buyer@example.org", actor="operator",
                             recipient_timezone="UTC", timezone_source="verified")
    approval_id = approve(foundation, rt, action)
    rt.set_send_window(account_id=aid, timezone_name="UTC", start_local="09:00",
                       end_local="17:00", weekdays=[0, 1, 2, 3, 4], actor="operator")
    with foundation[1].read() as c:
        assert c.execute("SELECT state FROM actions WHERE id=?", (action,)).fetchone()[0] == "cancelled"
        assert c.execute("SELECT decision FROM approvals WHERE id=?", (approval_id,)).fetchone()[0] == "revoked"
    assert rt.dispatch_once() is None
    assert mail.sent == []


def test_reply_slots_require_real_availability(foundation):
    rt = runtime(foundation)
    aid = account(foundation, rt)
    now = utc_text(foundation[0]())
    rt.ingest_page(account_id=aid, messages=[IncomingMail(
        "m1", "t1", "buyer@example.org", "lumen@example.com", "Meeting", "Can we meet?", now
    )], next_cursor="c1")
    with foundation[1].read() as c:
        event_id = c.execute("SELECT id FROM inbound_events WHERE provider_event_id='m1'").fetchone()[0]
    slot = {"start_at_utc": utc_text(foundation[0]() + timedelta(hours=2)),
            "end_at_utc": utc_text(foundation[0]() + timedelta(hours=3))}
    with pytest.raises(InvalidStateError):
        rt.propose_response(event_id=event_id, account_id=aid, subject="Re: Meeting",
                            body="I am free then", author="4C", slots=[slot])
    calendar = FakeCalendar()
    calendar.available_now = False
    rt = runtime(foundation, calendar=calendar)
    with pytest.raises(InvalidStateError):
        rt.propose_response(event_id=event_id, account_id=aid, subject="Re: Meeting",
                            body="I am free then", author="4C", slots=[slot])
    calendar.available_now = True
    assert rt.propose_response(event_id=event_id, account_id=aid, subject="Re: Meeting",
                               body="I am free then", author="4C", slots=[slot])


def test_quota_blocks_third_send(foundation):
    rt = runtime(foundation, live=True, mail=FakeMail())
    aid = account(foundation, rt)
    reviewed_cold_draft(foundation)
    now = utc_text(foundation[0]())
    with foundation[1].transaction() as c:
        for suffix in ("2", "3"):
            c.execute("INSERT INTO drafts(id,prospect_id,contact_id,channel,subject,body,"
                      "content_hash,version,evidence_json,author_role,status,created_at_utc) "
                      "SELECT ?,prospect_id,contact_id,channel,subject,body,content_hash,version,"
                      "evidence_json,author_role,status,? FROM drafts WHERE id='draft'",
                      ("draft" + suffix, now))
            c.execute("INSERT INTO content_reviews(id,draft_id,content_hash,input_hash,rule_version,"
                      "review_round,decision,reviewer_role,reviewed_at_utc) "
                      "SELECT ?,?,content_hash,input_hash,rule_version,review_round,decision,"
                      "reviewer_role,? FROM content_reviews WHERE id='review'",
                      ("review" + suffix, "draft" + suffix, now))
    for draft_id in ("draft", "draft2"):
        action = rt.prepare_send(draft_id=draft_id, account_id=aid,
                                 recipient="buyer@example.org", actor="operator",
                                 recipient_timezone="UTC", timezone_source="verified")
        approve(foundation, rt, action)
        assert rt.dispatch_once()["state"] == "accepted"
    third = rt.prepare_send(draft_id="draft3", account_id=aid,
                            recipient="buyer@example.org", actor="operator",
                            recipient_timezone="UTC", timezone_source="verified")
    approve(foundation, rt, third)
    with pytest.raises(InvalidStateError, match="quota exhausted"):
        rt.dispatch_once()
    assert len(rt.mail.sent) == 2


def test_non_utc_window_is_enforced_on_windows(foundation):
    mail = FakeMail()
    rt = runtime(foundation, live=True, mail=mail)
    aid = account(foundation, rt)
    draft = reviewed_cold_draft(foundation)
    rt.set_send_window(account_id=aid, timezone_name="America/Chicago",
                       start_local="09:00", end_local="17:00",
                       weekdays=[0, 1, 2, 3, 4], actor="operator")
    action = rt.prepare_send(draft_id=draft, account_id=aid,
                             recipient="buyer@example.org", actor="operator",
                             recipient_timezone="America/Chicago", timezone_source="verified")
    approve(foundation, rt, action, hours=4)
    with pytest.raises(InvalidStateError, match="outside approved local send window"):
        rt.dispatch_once()
    assert mail.sent == []
    foundation[0].advance(2 * 3600)  # 14:00 UTC is 09:00 in Chicago on this fixture date.
    assert rt.dispatch_once()["state"] == "accepted"
