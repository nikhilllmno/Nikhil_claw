"""Migration 0026: scheduler, cron jobs, and delivery (docs/architecture.md, Section 12.1)."""

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
    "subagents",
    "connections",
    "integrations",
    "delegation",
    "tasks",
]
TABLES = ("jobs", "job_fires", "deliveries")


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


def _has_run_model(url: str) -> bool:
    rows = asyncio.run(
        _fetch(
            url,
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'"
            " AND table_name = 'runs' AND column_name = 'model'",
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
        command.downgrade(config, "0025")
        for table in TABLES:
            assert not _exists(url, table)
        assert not _has_run_model(url)
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
                json.dumps(["web", "subagents"]),
            )
        )
        command.upgrade(config, "0026")
        for table in TABLES:
            assert _exists(url, table)
        assert _has_run_model(url)
        toolsets = _toolsets(url)
        assert toolsets["profile-a"] == [*OLD, "cron"]
        assert toolsets["profile-a2"] == ["web", "subagents"], "a changed profile keeps its list"

        # Each table has row level security and one read policy.
        policies = asyncio.run(
            _fetch(
                url,
                "SELECT tablename, cmd FROM pg_policies WHERE schemaname = 'public'"
                " AND tablename = ANY($1::text[])",
                list(TABLES),
            )
        )
        assert {(row["tablename"], row["cmd"]) for row in policies} == {
            (table, "SELECT") for table in TABLES
        }
        forced = asyncio.run(
            _fetch(
                url,
                "SELECT relname FROM pg_class WHERE relrowsecurity AND relname = ANY($1::text[])",
                list(TABLES),
            )
        )
        assert len(forced) == len(TABLES)

        # A job with its fire and delivery goes with the downgrade; the toolset goes back.
        asyncio.run(
            _execute(
                url,
                "INSERT INTO jobs (org_id, workspace_id, owner_user_id, agent_profile_id, name)"
                " SELECT s.org_id, s.workspace_id, s.owner_user_id, s.agent_profile_id, 'job'"
                " FROM sessions s WHERE s.owner_user_id IS NOT NULL LIMIT 1",
            )
        )
        command.downgrade(config, "0025")
        for table in TABLES:
            assert not _exists(url, table)
        assert not _has_run_model(url)
        assert _toolsets(url)["profile-a"] == OLD
        assert _toolsets(url)["profile-a2"] == ["web", "subagents"]
        command.upgrade(config, "head")


def test_a_job_fires_once_for_one_scheduled_time() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        asyncio.run(
            _execute(
                url,
                "INSERT INTO job_fires (org_id, job_id, job_version, scheduled_for)"
                " SELECT org_id, id, 1, '2026-01-01T00:00:00Z' FROM jobs LIMIT 1",
            )
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            asyncio.run(
                _execute(
                    url,
                    "INSERT INTO job_fires (org_id, job_id, job_version, scheduled_for)"
                    " SELECT org_id, id, 1, '2026-01-01T00:00:00Z' FROM jobs LIMIT 1",
                )
            )
