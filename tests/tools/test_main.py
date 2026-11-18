"""The ``python -m sama.tools`` entry point."""

from __future__ import annotations

import pytest

from sama.tools.__main__ import build_app


def test_build_app_needs_the_run_token_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAMA_RUN_TOKEN_KEY", raising=False)
    with pytest.raises(SystemExit, match="SAMA_RUN_TOKEN_KEY"):
        build_app("memory")


def test_build_app_with_postgres_needs_the_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAMA_RUN_TOKEN_KEY", "k" * 32)
    monkeypatch.delenv("SAMA_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit, match="SAMA_DATABASE_URL"):
        build_app("postgres")


def test_build_app_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAMA_RUN_TOKEN_KEY", "k" * 32)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    assert build_app("memory") is not None
