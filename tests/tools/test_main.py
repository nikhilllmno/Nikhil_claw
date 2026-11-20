"""The ``python -m sama.tools`` entry point."""

from __future__ import annotations

import pytest

from sama.skills import PostgresSkillStore
from sama.tools.__main__ import build_app
from sama.tools.server import ToolServer


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


async def test_build_app_with_postgres_serves_the_skill_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    real = ToolServer

    def spy(**kwargs: object) -> ToolServer:
        seen.update(kwargs)
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("sama.tools.__main__.ToolServer", spy)
    monkeypatch.setenv("SAMA_RUN_TOKEN_KEY", "k" * 32)
    monkeypatch.setenv("SAMA_DATABASE_URL", "postgresql://u:p@localhost:1/none")
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    build_app("postgres")
    assert isinstance(seen["skills"], PostgresSkillStore)
    names = {tool.name for tool in await real(**seen).mcp().list_tools()}  # type: ignore[arg-type]
    assert {"skills_list", "skill_view", "skill_manage"} <= names
    seen.clear()
    build_app("memory")
    assert seen["skills"] is None
