"""Migration 0019: native sub-agents (docs/architecture.md, Section 13.1)."""

from __future__ import annotations

import asyncio
import json

import asyncpg
import pytest
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
]


async def _fetch(url: str, query: str, *args: object) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


async def _execute(url: str, query: str, *args: object) -> None:
    connection = await asyncpg.connect(url)
    try:
        await connection.execute(query, *args)
    finally:
        await connection.close()


def _has_column(url: str) -> bool:
    rows = asyncio.run(
        _fetch(
            url,
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'"
            " AND table_name = 'agent_profiles' AND column_name = 'subagents'",
        )
    )
    return bool(rows)


def _toolsets(url: str) -> dict[str, list[str]]:
    rows = asyncio.run(_fetch(url, "SELECT name, toolsets::text AS t FROM agent_profiles"))
    return {row["name"]: json.loads(row["t"]) for row in rows}


def test_the_upgrade_adds_the_column_and_the_toolset_and_the_downgrade_removes_them() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        config = alembic_config(url)
        command.downgrade(config, "0018")
        assert not _has_column(url)
        # One profile has the exact default list. The others were changed by a person.
        asyncio.run(
            _execute(
                url,
                "UPDATE agent_profiles SET toolsets = $1::jsonb WHERE name = 'profile-a'",
                json.dumps(OLD),
            )
        )
        asyncio.run(
            _execute(
                url,
                "UPDATE agent_profiles SET toolsets = $1::jsonb WHERE name = 'profile-a2'",
                json.dumps(["web"]),
            )
        )
        command.upgrade(config, "0019")
        assert _has_column(url)
        toolsets = _toolsets(url)
        assert toolsets["profile-a"] == [*OLD, "subagents"]
        assert toolsets["profile-a2"] == ["web"], "a changed profile keeps its list"
        rows = asyncio.run(_fetch(url, "SELECT subagents::text AS s FROM agent_profiles"))
        assert {row["s"] for row in rows} == {"[]"}, "every profile starts with no sub-agent"

        command.downgrade(config, "0018")
        assert not _has_column(url)
        assert _toolsets(url)["profile-a"] == OLD
        command.upgrade(config, "head")


def test_the_column_takes_only_a_json_array() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        asyncio.run(_execute(url, "UPDATE agent_profiles SET subagents = '[]'::jsonb"))
        with pytest.raises(asyncpg.CheckViolationError):
            asyncio.run(_execute(url, "UPDATE agent_profiles SET subagents = '{}'::jsonb"))
