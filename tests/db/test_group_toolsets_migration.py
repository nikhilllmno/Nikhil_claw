"""Migration 0017: the group toolsets of an agent profile (docs/architecture.md, Sections 10.2
and 11.3)."""

from __future__ import annotations

import asyncio
import json

import asyncpg
from alembic import command

from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import seed

DEFAULT = ["todo", "web", "clarify", "memory", "sessions", "media"]


async def _has_column(url: str) -> bool:
    connection = await asyncpg.connect(url)
    try:
        return bool(
            await connection.fetchval(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public'"
                " AND table_name = 'agent_profiles' AND column_name = 'group_toolsets'"
            )
        )
    finally:
        await connection.close()


async def _set_toolsets(url: str) -> None:
    """Gives three profiles different toolsets: all the default ones, a few, and none."""
    connection = await asyncpg.connect(url)
    try:
        for name, toolsets in (
            ("profile-a", ["core", "todo", "web", "clarify", "github", "memory", "skills"]),
            ("profile-a2", ["media", "web", "other"]),
            ("profile-b", []),
        ):
            await connection.execute(
                "UPDATE agent_profiles SET toolsets = $1::jsonb WHERE name = $2",
                json.dumps(toolsets),
                name,
            )
    finally:
        await connection.close()


async def _groups(url: str) -> dict[str, list[str]]:
    connection = await asyncpg.connect(url)
    try:
        rows = await connection.fetch(
            "SELECT name, group_toolsets::text AS groups FROM agent_profiles"
        )
    finally:
        await connection.close()
    return {row["name"]: json.loads(row["groups"]) for row in rows}


async def _new_profile_default(url: str) -> list[str]:
    connection = await asyncpg.connect(url)
    try:
        org_id = await connection.fetchval("SELECT org_id FROM agent_profiles LIMIT 1")
        groups = await connection.fetchval(
            "INSERT INTO agent_profiles (org_id, name, model) VALUES ($1, 'fresh', 'm')"
            " RETURNING group_toolsets::text",
            org_id,
        )
    finally:
        await connection.close()
    return list(json.loads(groups))


def test_the_upgrade_fills_the_group_toolsets_from_the_profile_toolsets() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        config = alembic_config(url)
        command.downgrade(config, "0016")
        assert asyncio.run(_has_column(url)) is False
        asyncio.run(_set_toolsets(url))
        command.upgrade(config, "0017")
        assert asyncio.run(_has_column(url)) is True
        groups = asyncio.run(_groups(url))
        # A profile with every default toolset gets the default list, in its order. A group
        # has no toolset that the profile does not have, and no toolset of the default list
        # that is not in the group list (core, github, skills).
        assert groups["profile-a"] == ["todo", "web", "clarify", "memory"]
        assert groups["profile-a2"] == ["web", "media"]
        assert groups["profile-b"] == []
        assert set(groups["profile-a"]) <= set(DEFAULT)
        # A profile that a user makes later has no group toolset: it fails closed.
        assert asyncio.run(_new_profile_default(url)) == []


def test_the_default_profiles_get_the_whole_default_list() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        config = alembic_config(url)
        command.downgrade(config, "0016")
        connection_toolsets = [
            "core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",
        ]  # fmt: skip

        async def _prepare() -> None:
            connection = await asyncpg.connect(url)
            try:
                await connection.execute(
                    "UPDATE agent_profiles SET toolsets = $1::jsonb",
                    json.dumps(connection_toolsets),
                )
            finally:
                await connection.close()

        asyncio.run(_prepare())
        command.upgrade(config, "0017")
        assert set(map(tuple, asyncio.run(_groups(url)).values())) == {tuple(DEFAULT)}
        command.downgrade(config, "0016")
        assert asyncio.run(_has_column(url)) is False
