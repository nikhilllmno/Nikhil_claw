"""Migration 0040: Hermes tool parity (docs/architecture.md, Sections 8.2, 9.8, 10.2.2, and
13.2; roadmap M7.5)."""

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


def _exists(url: str) -> bool:
    [row] = asyncio.run(_fetch(url, "SELECT to_regclass('public.skill_settings') IS NOT NULL"))
    return bool(row[0])


def _policies(url: str) -> list[str]:
    rows = asyncio.run(
        _fetch(url, "SELECT policyname FROM pg_policies WHERE tablename = 'skill_settings'")
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


async def _checks(url: str) -> dict[str, bool]:
    """Which new values the constraints accept. Each try is rolled back."""
    [profile] = await _fetch(url, "SELECT id, org_id FROM agent_profiles ORDER BY id LIMIT 1")
    [workspace] = await _fetch(
        url, "SELECT id FROM workspaces WHERE org_id = $1 LIMIT 1", profile["org_id"]
    )
    org = profile["org_id"]
    tries = {
        "mem0 provider": (
            "UPDATE agent_profiles SET memory_provider = 'mem0' WHERE id = $1",
            (profile["id"],),
        ),
        "bad provider": (
            "UPDATE agent_profiles SET memory_provider = 'honcho' WHERE id = $1",
            (profile["id"],),
        ),
        "mem0 purge": (
            "INSERT INTO data_purges (org_id, kind, refs) VALUES ($1, 'mem0', '{}')",
            (org,),
        ),
        "gemini key": (
            "INSERT INTO vault_secrets (org_id, name, key_id, wrapped_key, ciphertext)"
            " VALUES ($1, 'gemini_api_key', 'k', 'w', 'c')",
            (org,),
        ),
        "settings": (
            "INSERT INTO skill_settings (org_id, workspace_id, skill_name, settings)"
            " VALUES ($1, $2, 'news', '{\"city\": \"Paris\"}')",
            (org, workspace["id"]),
        ),
        "settings array": (
            "INSERT INTO skill_settings (org_id, workspace_id, skill_name, settings)"
            " VALUES ($1, $2, 'other', '[]')",
            (org, workspace["id"]),
        ),
    }
    connection = await asyncpg.connect(url)
    accepted: dict[str, bool] = {}
    try:
        for label, (sql, args) in tries.items():
            transaction = connection.transaction()
            await transaction.start()
            try:
                await connection.execute(sql, *args)
                accepted[label] = True
            except asyncpg.CheckViolationError:
                accepted[label] = False
            finally:
                await transaction.rollback()
    finally:
        await connection.close()
    return accepted


def test_the_upgrade_adds_the_columns_the_table_and_the_toolset() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        assert _exists(url)
        assert _policies(url) == ["skill_settings_select"]
        assert asyncio.run(_checks(url)) == {
            "mem0 provider": True,
            "bad provider": False,
            "mem0 purge": True,
            "gemini key": True,
            "settings": True,
            "settings array": False,
        }
        config = alembic_config(url)
        command.downgrade(config, "0039")
        assert not _exists(url)
        _set(url, "profile-a", OLD)
        _set(url, "profile-a2", CUSTOM)
        command.upgrade(config, "0040")
        toolsets = _toolsets(url)
        assert toolsets["profile-a"] == [*OLD, "moa"]
        assert toolsets["profile-a2"] == CUSTOM  # a person changed it
        command.downgrade(config, "0039")
        assert _toolsets(url)["profile-a"] == OLD
        command.upgrade(config, "head")
