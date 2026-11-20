"""Migration 0018: context management (docs/architecture.md, Section 6.3)."""

from __future__ import annotations

import asyncio
import json

import asyncpg
from alembic import command

from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import seed

OLD = ["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media"]


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


def _exists(url: str, table: str) -> bool:
    rows = asyncio.run(
        _fetch(
            url,
            "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public'"
            " AND table_name = $1",
            table,
        )
    )
    return bool(rows)


def _has_column(url: str, column: str) -> bool:
    rows = asyncio.run(
        _fetch(
            url,
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'"
            " AND table_name = 'runs' AND column_name = $1",
            column,
        )
    )
    return bool(rows)


def _toolsets(url: str) -> dict[str, list[str]]:
    rows = asyncio.run(_fetch(url, "SELECT name, toolsets::text AS t FROM agent_profiles"))
    return {row["name"]: json.loads(row["t"]) for row in rows}


def test_the_upgrade_adds_the_tables_and_the_toolset_and_the_downgrade_removes_them() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        config = alembic_config(url)
        command.downgrade(config, "0017")
        assert not _exists(url, "session_compactions")
        assert not _exists(url, "context_pins")
        assert not _has_column(url, "compact")
        assert not _has_column(url, "compact_focus")
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
                json.dumps(["web", "media"]),
            )
        )
        command.upgrade(config, "0018")
        assert _exists(url, "session_compactions")
        assert _exists(url, "context_pins")
        assert _has_column(url, "compact")
        toolsets = _toolsets(url)
        assert toolsets["profile-a"] == [*OLD, "tool_search"]
        assert toolsets["profile-a2"] == ["web", "media"], "a changed profile keeps its list"

        # The tables have row level security and the policies of the sessions.
        policies = asyncio.run(
            _fetch(
                url,
                "SELECT tablename FROM pg_policies WHERE schemaname = 'public'"
                " AND tablename IN ('session_compactions', 'context_pins')",
            )
        )
        assert {row["tablename"] for row in policies} == {"session_compactions", "context_pins"}
        forced = asyncio.run(
            _fetch(
                url,
                "SELECT relname FROM pg_class WHERE relrowsecurity"
                " AND relname IN ('session_compactions', 'context_pins')",
            )
        )
        assert len(forced) == 2

        # A compact run that is open when the schema goes back is cancelled, not left behind.
        asyncio.run(
            _execute(
                url,
                "INSERT INTO runs (org_id, session_id, agent_profile_id, runtime, compact)"
                " SELECT org_id, id, agent_profile_id, 'fake', true FROM sessions LIMIT 1",
            )
        )
        command.downgrade(config, "0017")
        cancelled = asyncio.run(
            _fetch(
                url,
                "SELECT status FROM runs WHERE failure_message LIKE 'the compact run was removed%'",
            )
        )
        assert [row["status"] for row in cancelled] == ["cancelled"]
        assert _toolsets(url)["profile-a"] == OLD
        command.upgrade(config, "head")
