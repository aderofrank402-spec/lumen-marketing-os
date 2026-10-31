from __future__ import annotations

import pytest
import json
from pathlib import Path

from lumen_marketing_os.cli.main import main
from lumen_marketing_os.core.errors import InvalidStateError, PausedError
from lumen_marketing_os.workflows.outreach.runtime import IncomingMail, OutreachRuntime
from lumen_marketing_os.workflows.support.runtime import SupportRuntime


def _runtime(foundation):
    clock, db, pause, *_ = foundation
    return SupportRuntime(db, pause, clock=clock)


def _source(runtime):
    source = runtime.register_source(key="manual-support", kind="manual",
                                     external_reference="operator-reviewed export", actor="owner")
    runtime.enable_source(source_id=source, actor="owner", verified=True)
    return source


def _event(event_id="evt-1", body="The lookup crashes when I enter a business name"):
    return {"provider_event_id": event_id, "subject": "Issue with lookup", "body": body,
            "sender": "person@example.com", "received_at_utc": "2026-09-30T11:00:00+00:00"}


def test_intake_deduplicates_and_cursor_is_transactional(foundation):
    runtime = _runtime(foundation)
    source = _source(runtime)
    first = runtime.ingest_batch(source_id=source, items=[_event()], next_cursor="page-2", actor="9A")
    second = runtime.ingest_batch(source_id=source, items=[_event()], next_cursor="page-2", actor="9A")
    assert first["created"] == 1 and second["duplicates"] == 1
    assert first["ticket_ids"] == second["ticket_ids"]
    with pytest.raises(ValueError):
        runtime.ingest_batch(source_id=source, items=[_event("evt-2"), {"bad": True}],
                             next_cursor="page-3", actor="9A")
    report = runtime.report()
    assert len(report["tickets"]) == 1
    assert report["sources"][0]["cursor_value"] == "page-2"


def test_triage_review_reply_and_sheet_version_gate(foundation):
    runtime = _runtime(foundation)
    source = _source(runtime)
    ticket = runtime.ingest_batch(source_id=source, items=[_event()], actor="9A")["ticket_ids"][0]
    result = runtime.triage(ticket_id=ticket, actor="9B")
    assert result["category"] == "bug"
    assert result["state"] == "needs_review"
    with pytest.raises(InvalidStateError):
        runtime.triage(ticket_id=ticket, actor="9B")
    runtime.review(ticket_id=ticket, category="bug", priority="high", state="open",
                   rationale="Reproduced locally in a controlled fixture", actor="reviewer")
    draft = runtime.draft_response(ticket_id=ticket, subject="Re: Issue with lookup",
                                   body="Thanks for reporting this. We are investigating and will update you.",
                                   author="9D")
    details = runtime.ticket(ticket)
    digest = details["drafts"][0]["body_sha256"]
    with pytest.raises(InvalidStateError):
        runtime.approve_response(draft_id=draft, reviewer="9D", expected_hash=digest)
    runtime.approve_response(draft_id=draft, reviewer="owner", expected_hash=digest)
    projection = runtime.sheet_projection(ticket_id=ticket, spreadsheet_id="sheet-1", actor="9C")
    assert projection["state"] == "pending"
    with pytest.raises(InvalidStateError):
        runtime.acknowledge_sheet(ticket_id=ticket, spreadsheet_id="sheet-1",
                                  version=projection["version"], digest="wrong", actor="9C")
    runtime.acknowledge_sheet(ticket_id=ticket, spreadsheet_id="sheet-1",
                              version=projection["version"], digest=projection["hash"], actor="9C")
    assert runtime.sheet_projection(ticket_id=ticket, spreadsheet_id="sheet-1", actor="9C")["state"] == "synced"
    runtime.review(ticket_id=ticket, category="bug", priority="high", state="resolved",
                   rationale="Issue resolved in verified test", actor="owner")
    assert runtime.ticket(ticket)["drafts"][0]["state"] == "retired"
    assert runtime.report()["pending_sheet"][0]["ticket_id"] == ticket
    with pytest.raises(InvalidStateError):
        runtime.acknowledge_sheet(ticket_id=ticket, spreadsheet_id="sheet-1",
                                  version=projection["version"], digest=projection["hash"], actor="9C")


def test_security_and_billing_escalate_without_automatic_reply(foundation):
    runtime = _runtime(foundation)
    source = _source(runtime)
    items = [_event("security", "We found an exposed API key in our account"),
             _event("billing", "I was charged twice for my subscription")]
    tickets = runtime.ingest_batch(source_id=source, items=items, actor="9A")["ticket_ids"]
    decisions = [runtime.triage(ticket_id=t, actor="9B") for t in tickets]
    assert decisions[0]["priority"] == "urgent" and decisions[0]["state"] == "needs_review"
    assert decisions[1]["category"] == "billing" and decisions[1]["state"] == "needs_review"
    with pytest.raises(InvalidStateError):
        runtime.draft_response(ticket_id=tickets[0], subject="Re: urgent", body="Fixed!", author="9D")


def test_pause_blocks_mutation(foundation):
    runtime = _runtime(foundation)
    _, _, pause, *_ = foundation
    source = _source(runtime)
    pause.pause(actor="owner", reason="incident", lane="support")
    with pytest.raises(PausedError):
        runtime.ingest_batch(source_id=source, items=[_event()], actor="9A")
    assert runtime.report()["tickets"] == []


def test_tick_classifies_once_without_external_action(foundation):
    runtime = _runtime(foundation)
    source = _source(runtime)
    runtime.ingest_batch(source_id=source, items=[_event()], actor="9A")
    assert runtime.tick()["triaged"] == 1
    assert runtime.tick()["triaged"] == 0
    with foundation[1].read() as c:
        assert c.execute("SELECT count(*) FROM actions").fetchone()[0] == 0


def test_gmail_handoff_creates_proposal_but_no_send(foundation):
    clock, db, pause, *_ = foundation
    outreach = OutreachRuntime(database=db, pause=pause, suppression_key=b"x" * 32, clock=clock)
    account = outreach.register_account(email="lumen@example.com", policy_version="p1", actor="owner")
    outreach.enable_account(account_id=account, actor="owner", confirmed=True)
    outreach.ingest_page(account_id=account, messages=[IncomingMail(
        provider_id="gmail-1", thread_id="thread-1", sender="customer@example.com",
        recipient="lumen@example.com", subject="Lookup error", body="The lookup crashes",
        received_at_utc="2026-09-30T11:00:00+00:00")], next_cursor="cursor-2")
    with db.read() as c:
        inbound = c.execute("SELECT id FROM inbound_events WHERE provider_event_id='gmail-1'").fetchone()[0]
    runtime = _runtime(foundation)
    source = runtime.register_source(key="gmail-support", kind="gmail", external_reference="lumen@example.com",
                                     account_id=account, actor="owner")
    runtime.enable_source(source_id=source, actor="owner", verified=True)
    ticket = runtime.ingest_batch(source_id=source, items=[{
        "provider_event_id": "gmail-1", "inbound_event_id": inbound,
        "received_at_utc": "2026-09-30T11:00:00+00:00"}], actor="9A")["ticket_ids"][0]
    runtime.triage(ticket_id=ticket, actor="9B")
    runtime.review(ticket_id=ticket, category="bug", priority="normal", state="open",
                   rationale="Reviewed reported lookup failure", actor="owner")
    draft = runtime.draft_response(ticket_id=ticket, subject="Re: Lookup error",
                                   body="Thanks for the report. We are investigating.", author="9D")
    digest = runtime.ticket(ticket)["drafts"][0]["body_sha256"]
    runtime.approve_response(draft_id=draft, reviewer="owner", expected_hash=digest)
    handoff = runtime.handoff_email(draft_id=draft, outreach=outreach, actor="9D")
    assert handoff["sent"] is False
    assert runtime.handoff_email(draft_id=draft, outreach=outreach, actor="9D") == handoff
    with db.read() as c:
        assert c.execute("SELECT count(*) FROM actions").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM delivery_attempts").fetchone()[0] == 0


def test_support_cli_local_intake_and_tick(tmp_path, capsys):
    project = Path(__file__).resolve().parents[1]
    config = tmp_path / "config"
    config.mkdir()
    for name in ("base.json", "development.json"):
        (config / name).write_text((project / "config" / name).read_text(encoding="utf-8"),
                                   encoding="utf-8")
    prefix = ["--workspace", str(tmp_path)]
    assert main(prefix + ["init"]) == 0
    capsys.readouterr()
    assert main(prefix + ["support", "register-source", "--key", "local", "--kind", "manual",
                          "--reference", "operator test", "--actor", "owner"]) == 0
    source = json.loads(capsys.readouterr().out)["source_id"]
    assert main(prefix + ["support", "enable-source", source, "--verified", "--actor", "owner"]) == 0
    capsys.readouterr()
    event_file = tmp_path / "events.json"
    event_file.write_text(json.dumps([_event()]), encoding="utf-8")
    assert main(prefix + ["support", "ingest-file", source, "--file", str(event_file),
                          "--actor", "9A"]) == 0
    ticket = json.loads(capsys.readouterr().out)["ticket_ids"][0]
    assert main(prefix + ["support", "tick"]) == 0
    assert json.loads(capsys.readouterr().out)["triaged"] == 1
    assert main(prefix + ["support", "ticket", ticket]) == 0
    assert json.loads(capsys.readouterr().out)["category"] == "bug"
