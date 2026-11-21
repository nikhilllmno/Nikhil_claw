"""Migration 0041: the coding agent runtimes (docs/architecture.md, Section 15.4; roadmap M7.2)."""

from __future__ import annotations

import asyncio
import json

import asyncpg
from alembic import command

from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import seed

OLD = [
    "core",
    "todo",
    "web",
    "clarify",
    "github",
    "memory",
    "skills",
    "sessions",
    "media",
    "tool_search",
    "subagents",
    "connections",
    "integrations",
    "delegation",
    "tasks",
    "cron",
    "code_exec",
    "workflows",
    "browser",
]
CUSTOM = ["core", "web"]


async def _fetch(url: str, query: str, *args: object) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


def _toolsets(url: str) -> dict[str, list[str]]:
    rows = asyncio.run(_fetch(url, "SELECT name, toolsets::text AS t FROM agent_profiles"))
    return {row["name"]: json.loads(row["t"]) for row in rows}


def _set(url: str, name: str, toolsets: list[str]) -> None:
    asyncio.run(
        _fetch(
            url,
            "UPDATE agent_profiles SET toolsets = $1::jsonb WHERE name = $2",
            json.dumps(toolsets),
            name,
        )
    )


def _columns(url: str) -> set[str]:
    rows = asyncio.run(
        _fetch(
            url,
            "SELECT table_name FROM information_schema.columns WHERE table_schema = 'public'"
            " AND column_name = 'coding_agent'",
        )
    )
    return {row["table_name"] for row in rows}


async def _rejected(url: str) -> list[str]:
    """Tries values that the checks refuse, in transactions that are rolled back."""
    connection = await asyncpg.connect(url)
    failed: list[str] = []
    try:
        for label, query in (
            ("session agent", "UPDATE sessions SET coding_agent = 'cursor'"),
            ("run agent", "UPDATE runs SET coding_agent = 'cursor'"),
            ("runtime", "UPDATE agent_profiles SET runtime = 'cursor'"),
        ):
            try:
                async with connection.transaction():
                    await connection.execute(query)
            except asyncpg.CheckViolationError:
                failed.append(label)
        # The new values pass. The transaction is rolled back.
        transaction = connection.transaction()
        await transaction.start()
        await connection.execute("UPDATE agent_profiles SET runtime = 'codex'")
        await connection.execute("UPDATE sessions SET coding_agent = 'claude_code'")
        await connection.execute("UPDATE runs SET coding_agent = 'codex', runtime = 'codex'")
        await transaction.rollback()
    finally:
        await connection.close()
    return failed


def test_the_upgrade_adds_the_runtimes_the_columns_and_the_toolset() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        assert _columns(url) == {"sessions", "runs"}
        assert asyncio.run(_rejected(url)) == ["session agent", "run agent", "runtime"]
        config = alembic_config(url)
        command.downgrade(config, "0040")
        assert _columns(url) == set()
        _set(url, "profile-a", OLD)
        _set(url, "profile-a2", CUSTOM)
        command.upgrade(config, "0041")
        toolsets = _toolsets(url)
        assert toolsets["profile-a"] == [*OLD, "coding_agents"]
        assert toolsets["profile-a2"] == CUSTOM  # a person changed it
        asyncio.run(_fetch(url, "UPDATE agent_profiles SET runtime = 'claude_code'"))
        command.downgrade(config, "0040")
        assert _toolsets(url)["profile-a"] == OLD
        rows = asyncio.run(_fetch(url, "SELECT DISTINCT runtime FROM agent_profiles"))
        assert {row["runtime"] for row in rows} == {"claude_agent_sdk"}
        command.upgrade(config, "head")
