from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must have a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("stored timestamp has no timezone")
    return parsed.astimezone(timezone.utc)
