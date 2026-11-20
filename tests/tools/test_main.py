"""The ``python -m sama.tools`` entry point."""

from __future__ import annotations

import pytest

from sama.media.vision import VisionService
from sama.search import OpenAIEmbedder, SessionSearch
from sama.skills import PostgresSkillStore
from sama.tools.__main__ import build_app
from sama.tools.server import ToolServer
from sama.vault.redact import default_redactor


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


async def test_build_app_with_postgres_serves_the_session_tools_and_a_redacting_embedder(
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
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.delenv("SAMA_VAULT_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    build_app("postgres")
    sessions = seen["sessions"]
    assert isinstance(sessions, SessionSearch)
    assert isinstance(sessions._embedder, OpenAIEmbedder)
    assert sessions._embedder._redactor is default_redactor()
    names = {tool.name for tool in await real(**seen).mcp().list_tools()}  # type: ignore[arg-type]
    assert {"session_search", "session_read", "skills_list"} <= names
    # Without any key, the search has no embedder. The tools stay.
    monkeypatch.delenv("OPENAI_API_KEY")
    seen.clear()
    build_app("postgres")
    assert isinstance(seen["sessions"], SessionSearch)
    assert seen["sessions"]._embedder is None
    seen.clear()
    build_app("memory")
    assert seen["sessions"] is None


async def test_build_app_with_postgres_serves_vision_and_hides_it_in_memory_mode(
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
    monkeypatch.delenv("SAMA_VAULT_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    build_app("postgres")
    assert isinstance(seen["vision"], VisionService)
    names = {tool.name for tool in await real(**seen).mcp().list_tools()}  # type: ignore[arg-type]
    assert "vision_analyze" in names
    seen.clear()
    build_app("memory")
    assert seen["vision"] is None
