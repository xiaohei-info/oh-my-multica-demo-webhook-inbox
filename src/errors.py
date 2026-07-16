from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    MISSING_SIGNATURE = "missing_signature"
    INVALID_SIGNATURE = "invalid_signature"
    MISSING_EVENT_ID = "missing_event_id"
    INVALID_JSON = "invalid_json"
    BODY_TOO_LARGE = "body_too_large"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"
    DB_UNHEALTHY = "db_unhealthy"


class AppError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class StartupError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.DB_UNHEALTHY, message)


class SignatureError(AppError):
    def __init__(self, code: ErrorCode, message: str) -> None:
        if code not in (ErrorCode.MISSING_SIGNATURE, ErrorCode.INVALID_SIGNATURE):
            raise ValueError(
                f"SignatureError requires a signature error code, got {code}"
            )
        super().__init__(code, message)


class MissingEventIdError(AppError):
    def __init__(self, message: str = "Missing or empty X-Event-ID header") -> None:
        super().__init__(ErrorCode.MISSING_EVENT_ID, message)


class InvalidJsonError(AppError):
    def __init__(self, message: str = "Request body is not valid JSON") -> None:
        super().__init__(ErrorCode.INVALID_JSON, message)


class BodyTooLargeError(AppError):
    def __init__(
        self, message: str = "Request body exceeds maximum allowed size of 1 MiB"
    ) -> None:
        super().__init__(ErrorCode.BODY_TOO_LARGE, message)


class ConflictError(AppError):
    def __init__(
        self,
        message: str = "Event ID already exists with a different body",
    ) -> None:
        super().__init__(ErrorCode.CONFLICT, message)


class NotFoundError(AppError):
    def __init__(self, message: str = "Event not found") -> None:
        super().__init__(ErrorCode.NOT_FOUND, message)
