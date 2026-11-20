"""Migration 0023: delegation and the task board (docs/architecture.md, Sections 12.4 and 13.2)."""

from __future__ import annotations

import asyncio
import json
import uuid

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
]
OLD_GROUP = ["todo", "web", "clarify", "memory", "sessions", "media"]


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


def _tables(url: str) -> set[str]:
    rows = asyncio.run(
        _fetch(
            url,
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            " AND table_name IN ('tasks', 'task_comments')",
        )
    )
    return {row["table_name"] for row in rows}


def _has_parent_run(url: str) -> bool:
    rows = asyncio.run(
        _fetch(
            url,
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'"
            " AND table_name = 'sessions' AND column_name = 'parent_run_id'",
        )
    )
    return bool(rows)


def _toolsets(url: str, column: str = "toolsets") -> dict[str, list[str]]:
    query = f"SELECT name, {column}::text AS t FROM agent_profiles"  # noqa: S608 (a fixed name)
    rows = asyncio.run(_fetch(url, query))
    return {row["name"]: json.loads(row["t"]) for row in rows}


def test_the_upgrade_adds_the_tables_and_the_toolsets_and_the_downgrade_removes_them() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
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
        asyncio.run(
            _execute(
                url,
                "UPDATE agent_profiles SET group_toolsets = $1::jsonb WHERE name = 'profile-a'",
                json.dumps(OLD_GROUP),
            )
        )
        config = alembic_config(url)
        command.downgrade(config, "0022")
        assert _tables(url) == set()
        assert not _has_parent_run(url)
        assert _toolsets(url)["profile-a"] == OLD

        command.upgrade(config, "0023")
        assert _tables(url) == {"tasks", "task_comments"}
        assert _has_parent_run(url)
        toolsets = _toolsets(url)
        assert toolsets["profile-a"] == [*OLD, "delegation", "tasks"]
        assert toolsets["profile-a2"] == ["web"], "a changed profile keeps its list"
        assert _toolsets(url, "group_toolsets")["profile-a"] == [*OLD_GROUP, "tasks"]

        command.downgrade(config, "0022")
        assert _toolsets(url)["profile-a"] == OLD
        assert _toolsets(url, "group_toolsets")["profile-a"] == OLD_GROUP
        command.upgrade(config, "head")


def test_only_a_delegation_session_has_a_parent_run() -> None:
    with migrated_database() as url:
        world = asyncio.run(seed(url))
        ids = world.ids
        child = uuid.uuid4()
        insert = (
            "INSERT INTO sessions (id, org_id, workspace_id, agent_profile_id, audience,"
            " parent_session_id, parent_run_id, lineage)"
            " VALUES ($1, $2, $3, $4, 'shared', $5, $6, $7)"
        )
        values = (ids["org-a"], ids["a1"], ids["profile-a"], ids["a1-shared"])
        with pytest.raises(asyncpg.CheckViolationError):
            asyncio.run(_execute(url, insert, child, *values, ids["run:a1-shared"], "branch"))
        # The parent run must be a run of the parent session.
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            asyncio.run(_execute(url, insert, child, *values, ids["run:a1-alice"], "delegation"))
        asyncio.run(_execute(url, insert, child, *values, ids["run:a1-shared"], "delegation"))


def test_the_task_checks_hold() -> None:
    with migrated_database() as url:
        world = asyncio.run(seed(url))
        ids = world.ids
        insert = (
            "INSERT INTO tasks (org_id, workspace_id, title, audience, owner_user_id,"
            " assignee_profile_id, assignee_user_id, depends_on)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)"
        )
        base = [ids["org-a"], ids["a1"], "t", "shared", None, None, None, []]

        def attempt(**change: object) -> None:
            values = list(base)
            for key, value in change.items():
                values[["title", "audience", "owner", "profile", "user", "deps"].index(key) + 2] = (
                    value
                )
            asyncio.run(_execute(url, insert, *values))

        attempt()
        with pytest.raises(asyncpg.CheckViolationError):
            attempt(title="")
        with pytest.raises(asyncpg.CheckViolationError):
            attempt(audience="private")
        with pytest.raises(asyncpg.CheckViolationError):
            attempt(profile=ids["profile-a"], user=ids["alice"])
        with pytest.raises(asyncpg.CheckViolationError):
            attempt(deps=[uuid.uuid4() for _ in range(21)])
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            attempt(profile=ids["profile-b"])
