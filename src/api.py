from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.domain import (
    DatabaseStatus,
    Event,
    EventResult,
    HealthResult,
    HealthStatus,
    StatusCode,
)
from src.errors import (
    AppError,
    BodyTooLargeError,
    ConflictError,
    ErrorCode,
    MissingEventIdError,
    NotFoundError,
    SignatureError,
)

logger = logging.getLogger(__name__)

MAX_CONTENT_LENGTH = 1_048_576


@runtime_checkable
class WebhookService(Protocol):
    """Service boundary that the HTTP adapter delegates to."""

    def receive_event(
        self, signature: str | None, event_id: str, raw_body: bytes
    ) -> EventResult: ...

    def get_event(self, event_id: str) -> Event | None: ...

    def check_health(self) -> HealthResult: ...


async def read_limited_body(
    request: Request, max_bytes: int = MAX_CONTENT_LENGTH
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise BodyTooLargeError()
        chunks.append(chunk)
    return b"".join(chunks)


_STATUS_FOR_ERROR: dict[ErrorCode, int] = {
    ErrorCode.MISSING_SIGNATURE: 401,
    ErrorCode.INVALID_SIGNATURE: 401,
    ErrorCode.MISSING_EVENT_ID: 400,
    ErrorCode.INVALID_JSON: 400,
    ErrorCode.BODY_TOO_LARGE: 413,
    ErrorCode.CONFLICT: 409,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.DB_UNHEALTHY: 503,
}


def _error_response(code: ErrorCode, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=_STATUS_FOR_ERROR[code],
        content={"error": code.value, "message": message},
    )


def _event_to_dict(event: Event, *, duplicate: bool = False) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "payload": event.payload,
        "received_at": event.received_at.isoformat(),
        "duplicate": duplicate,
    }


def create_app(service: WebhookService) -> FastAPI:
    """Build a FastAPI app backed by the given WebhookService."""
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return _error_response(exc.code, exc.message)

    @app.post("/webhooks")
    async def post_webhooks(request: Request) -> JSONResponse:
        raw_body = await read_limited_body(request)
        event_id = request.headers.get("x-event-id")
        if not event_id:
            raise MissingEventIdError()
        signature = request.headers.get("x-webhook-signature")
        result = service.receive_event(signature, event_id, raw_body)
        if result.status is StatusCode.CONFLICT:
            raise ConflictError()
        return JSONResponse(
            status_code=201 if result.status is StatusCode.CREATED else 200,
            content=_event_to_dict(
                result.event, duplicate=result.status is StatusCode.DUPLICATE
            ),
        )

    @app.get("/events/{event_id}")
    async def get_event(event_id: str) -> JSONResponse:
        event = service.get_event(event_id)
        if event is None:
            raise NotFoundError()
        return JSONResponse(content=_event_to_dict(event))

    @app.get("/health")
    async def health() -> JSONResponse:
        result = service.check_health()
        if result.status is HealthStatus.DEGRADED:
            return _error_response(ErrorCode.DB_UNHEALTHY, "Database is not reachable")
        return JSONResponse(
            content={"status": result.status.value, "database": result.database.value}
        )

    return app


class _StubService:
    """Placeholder used by the default uvicorn entry point.

    The real composition root wires a repository-backed service; this stub
    keeps the module importable without a DATABASE_PATH / WEBHOOK_SECRET.
    """

    def receive_event(
        self, signature: str | None, event_id: str, raw_body: bytes
    ) -> EventResult:
        raise SignatureError(
            ErrorCode.MISSING_SIGNATURE, "Missing X-Webhook-Signature header"
        )

    def get_event(self, event_id: str) -> Event | None:
        return None

    def check_health(self) -> HealthResult:
        return HealthResult(status=HealthStatus.OK, database=DatabaseStatus.OK)


app: FastAPI = create_app(_StubService())
