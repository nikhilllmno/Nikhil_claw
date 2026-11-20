"""Migration 0022: the connections table, the vault names of connections, and the two new
toolsets of the default profile (docs/architecture.md, Section 10.3)."""

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
]
CHANGED = ["core", "todo", "web"]


async def _set_toolsets(url: str) -> None:
    connection = await asyncpg.connect(url)
    try:
        for name, toolsets in (("profile-a", OLD), ("profile-b", CHANGED)):
            await connection.execute(
                "UPDATE agent_profiles SET toolsets = $1::jsonb WHERE name = $2",
                json.dumps(toolsets),
                name,
            )
    finally:
        await connection.close()


async def _toolsets(url: str) -> dict[str, list[str]]:
    connection = await asyncpg.connect(url)
    try:
        rows = await connection.fetch("SELECT name, toolsets::text AS t FROM agent_profiles")
    finally:
        await connection.close()
    return {row["name"]: json.loads(row["t"]) for row in rows}


async def _has_table(url: str) -> bool:
    connection = await asyncpg.connect(url)
    try:
        return bool(await connection.fetchval("SELECT to_regclass('public.connections')"))
    finally:
        await connection.close()


async def _put_secret(url: str, name: str) -> None:
    connection = await asyncpg.connect(url)
    try:
        org_id = await connection.fetchval("SELECT id FROM orgs LIMIT 1")
        await connection.execute(
            "INSERT INTO vault_secrets (org_id, name, key_id, wrapped_key, ciphertext)"
            " VALUES ($1, $2, 'k', 'w'::bytea, 'c'::bytea)",
            org_id,
            name,
        )
    finally:
        await connection.close()


def test_the_upgrade_adds_the_toolsets_and_the_downgrade_removes_them() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        config = alembic_config(url)
        command.downgrade(config, "0021")
        assert asyncio.run(_has_table(url)) is False
        asyncio.run(_set_toolsets(url))
        command.upgrade(config, "head")
        assert asyncio.run(_has_table(url)) is True
        after = asyncio.run(_toolsets(url))
        assert after["profile-a"] == [*OLD, "connections", "integrations"]
        assert after["profile-b"] == CHANGED, "a changed profile keeps its toolsets"
        # The vault takes the Composio key and the secret of a connection, nothing else.
        asyncio.run(_put_secret(url, "composio_api_key"))
        asyncio.run(_put_secret(url, f"connection:{uuid.uuid4()}"))
        with pytest.raises(asyncpg.CheckViolationError):
            asyncio.run(_put_secret(url, "connection:not-a-uuid"))
        command.downgrade(config, "0021")
        assert asyncio.run(_has_table(url)) is False
        assert asyncio.run(_toolsets(url))["profile-a"] == OLD
        command.upgrade(config, "head")
