from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.config import DEFAULT_DATABASE_PATH, Settings, get_settings, load_settings
from src.errors import StartupError


def _clean_env() -> None:
    for key in ("WEBHOOK_SECRET", "DATABASE_PATH"):
        os.environ.pop(key, None)


class TestSettings:
    def test_default_database_path_matches_design(self) -> None:
        assert Path("./webhook_inbox.db") == DEFAULT_DATABASE_PATH
        settings = Settings(webhook_secret="ok", database_path=DEFAULT_DATABASE_PATH)
        assert settings.webhook_secret == "ok"
        assert settings.database_path == Path("./webhook_inbox.db")


class TestLoadSettings:
    def test_wraps_validation_error_as_startup_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_env()
        with pytest.raises(StartupError):
            load_settings()

    def test_successful_load(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clean_env()
        monkeypatch.setenv("WEBHOOK_SECRET", "ok-secret")
        settings = load_settings()
        assert settings.webhook_secret == "ok-secret"

    def test_uses_default_database_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clean_env()
        monkeypatch.setenv("WEBHOOK_SECRET", "ok-secret")
        settings = load_settings()
        assert settings.database_path == Path("./webhook_inbox.db")

    def test_custom_database_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clean_env()
        monkeypatch.setenv("WEBHOOK_SECRET", "ok-secret")
        monkeypatch.setenv("DATABASE_PATH", "/tmp/custom.db")
        settings = load_settings()
        assert settings.database_path == Path("/tmp/custom.db")

    def test_missing_webhook_secret_raises_startup_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_env()
        with pytest.raises(StartupError):
            load_settings()

    def test_empty_webhook_secret_raises_startup_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_env()
        monkeypatch.setenv("WEBHOOK_SECRET", "")
        with pytest.raises(StartupError):
            load_settings()

    def test_empty_database_path_uses_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_env()
        monkeypatch.setenv("WEBHOOK_SECRET", "ok-secret")
        monkeypatch.setenv("DATABASE_PATH", "")
        settings = load_settings()
        assert settings.database_path == Path("./webhook_inbox.db")


class TestGetSettings:
    def test_returns_cached_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clean_env()
        get_settings.cache_clear()
        monkeypatch.setenv("WEBHOOK_SECRET", "cached-secret")
        first = get_settings()
        second = get_settings()
        assert first is second
        assert first.webhook_secret == "cached-secret"

    def test_cache_clear_allows_reload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clean_env()
        get_settings.cache_clear()
        monkeypatch.setenv("WEBHOOK_SECRET", "first")
        assert get_settings().webhook_secret == "first"
        monkeypatch.setenv("WEBHOOK_SECRET", "second")
        get_settings.cache_clear()
        assert get_settings().webhook_secret == "second"
