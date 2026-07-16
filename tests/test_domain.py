from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.domain import (
    DatabaseStatus,
    Event,
    EventResult,
    HealthResult,
    HealthStatus,
    StatusCode,
    now_utc,
)
from src.errors import (
    AppError,
    BodyTooLargeError,
    ConflictError,
    ErrorCode,
    InvalidJsonError,
    MissingEventIdError,
    NotFoundError,
    SignatureError,
    StartupError,
)


class TestEvent:
    def test_creates_valid_event(self) -> None:
        received = now_utc()
        event = Event(
            event_id="evt-1",
            body_raw=b'{"ok":true}',
            payload={"ok": True},
            received_at=received,
        )
        assert event.event_id == "evt-1"
        assert event.body_raw == b'{"ok":true}'
        assert event.payload == {"ok": True}
        assert event.received_at == received
        assert event.duplicate is False

    def test_empty_event_id_raises(self) -> None:
        with pytest.raises(ValueError):
            Event(event_id="", body_raw=b"x", payload=None, received_at=now_utc())

    def test_non_bytes_body_raises(self) -> None:
        with pytest.raises(TypeError):
            Event(
                event_id="evt-1",
                body_raw="not bytes",  # type: ignore[arg-type]
                payload=None,
                received_at=now_utc(),
            )

    def test_non_datetime_received_at_raises(self) -> None:
        with pytest.raises(TypeError):
            Event(
                event_id="evt-1",
                body_raw=b"x",
                payload=None,
                received_at="not-a-datetime",  # type: ignore[arg-type]
            )

    def test_duplicate_flag_defaults_to_false(self) -> None:
        event = Event(
            event_id="evt-1",
            body_raw=b"{}",
            payload={},
            received_at=now_utc(),
        )
        assert event.duplicate is False

    def test_event_is_immutable(self) -> None:
        event = Event(
            event_id="evt-1",
            body_raw=b"{}",
            payload={},
            received_at=now_utc(),
        )
        with pytest.raises(AttributeError):
            event.event_id = "other"  # type: ignore[misc]

    def test_slots_are_present(self) -> None:
        event = Event(
            event_id="evt-1",
            body_raw=b"{}",
            payload={},
            received_at=now_utc(),
        )
        assert not hasattr(event, "__dict__")


class TestEventResult:
    def test_wraps_event_with_status(self) -> None:
        event = Event(
            event_id="evt-1",
            body_raw=b"{}",
            payload={},
            received_at=now_utc(),
        )
        result = EventResult(event=event, status=StatusCode.CREATED)
        assert result.event is event
        assert result.status is StatusCode.CREATED


class TestHealthResult:
    def test_ok_result(self) -> None:
        health = HealthResult(status=HealthStatus.OK, database=DatabaseStatus.OK)
        assert health.status == HealthStatus.OK
        assert health.database == DatabaseStatus.OK

    def test_degraded_result(self) -> None:
        health = HealthResult(
            status=HealthStatus.DEGRADED, database=DatabaseStatus.ERROR
        )
        assert health.status == HealthStatus.DEGRADED
        assert health.database == DatabaseStatus.ERROR


class TestEnums:
    def test_status_code_values(self) -> None:
        assert StatusCode.CREATED == "created"
        assert StatusCode.DUPLICATE == "duplicate"
        assert StatusCode.CONFLICT == "conflict"

    def test_health_status_values(self) -> None:
        assert HealthStatus.OK == "ok"
        assert HealthStatus.DEGRADED == "degraded"

    def test_database_status_values(self) -> None:
        assert DatabaseStatus.OK == "ok"
        assert DatabaseStatus.ERROR == "error"


class TestNowUtc:
    def test_returns_timezone_aware_datetime(self) -> None:
        value = now_utc()
        assert isinstance(value, datetime)
        assert value.tzinfo == timezone.utc


class TestErrorHierarchy:
    def test_app_error_exposes_code_and_message(self) -> None:
        error = AppError(ErrorCode.CONFLICT, "boom")
        assert error.code is ErrorCode.CONFLICT
        assert error.message == "boom"
        assert str(error) == "boom"

    def test_startup_error_uses_db_unhealthy_code(self) -> None:
        error = StartupError("bad config")
        assert error.code is ErrorCode.DB_UNHEALTHY

    def test_signature_error_rejects_non_signature_codes(self) -> None:
        with pytest.raises(ValueError):
            SignatureError(ErrorCode.CONFLICT, "nope")

    def test_signature_error_accepts_signature_codes(self) -> None:
        missing = SignatureError(ErrorCode.MISSING_SIGNATURE, "m")
        invalid = SignatureError(ErrorCode.INVALID_SIGNATURE, "i")
        assert missing.code is ErrorCode.MISSING_SIGNATURE
        assert invalid.code is ErrorCode.INVALID_SIGNATURE

    def test_missing_event_id_error(self) -> None:
        error = MissingEventIdError()
        assert error.code is ErrorCode.MISSING_EVENT_ID

    def test_invalid_json_error(self) -> None:
        error = InvalidJsonError()
        assert error.code is ErrorCode.INVALID_JSON

    def test_body_too_large_error(self) -> None:
        error = BodyTooLargeError()
        assert error.code is ErrorCode.BODY_TOO_LARGE

    def test_conflict_error(self) -> None:
        error = ConflictError()
        assert error.code is ErrorCode.CONFLICT

    def test_not_found_error(self) -> None:
        error = NotFoundError()
        assert error.code is ErrorCode.NOT_FOUND
