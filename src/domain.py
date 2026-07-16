from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class StatusCode(str, Enum):
    CREATED = "created"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


class HealthStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"


class DatabaseStatus(str, Enum):
    OK = "ok"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    body_raw: bytes
    payload: Any
    received_at: datetime
    duplicate_count: int = 0

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("event_id must not be empty")
        if not isinstance(self.body_raw, bytes):
            raise TypeError("body_raw must be bytes")
        if not isinstance(self.received_at, datetime):
            raise TypeError("received_at must be a datetime")
        if type(self.duplicate_count) is not int or self.duplicate_count < 0:
            raise ValueError("duplicate_count must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class EventResult:
    event: Event
    status: StatusCode


@dataclass(frozen=True, slots=True)
class HealthResult:
    status: HealthStatus
    database: DatabaseStatus


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
