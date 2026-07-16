from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from src.domain import Event, StatusCode
from src.errors import ConflictError
from src.repository import Repository, new_repository


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
        with pytest.raises(ConflictError) as exc:
            repo.upsert_event(_event("evt-1", "second"))
        assert exc.value.code == ConflictError().code

    def test_conflict_preserves_existing_row(self, repo: Repository) -> None:
        original = _event("evt-1", "first")
        repo.upsert_event(original)
        with pytest.raises(ConflictError):
            repo.upsert_event(_event("evt-1", "second"))
        stored = repo.get_event("evt-1")
        assert stored is not None
        assert stored.body_raw == b"first"
        assert stored.duplicate_count == 0

    def test_duplicate_uses_exact_byte_comparison(self, repo: Repository) -> None:
        repo.upsert_event(_event("evt-1", '{"a":1}'))
        with pytest.raises(ConflictError):
            repo.upsert_event(_event("evt-1", '{ "a": 1 }'))

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
