from __future__ import annotations

import hashlib
import hmac
import json
from typing import TYPE_CHECKING, Any

from src.domain import Event, EventResult, StatusCode, now_utc
from src.errors import (
    ConflictError,
    ErrorCode,
    InvalidJsonError,
    MissingEventIdError,
    SignatureError,
)

if TYPE_CHECKING:
    from src.repository import Repository


class Service:
    def __init__(self, webhook_secret: str, repository: Repository) -> None:
        self._secret = webhook_secret.encode("utf-8")
        self._repository = repository

    def verify_signature(self, signature: str | None, body: bytes) -> None:
        if not signature:
            raise SignatureError(
                ErrorCode.MISSING_SIGNATURE,
                "Missing X-Webhook-Signature header",
            )
        if not signature.startswith("sha256="):
            raise SignatureError(
                ErrorCode.INVALID_SIGNATURE,
                "Malformed X-Webhook-Signature header",
            )
        expected = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature[7:]):
            raise SignatureError(
                ErrorCode.INVALID_SIGNATURE,
                "Signature does not match",
            )

    def _parse_json(self, body: bytes) -> Any:
        try:
            return json.loads(body)
        except (ValueError, TypeError) as exc:
            raise InvalidJsonError() from exc

    def receive_event(
        self,
        signature: str | None,
        event_id: str | None,
        body: bytes,
    ) -> EventResult:
        self.verify_signature(signature, body)
        if not event_id:
            raise MissingEventIdError()
        payload = self._parse_json(body)
        event = Event(
            event_id=event_id,
            body_raw=body,
            payload=payload,
            received_at=now_utc(),
        )
        try:
            result = self._repository.upsert_event(event)
        except ConflictError as exc:
            raise ConflictError() from exc
        if result.status is StatusCode.CONFLICT:
            raise ConflictError()
        return result
