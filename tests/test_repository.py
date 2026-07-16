from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pytest

from src.domain import Event, StatusCode
from src.repository import Repository, new_repository

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def repo(tmp_path: str) -> Repository:
    db_path = f"{tmp_path}/inbox.db"
    return new_repository(db_path)


def _event(event_id: str, body: str, count: int = 0) -> Event:
    return Event(
        event_id=event_id,
        body_raw=body.encode(),
        payload={"v": body},
        received_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        duplicate_count=count,
    )


class TestRepositoryLifecycle:
    def test_creates_schema_in_new_database(self, tmp_path: str) -> None:
        db_path = f"{tmp_path}/inbox.db"
        new_repository(db_path)
        result = (
            sqlite3.connect(db_path)
            .execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
            )
            .fetchone()
        )
        assert result is not None

    def test_pragma_setup_runs_on_open(self, tmp_path: str) -> None:
        db_path = f"{tmp_path}/inbox.db"
        repo = new_repository(db_path)
        mode = repo._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_busy_timeout_is_set(self, tmp_path: str) -> None:
        db_path = f"{tmp_path}/inbox.db"
        repo = new_repository(db_path)
        timeout = repo._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout >= 1000

    def test_creates_schema_idempotently(self, tmp_path: str) -> None:
        db_path = f"{tmp_path}/inbox.db"
        new_repository(db_path)
        conn = sqlite3.connect(db_path)
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY)"
        )
        # Should not error if schema exists
        new_repository(db_path)


class TestUpsertEvent:
    def test_creates_new_event_and_returns_created(self, repo: Repository) -> None:
        event = _event("evt-1", "first")
        result = repo.upsert_event(event)
        assert result.status is StatusCode.CREATED
        assert result.event.event_id == "evt-1"
        assert result.event.duplicate_count == 0

    def test_idempotent_duplicate_increments_count(self, repo: Repository) -> None:
        event = _event("evt-1", "first")
        first = repo.upsert_event(event)
        second = repo.upsert_event(event)
        assert first.status is StatusCode.CREATED
        assert second.status is StatusCode.DUPLICATE
        assert second.event.duplicate_count == 1

    def test_multiple_duplicates_increment_progressively(
        self, repo: Repository
    ) -> None:
        event = _event("evt-1", "first")
        repo.upsert_event(event)
        repo.upsert_event(event)
        third = repo.upsert_event(event)
        assert third.status is StatusCode.DUPLICATE
        assert third.event.duplicate_count == 2

    def test_conflict_on_same_id_different_body(self, repo: Repository) -> None:
        repo.upsert_event(_event("evt-1", "first"))
        result = repo.upsert_event(_event("evt-1", "second"))
        assert result.status is StatusCode.CONFLICT
        assert result.event.body_raw == b"first"
        assert result.event.duplicate_count == 0

    def test_conflict_preserves_existing_row(self, repo: Repository) -> None:
        original = _event("evt-1", "first")
        repo.upsert_event(original)
        repo.upsert_event(_event("evt-1", "second"))
        stored = repo.get_event("evt-1")
        assert stored is not None
        assert stored.body_raw == b"first"
        assert stored.duplicate_count == 0

    def test_duplicate_uses_exact_byte_comparison(self, repo: Repository) -> None:
        repo.upsert_event(_event("evt-1", '{"a":1}'))
        result = repo.upsert_event(_event("evt-1", '{ "a": 1 }'))
        assert result.status is StatusCode.CONFLICT

    def test_stores_payload_json(self, repo: Repository) -> None:
        event = _event("evt-1", "first")
        repo.upsert_event(event)
        stored = repo.get_event("evt-1")
        assert stored is not None
        assert stored.payload == {"v": "first"}


class TestGetEvent:
    def test_returns_none_for_missing(self, repo: Repository) -> None:
        assert repo.get_event("missing") is None

    def test_returns_stored_event(self, repo: Repository) -> None:
        event = _event("evt-1", "first")
        repo.upsert_event(event)
        stored = repo.get_event("evt-1")
        assert stored is not None
        assert stored.event_id == "evt-1"
        assert stored.body_raw == b"first"
        assert stored.payload == {"v": "first"}
        assert stored.duplicate_count == 0

    def test_returns_updated_duplicate_count(self, repo: Repository) -> None:
        event = _event("evt-1", "first")
        repo.upsert_event(event)
        repo.upsert_event(event)
        stored = repo.get_event("evt-1")
        assert stored is not None
        assert stored.duplicate_count == 1


class TestConcurrentUpsert:
    def test_concurrent_same_id_writes_do_not_raise(self, tmp_path: Path) -> None:
        db_path = tmp_path / "concurrent.db"
        workers = 24
        timeout = 10.0
        barrier = threading.Barrier(workers, timeout=timeout)
        errors: list[Exception] = []
        setup_errors: list[Exception] = []
        lock = threading.Lock()
        created_count = {"n": 0}
        duplicated_count = {"n": 0}

        def worker() -> None:
            try:
                repo = new_repository(str(db_path))
            except Exception as exc:  # noqa: BLE001
                with lock:
                    setup_errors.append(exc)
                return
            try:
                barrier.wait(timeout=timeout)
            except threading.BrokenBarrierError as exc:
                with lock:
                    setup_errors.append(exc)
                return
            try:
                result = repo.upsert_event(_event("evt-concurrent", "payload"))
                with lock:
                    if result.status is StatusCode.CREATED:
                        created_count["n"] += 1
                    else:
                        duplicated_count["n"] += 1
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=timeout * 2)

        assert not setup_errors, setup_errors
        assert not errors, errors
        assert all(not t.is_alive() for t in threads)
        assert created_count["n"] == 1
        assert duplicated_count["n"] == workers - 1
        repo = new_repository(str(db_path))
        stored = repo.get_event("evt-concurrent")
        assert stored is not None
        assert stored.duplicate_count == workers - 1
