from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from src.errors import StartupError

DEFAULT_DATABASE_PATH = Path("./webhook_inbox.db")
MAX_CONTENT_LENGTH = 1024 * 1024


class Settings:
    def __init__(self, webhook_secret: str, database_path: Path) -> None:
        self.webhook_secret = webhook_secret
        self.database_path = database_path


def _read_settings() -> Settings:
    secret = os.environ.get("WEBHOOK_SECRET")
    if not secret:
        raise StartupError(
            "Invalid startup configuration: WEBHOOK_SECRET is missing or empty"
        )
    raw_path = os.environ.get("DATABASE_PATH")
    database_path = (
        Path(raw_path) if raw_path and raw_path.strip() else DEFAULT_DATABASE_PATH
    )
    return Settings(webhook_secret=secret, database_path=database_path)


@lru_cache
def get_settings() -> Settings:
    return load_settings()


def load_settings() -> Settings:
    try:
        return _read_settings()
    except StartupError:
        raise
    except Exception as exc:
        raise StartupError(f"Invalid startup configuration: {exc}") from exc
