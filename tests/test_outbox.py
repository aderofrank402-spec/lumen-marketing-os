from __future__ import annotations

import pytest

from lumen_marketing_os.core.errors import LeaseLostError, PausedError
from lumen_marketing_os.engine.queue import OutgoingEvent


def _seed(foundation):
    _, _, _, queue, _ = foundation
    job = queue.enqueue(workflow="research.scan", lane="research", payload={}, logical_key="seed")
    queue.claim(worker_id="worker")
    queue.complete(job_id=job.id, worker_id="worker", events=[
        OutgoingEvent("signal.observed", "signal", "s-1", "research", {"id": "s-1"}, "signal:s-1")
    ])


def test_outbox_retry_and_recovery(foundation):
    clock, _, _, _, outbox = foundation
    _seed(foundation)
    first = outbox.claim(worker_id="one", lease_seconds=10)
    assert first.attempt_count == 1
    with pytest.raises(LeaseLostError):
        outbox.acknowledge(item_id=first.id, worker_id="two")
    clock.advance(11)
    assert outbox.recover_expired() == 1
    second = outbox.claim(worker_id="two")
    assert second.id == first.id and second.attempt_count == 2
    assert outbox.fail(item_id=second.id, worker_id="two", error_class="network",
                       retry_after_seconds=15).state == "retry_wait"
    assert outbox.claim(worker_id="three") is None
    clock.advance(15)
    third = outbox.claim(worker_id="three")
    assert third.attempt_count == 3
    assert outbox.acknowledge(item_id=third.id, worker_id="three").state == "delivered"


def test_pause_blocks_outbox_delivery(foundation):
    _, _, pause, _, outbox = foundation
    _seed(foundation)
    pause.pause(actor="owner", reason="incident")
    with pytest.raises(PausedError):
        outbox.claim(worker_id="worker")
    assert outbox.counts() == {"pending": 1}
