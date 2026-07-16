from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

import pytest

from src import service as service_module
from src.domain import Event, EventResult, StatusCode
from src.errors import (
    AppError,
    ConflictError,
    ErrorCode,
    InvalidJsonError,
    MissingEventIdError,
    SignatureError,
)
from src.repository import new_repository
from src.service import Service


@pytest.fixture
def service(tmp_path: str) -> Service:
    db_path = f"{tmp_path}/inbox.db"
    return Service(webhook_secret="super-secret", repository=new_repository(db_path))


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestVerifySignature:
    def test_accepts_valid_signature(self, service: Service) -> None:
        body = b'{"ok":true}'
        service.verify_signature(_sign("super-secret", body), body)

    def test_rejects_missing_signature(self, service: Service) -> None:
        with pytest.raises(SignatureError) as exc:
            service.verify_signature(None, b"{}")
        assert exc.value.code is ErrorCode.MISSING_SIGNATURE

    def test_rejects_empty_signature(self, service: Service) -> None:
        with pytest.raises(SignatureError):
            service.verify_signature("", b"{}")

    def test_rejects_malformed_prefix(self, service: Service) -> None:
        with pytest.raises(SignatureError) as exc:
            service.verify_signature("not-a-signature", b"{}")
        assert exc.value.code is ErrorCode.INVALID_SIGNATURE

    def test_rejects_invalid_hex_value(self, service: Service) -> None:
        body = b'{"ok":true}'
        with pytest.raises(SignatureError) as exc:
            service.verify_signature("sha256=deadbeef", body)
        assert exc.value.code is ErrorCode.INVALID_SIGNATURE

    def test_rejects_wrong_signature(self, service: Service) -> None:
        body = b'{"ok":true}'
        wrong = "sha256=" + hashlib.sha256(b"nope").hexdigest()
        with pytest.raises(SignatureError):
            service.verify_signature(wrong, body)

    def test_constant_time_comparison_used(
        self, service: Service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = {"n": 0}
        original = hmac.compare_digest

        def spy(a: str, b: str) -> bool:
            called["n"] += 1
            return original(a, b)

        monkeypatch.setattr(hmac, "compare_digest", spy)
        body = b'{"ok":true}'
        service.verify_signature(_sign("super-secret", body), body)
        assert called["n"] == 1


class TestReceiveEvent:
    def test_accepts_valid_webhook(self, service: Service) -> None:
        body = b'{"type":"invoice.paid","amount":42}'
        result = service.receive_event(
            signature=_sign("super-secret", body),
            event_id="evt-1",
            body=body,
        )
        assert result.status is StatusCode.CREATED
        assert result.event.event_id == "evt-1"
        assert result.event.payload == {"type": "invoice.paid", "amount": 42}

    def test_stores_accepted_event(self, service: Service) -> None:
        body = b'{"ok":true}'
        service.receive_event(
            signature=_sign("super-secret", body),
            event_id="evt-1",
            body=body,
        )
        stored = service._repository.get_event("evt-1")
        assert stored is not None
        assert stored.body_raw == body

    def test_verifies_signature_before_parsing_json(self) -> None:
        calls: list[str] = []

        class FakeRepository:
            def upsert_event(self, event: Event) -> EventResult:
                calls.append("upsert")
                return EventResult(event=event, status=StatusCode.CREATED)

        svc = Service(
            webhook_secret="super-secret",
            repository=FakeRepository(),  # type: ignore[arg-type]
        )
        with pytest.raises(SignatureError):
            svc.receive_event(
                signature=None,
                event_id="evt-x",
                body=b"not-json",
            )
        assert "upsert" not in calls

    def test_rejects_missing_event_id(self, service: Service) -> None:
        body = b'{"ok":true}'
        with pytest.raises(MissingEventIdError):
            service.receive_event(
                signature=_sign("super-secret", body),
                event_id=None,
                body=body,
            )

    def test_rejects_empty_event_id(self, service: Service) -> None:
        body = b'{"ok":true}'
        with pytest.raises(MissingEventIdError):
            service.receive_event(
                signature=_sign("super-secret", body),
                event_id="",
                body=body,
            )

    def test_rejects_invalid_json(self, service: Service) -> None:
        body = b"{not-json"
        with pytest.raises(InvalidJsonError):
            service.receive_event(
                signature=_sign("super-secret", body),
                event_id="evt-bad",
                body=body,
            )

    def test_rejects_empty_body_as_invalid_json(self, service: Service) -> None:
        body = b""
        with pytest.raises(InvalidJsonError):
            service.receive_event(
                signature=_sign("super-secret", body),
                event_id="evt-empty",
                body=body,
            )

    def test_idempotent_duplicate(self, service: Service) -> None:
        body = b'{"kind":"same","n":1}'
        first = service.receive_event(
            signature=_sign("super-secret", body),
            event_id="evt-dup",
            body=body,
        )
        second = service.receive_event(
            signature=_sign("super-secret", body),
            event_id="evt-dup",
            body=body,
        )
        assert first.status is StatusCode.CREATED
        assert second.status is StatusCode.DUPLICATE
        assert second.event.duplicate_count == 1

    def test_conflict_for_different_body(self, service: Service) -> None:
        first = b'{"state":"first"}'
        second = b'{"state":"second"}'
        service.receive_event(
            signature=_sign("super-secret", first),
            event_id="evt-cf",
            body=first,
        )
        result = service.receive_event(
            signature=_sign("super-secret", second),
            event_id="evt-cf",
            body=second,
        )
        assert result.status is StatusCode.CONFLICT
        assert result.event.body_raw == first
        assert result.event.payload == {"state": "first"}

    def test_preserves_received_at_on_duplicate(self, service: Service) -> None:
        body = b'{"k":1}'
        first = service.receive_event(
            signature=_sign("super-secret", body),
            event_id="evt-dup",
            body=body,
        )
        second = service.receive_event(
            signature=_sign("super-secret", body),
            event_id="evt-dup",
            body=body,
        )
        assert first.event.received_at == second.event.received_at

    def test_generates_server_side_received_at(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: str
    ) -> None:
        fake_now = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(service_module, "now_utc", lambda: fake_now)
        db_path = f"{tmp_path}/received_at.db"
        svc = Service(
            webhook_secret="super-secret",
            repository=new_repository(db_path),
        )
        body = b'{"a":1}'
        result = svc.receive_event(
            signature=_sign("super-secret", body),
            event_id="evt-1",
            body=body,
        )
        assert result.event.received_at == fake_now


class TestErrorHierarchy:
    def test_all_service_errors_are_app_errors(self) -> None:
        for error in [
            SignatureError(ErrorCode.MISSING_SIGNATURE, "m"),
            SignatureError(ErrorCode.INVALID_SIGNATURE, "i"),
            MissingEventIdError(),
            InvalidJsonError(),
            ConflictError(),
        ]:
            assert isinstance(error, AppError)
            assert isinstance(error.code, ErrorCode)
