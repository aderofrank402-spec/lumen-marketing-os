from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lumen_marketing_os.db.database import Database
from lumen_marketing_os.engine.outbox import Outbox
from lumen_marketing_os.engine.queue import JobQueue
from lumen_marketing_os.policy.pause import PauseControl


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def foundation(tmp_path):
    clock = FixedClock()
    database = Database(tmp_path / "runtime" / "lumen_growth.db")
    database.initialize()
    pause = PauseControl(database, tmp_path / "PAUSE_ALL.lock", clock=clock)
    queue = JobQueue(database, pause, clock=clock, retry_base_seconds=30,
                     retry_max_seconds=300)
    outbox = Outbox(database, pause, clock=clock)
    return clock, database, pause, queue, outbox
