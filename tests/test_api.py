from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi.testclient import TestClient

from src.api import create_app
from src.domain import (
    DatabaseStatus,
    Event,
    EventResult,
    HealthResult,
    HealthStatus,
    StatusCode,
)
from src.errors import (
    ErrorCode,
    SignatureError,
)

RECEIVED_AT = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def _event(event_id: str, body: bytes, payload: Any) -> Event:
    return Event(
        event_id=event_id,
        body_raw=body,
        payload=payload,
        received_at=RECEIVED_AT,
    )


class ReceivingService:
    """Service fake covering signature + receive + get + health."""

    def __init__(
        self,
        *,
        event: Event | None = None,
        status: StatusCode = StatusCode.CREATED,
        store: dict[str, Event] | None = None,
        health: HealthResult | None = None,
        raise_invalid_signature: bool = False,
    ) -> None:
        self._event = event or _event("evt-1", b'{"ok":1}', {"ok": 1})
        self._status = status
        self._store: dict[str, Event] = store if store is not None else {}
        self._health = health or HealthResult(
            status=HealthStatus.OK, database=DatabaseStatus.OK
        )
        self._raise_invalid_signature = raise_invalid_signature

    def receive_event(
        self, event_id: str, raw_body: bytes, signature_header: str | None
    ) -> EventResult:
        if not signature_header:
            raise SignatureError(
                ErrorCode.MISSING_SIGNATURE, "Missing X-Webhook-Signature header"
            )
        if self._raise_invalid_signature:
            raise SignatureError(
                ErrorCode.INVALID_SIGNATURE, "Invalid X-Webhook-Signature"
            )
        if event_id in self._store:
            return EventResult(event=self._store[event_id], status=StatusCode.DUPLICATE)
        return EventResult(event=self._event, status=self._status)

    def get_event(self, event_id: str) -> Event | None:
        return self._store.get(event_id)

    def check_health(self) -> HealthResult:
        return self._health


def _client(service: Any) -> TestClient:
    return TestClient(create_app(service), raise_server_exceptions=False)


class TestRejectMissingSignature:
    def test_missing_signature_returns_401(self) -> None:
        client = _client(ReceivingService())
        response = client.post(
            "/webhooks",
            content=b'{"ok":true}',
            headers={"x-event-id": "evt-1", "content-type": "application/json"},
        )
        assert response.status_code == 401
        assert response.json() == {
            "error": "missing_signature",
            "message": "Missing X-Webhook-Signature header",
        }


class TestRejectInvalidSignature:
    def test_invalid_signature_returns_401(self) -> None:
        svc = ReceivingService(raise_invalid_signature=True)
        client = _client(svc)
        response = client.post(
            "/webhooks",
            content=b'{"ok":true}',
            headers={
                "x-event-id": "evt-1",
                "x-webhook-signature": "sha256=deadbeef",
                "content-type": "application/json",
            },
        )
        assert response.status_code == 401
        assert response.json() == {
            "error": "invalid_signature",
            "message": "Invalid X-Webhook-Signature",
        }


class TestRejectMissingEventId:
    def test_missing_event_id_returns_400(self) -> None:
        client = _client(ReceivingService())
        response = client.post(
            "/webhooks",
            content=b'{"ok":true}',
            headers={
                "x-webhook-signature": "sha256=whatever",
                "content-type": "application/json",
            },
        )
        assert response.status_code == 400
        assert response.json() == {
            "error": "missing_event_id",
            "message": "Missing or empty X-Event-ID header",
        }


class TestBodyTooLarge:
    def test_body_over_limit_returns_413_before_service(self) -> None:
        service = ReceivingService()
        client = _client(service)
        oversize = b"{" + b'"x":"' + (b"y" * (1_048_576)) + b'"}'
        response = client.post(
            "/webhooks",
            content=oversize,
            headers={
                "x-event-id": "evt-big",
                "x-webhook-signature": "sha256=a",
                "content-type": "application/json",
            },
        )
        assert response.status_code == 413
        assert response.json() == {
            "error": "body_too_large",
            "message": "Request body exceeds maximum allowed size of 1 MiB",
        }


class TestGetEvent:
    def test_get_existing_event_returns_200(self) -> None:
        event = _event("evt-1", b'{"ok":1}', {"ok": 1})
        svc = ReceivingService(store={"evt-1": event}, event=event)
        client = _client(svc)
        response = client.get("/events/evt-1")
        assert response.status_code == 200
        body = response.json()
        assert body["event_id"] == "evt-1"
        assert body["payload"] == {"ok": 1}
        assert body["received_at"] == RECEIVED_AT.isoformat()

    def test_get_unknown_event_returns_404(self) -> None:
        client = _client(ReceivingService(store={}))
        response = client.get("/events/evt-missing")
        assert response.status_code == 404
        assert response.json() == {
            "error": "not_found",
            "message": "Event not found",
        }


class TestHealth:
    def test_healthy_returns_200(self) -> None:
        client = _client(ReceivingService())
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["database"] == "ok"
        assert "error" not in body

    def test_db_unhealthy_returns_503(self) -> None:
        svc = ReceivingService(
            health=HealthResult(
                status=HealthStatus.DEGRADED, database=DatabaseStatus.ERROR
            )
        )
        client = _client(svc)
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json() == {
            "error": "db_unhealthy",
            "message": "Database is not reachable",
        }


class TestReceivePaths:
    def test_created_returned_as_201(self) -> None:
        client = _client(ReceivingService(status=StatusCode.CREATED))
        response = client.post(
            "/webhooks",
            content=b'{"ok":true}',
            headers={
                "x-event-id": "evt-1",
                "x-webhook-signature": "sha256=abc",
                "content-type": "application/json",
            },
        )
        assert response.status_code == 201
        assert response.json()["duplicate"] is False

    def test_duplicate_returned_as_200_with_duplicate_flag(self) -> None:
        client = _client(ReceivingService(status=StatusCode.DUPLICATE))
        response = client.post(
            "/webhooks",
            content=b'{"ok":true}',
            headers={
                "x-event-id": "evt-1",
                "x-webhook-signature": "sha256=abc",
                "content-type": "application/json",
            },
        )
        assert response.status_code == 200
        assert response.json()["duplicate"] is True
