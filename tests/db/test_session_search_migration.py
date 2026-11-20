"""Migration 0014 on a database that has messages and profiles (docs/architecture.md,
Section 7.1)."""

from __future__ import annotations

import asyncio
import json

import asyncpg
from alembic import command

from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import seed

OLD = ["core", "todo", "web", "clarify", "github", "memory", "skills"]
WITHOUT_SKILLS = ["core", "todo", "web", "clarify", "github", "memory"]
CUSTOM = ["core", "memory"]


async def _toolsets(url: str, names: list[str]) -> list[list[str]]:
    connection = await asyncpg.connect(url)
    try:
        rows = await connection.fetch(
            "SELECT toolsets::text AS toolsets FROM agent_profiles WHERE name = ANY($1::text[])"
            " ORDER BY name",
            names,
        )
    finally:
        await connection.close()
    return [json.loads(row["toolsets"]) for row in rows]


async def _prepare(url: str) -> None:
    connection = await asyncpg.connect(url)
    try:
        await connection.execute(
            "UPDATE agent_profiles SET toolsets = $1::jsonb WHERE name = 'profile-a'",
            json.dumps(OLD),
        )
        await connection.execute(
            "UPDATE agent_profiles SET toolsets = $1::jsonb WHERE name = 'profile-a2'",
            json.dumps(WITHOUT_SKILLS),
        )
        await connection.execute(
            "UPDATE agent_profiles SET toolsets = $1::jsonb WHERE name = 'profile-b'",
            json.dumps(CUSTOM),
        )
    finally:
        await connection.close()


async def _messages(url: str) -> list[tuple[str, bool]]:
    connection = await asyncpg.connect(url)
    try:
        rows = await connection.fetch(
            "SELECT search_vector::text AS vector, embedded_at IS NULL AS pending FROM messages"
        )
    finally:
        await connection.close()
    return [(row["vector"], row["pending"]) for row in rows]


def test_the_upgrade_indexes_old_messages_and_adds_the_toolset() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        config = alembic_config(url)
        command.downgrade(config, "0013")
        asyncio.run(_prepare(url))
        command.upgrade(config, "0014")
        names = ["profile-a", "profile-a2", "profile-b"]
        assert asyncio.run(_toolsets(url, names)) == [
            [*OLD, "sessions"],
            WITHOUT_SKILLS,  # Not the default list: a person changed it.
            CUSTOM,
        ]
        # The generated column indexes the messages that exist. They wait for the embed worker.
        messages = asyncio.run(_messages(url))
        assert messages
        assert all("'secret'" in vector and pending for vector, pending in messages)
        command.downgrade(config, "0013")
        assert asyncio.run(_toolsets(url, names)) == [OLD, WITHOUT_SKILLS, CUSTOM]
