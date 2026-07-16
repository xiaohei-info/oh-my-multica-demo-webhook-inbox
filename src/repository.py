from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from src.domain import Event, EventResult, StatusCode
from src.errors import ConflictError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    body_raw BLOB NOT NULL,
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL,
    duplicate_count INTEGER NOT NULL DEFAULT 0
);
"""


def _parse_timestamp(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class Repository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def get_event(self, event_id: str) -> Event | None:
        row = self._conn.execute(
            "SELECT event_id, body_raw, payload_json, received_at, duplicate_count "
            "FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        return Event(
            event_id=row[0],
            body_raw=row[1],
            payload=json.loads(row[2]),
            received_at=_parse_timestamp(row[3]),
            duplicate_count=row[4],
        )

    def upsert_event(self, event: Event) -> EventResult:
        existing = self._conn.execute(
            "SELECT body_raw, payload_json, received_at, duplicate_count FROM events WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()
        if existing is not None:
            body_raw, payload_json, received_at, duplicate_count = existing
            if body_raw == event.body_raw:
                new_count = duplicate_count + 1
                self._conn.execute(
                    "UPDATE events SET duplicate_count = ? WHERE event_id = ?",
                    (new_count, event.event_id),
                )
                self._conn.commit()
                return EventResult(
                    event=Event(
                        event_id=event.event_id,
                        body_raw=event.body_raw,
                        payload=json.loads(payload_json),
                        received_at=_parse_timestamp(received_at),
                        duplicate_count=new_count,
                    ),
                    status=StatusCode.DUPLICATE,
                )
            raise ConflictError()

        self._conn.execute(
            "INSERT INTO events (event_id, body_raw, payload_json, received_at, duplicate_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.body_raw,
                json.dumps(event.payload),
                event.received_at,
                event.duplicate_count,
            ),
        )
        self._conn.commit()
        return EventResult(event=event, status=StatusCode.CREATED)


def new_repository(db_path: str) -> Repository:
    conn = sqlite3.connect(db_path)
    return Repository(conn)
