"""Migration 0036: the cloud browser (docs/architecture.md, Section 14.1; roadmap M5.1)."""

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
]
CUSTOM = ["core", "web"]


async def _fetch(url: str, query: str, *args: object) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


def _exists(url: str) -> bool:
    [row] = asyncio.run(_fetch(url, "SELECT to_regclass('public.browser_contexts') IS NOT NULL"))
    return bool(row[0])


def _policies(url: str) -> list[str]:
    rows = asyncio.run(
        _fetch(url, "SELECT policyname FROM pg_policies WHERE tablename = 'browser_contexts'")
    )
    return sorted(row["policyname"] for row in rows)


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


async def _rejected(url: str) -> list[str]:
    """Tries rows that break a rule of the table, in transactions that are rolled back."""
    [row] = await _fetch(url, "SELECT org_id, user_id FROM browser_contexts ORDER BY id LIMIT 1")
    insert = (
        "INSERT INTO browser_contexts (org_id, user_id, target, provider_context_id)"
        " VALUES ($1, $2, $3, $4)"
    )
    connection = await asyncpg.connect(url)
    failed: list[str] = []
    try:
        for label, args in (
            ("duplicate", ("cloud", "ctx-2")),
            ("target", ("firefox", "ctx-3")),
            ("empty id", ("my_chrome", "")),
        ):
            try:
                async with connection.transaction():
                    await connection.execute(insert, row["org_id"], row["user_id"], *args)
            except (asyncpg.UniqueViolationError, asyncpg.CheckViolationError):
                failed.append(label)
    finally:
        await connection.close()
    return failed


def test_the_upgrade_adds_the_table_with_rls_and_the_toolset() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        assert _exists(url)
        assert _policies(url) == ["browser_contexts_select"]
        assert asyncio.run(_rejected(url)) == ["duplicate", "target", "empty id"]
        config = alembic_config(url)
        command.downgrade(config, "0035")
        assert not _exists(url)
        _set(url, "profile-a", OLD)
        _set(url, "profile-a2", CUSTOM)
        command.upgrade(config, "0036")
        assert _exists(url)
        assert _policies(url) == ["browser_contexts_select"]
        toolsets = _toolsets(url)
        assert toolsets["profile-a"] == [*OLD, "browser"]
        assert toolsets["profile-a2"] == CUSTOM  # a person changed it
        command.downgrade(config, "0035")
        assert _toolsets(url)["profile-a"] == OLD
        assert _toolsets(url)["profile-a2"] == CUSTOM
        command.upgrade(config, "head")
