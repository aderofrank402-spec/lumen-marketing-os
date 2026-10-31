from __future__ import annotations

from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

import pytest

from lumen_marketing_os.core.errors import ConflictError, PausedError
from lumen_marketing_os.workflows.operations.scheduler import ScheduleManager
from lumen_marketing_os.workflows.operations.ledger import OperationsLedger


def test_schedule_is_atomic_idempotent_and_restart_safe(foundation):
    clock, db, pause, queue, _ = foundation
    scheduler = ScheduleManager(db, pause, queue, clock=clock)
    scheduler.configure(schedule_id="research-hourly", workflow="research.tick",
                        interval_seconds=3600, next_due_at=clock(), active=True)
    first = scheduler.tick()
    assert len(first) == 1 and first[0]["decision"] == "enqueued"
    assert scheduler.tick() == []
    assert ScheduleManager(db, pause, queue, clock=clock).tick() == []
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM schedule_occurrences").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM jobs WHERE workflow='operations.dispatch'").fetchone()[0] == 1
    calls = []
    result = scheduler.work_once({"research.tick": lambda: calls.append("tick")})
    assert result["state"] == "succeeded"
    assert calls == ["tick"]
    assert scheduler.work_once({"research.tick": lambda: None}) is None


def test_catch_up_cap_overlap_window_and_pause(foundation):
    clock, db, pause, queue, _ = foundation
    scheduler = ScheduleManager(db, pause, queue, clock=clock)
    scheduler.configure(schedule_id="catchup", workflow="content.tick", interval_seconds=60,
                        next_due_at=clock() - timedelta(minutes=4), catch_up="all", active=True,
                        max_overlap=1)
    choices = scheduler.tick(max_catch_up_per_schedule=3)
    assert [x["decision"] for x in choices] == ["enqueued", "overlap_skipped", "overlap_skipped"]
    assert scheduler.tick()[0]["decision"] == "overlap_skipped"
    scheduler.configure(schedule_id="window", workflow="research.tick", interval_seconds=60,
                        next_due_at=clock(), timezone_name="UTC", window_start="23:00",
                        window_end="23:59", active=True)
    assert any(x["decision"] == "outside_window" for x in scheduler.tick())
    pause.pause(actor="test", reason="stop")
    with pytest.raises(PausedError):
        scheduler.tick()


def test_skip_policy_drops_overdue_burst(foundation):
    clock, db, pause, queue, _ = foundation
    scheduler = ScheduleManager(db, pause, queue, clock=clock)
    scheduler.configure(schedule_id="skip", workflow="research.tick", interval_seconds=60,
                        next_due_at=clock() - timedelta(minutes=5), catch_up="skip", active=True)
    assert scheduler.tick()[0]["decision"] == "catch_up_skipped"
    assert scheduler.tick() == []
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs WHERE workflow='operations.dispatch'").fetchone()[0] == 0


def test_concurrent_tickers_create_only_one_occurrence(foundation):
    clock, db, pause, queue, _ = foundation
    first = ScheduleManager(db, pause, queue, clock=clock)
    second = ScheduleManager(db, pause, queue, clock=clock)
    first.configure(schedule_id="parallel", workflow="research.tick", interval_seconds=60,
                    next_due_at=clock(), active=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda scheduler: scheduler.tick(), (first, second)))
    assert sum(len(result) for result in results) == 1
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM schedule_occurrences").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM jobs WHERE workflow='operations.dispatch'").fetchone()[0] == 1


def test_schedule_change_requires_current_version(foundation):
    clock, db, pause, queue, _ = foundation
    scheduler = ScheduleManager(db, pause, queue, clock=clock)
    version = scheduler.configure(schedule_id="versioned", workflow="content.tick",
                                  interval_seconds=60, next_due_at=clock())
    with pytest.raises(ConflictError):
        scheduler.configure(schedule_id="versioned", workflow="content.tick",
                            interval_seconds=120, next_due_at=clock())
    new_version = scheduler.configure(schedule_id="versioned", workflow="content.tick",
                                      interval_seconds=120, next_due_at=clock(),
                                      expected_version=version)
    assert version != new_version


def test_department_one_schedules_are_not_claimed_or_mutated(foundation):
    clock, db, pause, queue, _ = foundation
    now = clock().isoformat(timespec="microseconds")
    with db.transaction() as c:
        c.execute("INSERT INTO schedule_definitions(id,workflow,lane,interval_seconds,timezone,"
                  "catch_up_policy,active,next_due_at_utc,config_version) "
                  "VALUES ('research.1A','department1.collect','research.1A',60,'UTC','one',1,?,'old')",
                  (now,))
    scheduler = ScheduleManager(db, pause, queue, clock=clock)
    assert scheduler.tick() == []
    with pytest.raises(ConflictError):
        scheduler.configure(schedule_id="research.1A", workflow="research.tick",
                            interval_seconds=60, next_due_at=clock(), expected_version="old")
    with db.read() as c:
        row = c.execute("SELECT workflow,next_due_at_utc FROM schedule_definitions "
                        "WHERE id='research.1A'").fetchone()
    assert (row["workflow"], row["next_due_at_utc"]) == ("department1.collect", now)


def test_scheduled_backup_and_restore_are_local(foundation, tmp_path):
    clock, db, pause, queue, _ = foundation
    scheduler = ScheduleManager(db, pause, queue, clock=clock)
    ledger = OperationsLedger(db, clock=clock)
    scheduler.configure(schedule_id="daily-backup", workflow="operations.backup",
                        interval_seconds=86400, next_due_at=clock(), active=True)
    assert scheduler.tick()[0]["decision"] == "enqueued"
    handlers = {"operations.backup": lambda: ledger.scheduled_backup(tmp_path / "backups")}
    result = scheduler.work_once(handlers)
    assert result["state"] == "succeeded"
    assert ledger.restore_latest()["passed"]
    assert scheduler.health()["ok"]


def test_usage_estimate_idempotency_reconciliation_and_unknown(foundation):
    clock, db, _, _, _ = foundation
    ledger = OperationsLedger(db, clock=clock)
    unknown = ledger.record_usage(usage_key="u0", provider="example", model="small",
                                  prompt_tokens=100, completion_tokens=50, requests=1,
                                  occurred_at=clock())
    assert unknown["status"] == "unknown" and unknown["estimated_minor"] is None
    price_id = ledger.approve_price(provider="example", model="small", currency="USD",
                                    prompt_per_million_minor=100, completion_per_million_minor=200,
                                    request_minor=5, effective_at=clock(), actor="owner")
    item = ledger.record_usage(usage_key="u1", provider="example", model="small",
                               prompt_tokens=100, completion_tokens=50, requests=1,
                               occurred_at=clock())
    assert item["price_id"] == price_id and item["estimated_minor"] == 6
    assert ledger.record_usage(usage_key="u1", provider="example", model="small",
                               prompt_tokens=100, completion_tokens=50, requests=1,
                               occurred_at=clock())["id"] == item["id"]
    with pytest.raises(ConflictError):
        ledger.record_usage(usage_key="u1", provider="example", model="small",
                            prompt_tokens=101, completion_tokens=50, requests=1,
                            occurred_at=clock())
    settled = ledger.reconcile(usage_key="u1", actual_minor=8, currency="USD", actor="finance")
    assert settled["status"] == "reconciled" and settled["actual_minor"] == 8
    assert ledger.report()["unknown_usage_events"] == 1


def test_backup_restore_drill_is_isolated_and_detects_tamper(foundation, tmp_path):
    clock, db, _, _, _ = foundation
    ledger = OperationsLedger(db, clock=clock)
    destination = tmp_path / "backups" / "snapshot.db"
    record = ledger.backup(destination)
    assert record["sha256"]
    assert ledger.restore_drill(backup_id=record["backup_id"])["passed"]
    with destination.open("ab") as handle:
        handle.write(b"tamper")
    assert not ledger.restore_drill(backup_id=record["backup_id"])["passed"]
    assert db.integrity_check()["ok"]
