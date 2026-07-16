from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

from src.domain import Event, EventResult, StatusCode

# Set before any lock-taking statement so SQLite waits out contention on its
# own file-level locks instead of erroring. This is the only cross-process
# mechanism available: a threading.Lock would not serialize separate processes.
_BUSY_TIMEOUT_MS = 5000

# Bounded retry budget for the one-time conversion of a fresh database to WAL
# mode. Conversion needs an exclusive lock, so the very first connections to a
# new file can collide; the read-first check below means only the opening wave
# ever attempts it, and this loop lets stragglers observe the finished
# conversion (re-reading the mode each attempt) without retrying a doomed one.
_WAL_INIT_ATTEMPTS = 5
_WAL_INIT_BACKOFF_BASE_S = 0.01

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


def _init_connection(conn: sqlite3.Connection) -> None:
    """Initialize a connection safely across concurrent processes.

    Order matters: busy_timeout first (so SQLite waits on its own locks),
    then read the current journal mode. A read of ``PRAGMA journal_mode`` needs
    only a shared lock, so it is cheap and contention-free. If the database is
    already in WAL mode we must NOT re-run ``PRAGMA journal_mode=WAL``: that
    write form takes an exclusive lock, and on a fresh database many concurrent
    converters would convoy and exceed the busy budget. Only when the mode is
    not yet WAL do we attempt conversion, with bounded retry + backoff. Each
    retry re-reads the mode first, so a straggler observes the winner's finished
    conversion and returns without issuing another doomed ``WAL`` statement.
    On terminal failure the connection is closed so the caller holds no wedged
    handle, then the last error is surfaced.
    """
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    last_exc: sqlite3.OperationalError | None = None
    for attempt in range(_WAL_INIT_ATTEMPTS):
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if mode == "wal":
            _ensure_schema(conn)
            return
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            _ensure_schema(conn)
            return
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if attempt + 1 >= _WAL_INIT_ATTEMPTS:
                break
            time.sleep(_WAL_INIT_BACKOFF_BASE_S * (2**attempt))
    conn.close()
    assert last_exc is not None
    raise last_exc


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)
    conn.commit()


class Repository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        _init_connection(self._conn)

    def close(self) -> None:
        self._conn.close()

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
        return self._upsert_event_inner(event)

    def _upsert_event_inner(self, event: Event) -> EventResult:
        try:
            self._conn.execute(
                "INSERT INTO events (event_id, body_raw, payload_json, received_at, duplicate_count) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.body_raw,
                    json.dumps(event.payload),
                    event.received_at.isoformat(),
                    event.duplicate_count,
                ),
            )
        except sqlite3.IntegrityError:
            row = self._conn.execute(
                "SELECT body_raw, payload_json, received_at, duplicate_count "
                "FROM events WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if row is None:
                # Unreachable with the current PRIMARY KEY schema (a conflict
                # implies the row exists), but rollback leaves a clean
                # transaction state rather than returning under an open failure.
                self._conn.rollback()
                return EventResult(event=event, status=StatusCode.CREATED)
            body_raw, payload_json, received_at, duplicate_count = row
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
            self._conn.rollback()
            return EventResult(
                event=Event(
                    event_id=event.event_id,
                    body_raw=body_raw,
                    payload=json.loads(payload_json),
                    received_at=_parse_timestamp(received_at),
                    duplicate_count=duplicate_count,
                ),
                status=StatusCode.CONFLICT,
            )
        else:
            self._conn.commit()
            return EventResult(event=event, status=StatusCode.CREATED)


def new_repository(db_path: str) -> Repository:
    conn = sqlite3.connect(db_path, timeout=5.0)
    return Repository(conn)
