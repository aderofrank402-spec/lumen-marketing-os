from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from lumen_marketing_os.core.errors import ConflictError, InvalidStateError, LeaseLostError
from lumen_marketing_os.engine.queue import JobQueue, OutgoingEvent


def test_enqueue_is_idempotent_and_conflicts_are_visible(foundation):
    _, _, _, queue, _ = foundation
    first = queue.enqueue(workflow="research.scan", lane="research", payload={"q": "a"}, logical_key="run-1")
    same = queue.enqueue(workflow="research.scan", lane="research", payload={"q": "a"}, logical_key="run-1")
    assert first.id == same.id
    with pytest.raises(ConflictError):
        queue.enqueue(workflow="research.scan", lane="research", payload={"q": "b"}, logical_key="run-1")
    assert queue.counts() == {"pending": 1}


def test_concurrent_claim_has_one_winner_and_wrong_worker_cannot_complete(foundation):
    _, database, pause, queue, _ = foundation
    job = queue.enqueue(workflow="research.scan", lane="research", payload={}, logical_key="run-1")
    def claim(worker):
        return JobQueue(database, pause, clock=queue.clock).claim(worker_id=worker)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("one", "two")))
    claimed = [result for result in results if result is not None]
    assert len(claimed) == 1 and claimed[0].id == job.id
    with pytest.raises(LeaseLostError):
        queue.complete(job_id=job.id, worker_id="stranger")
    assert queue.complete(job_id=job.id, worker_id=claimed[0].lease_owner).state == "succeeded"


def test_retry_backoff_and_exhaustion(foundation):
    clock, _, _, queue, _ = foundation
    job = queue.enqueue(workflow="research.scan", lane="research", payload={},
                        logical_key="run-1", max_attempts=2)
    assert queue.claim(worker_id="worker").attempt_count == 1
    assert queue.fail(job_id=job.id, worker_id="worker", error_class="network", retryable=True).state == "retry_wait"
    assert queue.claim(worker_id="worker") is None
    clock.advance(30)
    assert queue.claim(worker_id="worker").attempt_count == 2
    assert queue.fail(job_id=job.id, worker_id="worker", error_class="network", retryable=True).state == "dead_letter"
    assert queue.claim(worker_id="worker") is None
    assert queue.replay(job_id=job.id, actor="owner", reason="provider restored").state == "pending"
    assert queue.claim(worker_id="worker").attempt_count == 3


def test_expired_internal_lease_recovers_but_external_needs_review(foundation):
    clock, _, _, queue, _ = foundation
    internal = queue.enqueue(workflow="scan", lane="research", payload={}, logical_key="internal")
    external = queue.enqueue(workflow="send", lane="email", payload={}, logical_key="external", effect="external")
    assert queue.claim(worker_id="worker", lease_seconds=10).id == internal.id
    enabled = JobQueue(queue.database, queue.pause, clock=clock, external_actions_enabled=True)
    assert enabled.claim(worker_id="worker", lease_seconds=10).id == external.id
    clock.advance(11)
    recovered = queue.recover_expired()
    assert recovered == {"retried": 1, "review": 1, "dead_letter": 0}
    assert queue.get(internal.id).state == "retry_wait"
    assert queue.get(external.id).state == "needs_review"
    assert queue.claim(worker_id="second").id == internal.id
    with pytest.raises(InvalidStateError):
        queue.replay(job_id=external.id, actor="owner", reason="unsure")
    assert queue.replay(job_id=external.id, actor="owner", reason="provider checked",
                        external_effect_ruled_out=True).state == "pending"


def test_external_jobs_are_disabled_by_default(foundation):
    _, _, _, queue, _ = foundation
    queue.enqueue(workflow="send", lane="email", payload={}, logical_key="send", effect="external")
    assert queue.claim(worker_id="worker") is None


def test_heartbeat_and_expired_lease_protection(foundation):
    clock, _, _, queue, _ = foundation
    job = queue.enqueue(workflow="scan", lane="research", payload={}, logical_key="scan")
    queue.claim(worker_id="one", lease_seconds=10)
    clock.advance(5)
    queue.heartbeat(job_id=job.id, worker_id="one", lease_seconds=20)
    clock.advance(11)
    assert queue.complete(job_id=job.id, worker_id="one").state == "succeeded"
    another = queue.enqueue(workflow="scan", lane="research", payload={}, logical_key="scan-2")
    queue.claim(worker_id="one", lease_seconds=10)
    clock.advance(11)
    with pytest.raises(LeaseLostError):
        queue.complete(job_id=another.id, worker_id="one")


def test_complete_and_outbox_commit_atomically(foundation):
    _, database, _, queue, outbox = foundation
    first = queue.enqueue(workflow="scan", lane="research", payload={}, logical_key="one")
    queue.claim(worker_id="worker")
    event = OutgoingEvent("prospect.discovered", "prospect", "p-1", "qualification",
                          {"prospect_id": "p-1"}, "prospect:p-1")
    queue.complete(job_id=first.id, worker_id="worker", events=[event])
    assert outbox.counts() == {"pending": 1}
    second = queue.enqueue(workflow="scan", lane="research", payload={}, logical_key="two")
    queue.claim(worker_id="worker")
    with pytest.raises(sqlite3.IntegrityError):
        queue.complete(job_id=second.id, worker_id="worker", events=[event])
    assert queue.get(second.id).state == "leased"
    assert outbox.counts() == {"pending": 1}
    item = outbox.claim(worker_id="projector")
    assert item.deduplication_key == "prospect:p-1"
    assert outbox.acknowledge(item_id=item.id, worker_id="projector").state == "delivered"
    assert outbox.counts() == {"delivered": 1}
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 1


def test_domain_change_outbox_and_job_state_share_one_transaction(foundation):
    _, database, _, queue, _ = foundation
    job = queue.enqueue(workflow="research.scan", lane="research", payload={}, logical_key="atomic")
    queue.claim(worker_id="worker")
    def add_organization(connection):
        connection.execute(
            "INSERT INTO organizations(id,display_name,normalized_name,identity_status,"
            "created_at_utc,updated_at_utc) VALUES ('org','Example','example','candidate','now','now')"
        )
    bad_event = OutgoingEvent("prospect.discovered", "prospect", "p-1", "qualification",
                              {}, "")
    with pytest.raises(ValueError):
        queue.complete(job_id=job.id, worker_id="worker", mutate=add_organization,
                       events=[bad_event])
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 0
    assert queue.get(job.id).state == "leased"
    event = OutgoingEvent("prospect.discovered", "prospect", "p-1", "qualification",
                          {}, "p-1")
    assert queue.complete(job_id=job.id, worker_id="worker", mutate=add_organization,
                          events=[event]).state == "succeeded"
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 1
