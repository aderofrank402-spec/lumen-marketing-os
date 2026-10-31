from __future__ import annotations

import pytest

from lumen_marketing_os.core.errors import PausedError


def test_global_pause_file_database_and_resume(foundation):
    _, database, pause, queue, _ = foundation
    job = queue.enqueue(workflow="research.scan", lane="research", payload={}, logical_key="scan-1")
    assert pause.pause(actor="owner", reason="incident").paused is True
    assert pause.pause_file.is_file()
    with pytest.raises(PausedError):
        queue.claim(worker_id="worker")
    pause.pause_file.unlink()
    with pytest.raises(PausedError):
        queue.claim(worker_id="worker")
    assert pause.resume(actor="owner", reason="resolved").paused is False
    assert not pause.pause_file.exists()
    assert queue.claim(worker_id="worker").id == job.id
    with database.read() as connection:
        types = [r[0] for r in connection.execute(
            "SELECT event_type FROM audit_events WHERE entity_type='control' ORDER BY occurred_at_utc")]
    assert "control.paused" in types and "control.resumed" in types


def test_manual_file_and_lane_pause(foundation):
    _, _, pause, queue, _ = foundation
    pause.pause_file.touch()
    assert pause.status().paused is True
    with pytest.raises(PausedError):
        queue.claim(worker_id="worker")
    pause.resume(actor="owner", reason="manual pause cleared")
    pause.pause(actor="owner", reason="mail issue", lane="email")
    email = queue.enqueue(workflow="mail.poll", lane="email", payload={}, logical_key="mail")
    research = queue.enqueue(workflow="research.scan", lane="research", payload={}, logical_key="research")
    assert pause.status("email").paused is True
    assert pause.status("research").paused is False
    assert queue.claim(worker_id="worker").id == research.id
    with pytest.raises(PausedError):
        queue.claim(worker_id="worker", lane="email")
    pause.resume(actor="owner", reason="fixed", lane="email")
    assert queue.claim(worker_id="worker", lane="email").id == email.id
