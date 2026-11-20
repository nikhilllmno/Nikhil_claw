"""Migration 0015 on a database that has profiles and messages (docs/architecture.md,
Section 11.7)."""

from __future__ import annotations

import asyncio
import json

import asyncpg
from alembic import command

from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import seed

OLD = ["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions"]
WITHOUT_SESSIONS = ["core", "todo", "web", "clarify", "github", "memory", "skills"]
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
        for name, toolsets in (
            ("profile-a", OLD),
            ("profile-a2", WITHOUT_SESSIONS),
            ("profile-b", CUSTOM),
        ):
            await connection.execute(
                "UPDATE agent_profiles SET toolsets = $1::jsonb WHERE name = $2",
                json.dumps(toolsets),
                name,
            )
    finally:
        await connection.close()


async def _table(url: str) -> dict[str, object]:
    connection = await asyncpg.connect(url)
    try:
        return {
            "exists": await connection.fetchval(
                "SELECT to_regclass('public.attachments') IS NOT NULL"
            ),
            "rls": await connection.fetchval(
                "SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass('public.attachments')"
            ),
            "policies": await connection.fetchval(
                "SELECT count(*) FROM pg_policies WHERE tablename = 'attachments'"
            ),
        }
    finally:
        await connection.close()


async def _grants(url: str) -> set[tuple[str, str]]:
    connection = await asyncpg.connect(url)
    try:
        rows = await connection.fetch(
            "SELECT grantee, column_name FROM information_schema.column_privileges"
            " WHERE table_schema = 'public' AND table_name = 'attachments'"
            " AND grantee IN ('anon', 'authenticated', 'PUBLIC')"
        )
    finally:
        await connection.close()
    return {(row["grantee"], row["column_name"]) for row in rows}


async def _checks(url: str) -> list[str]:
    """Tries bad rows in a transaction that is rolled back. Gives the failed constraints."""
    connection = await asyncpg.connect(url)
    failed: list[str] = []
    try:
        row = await connection.fetchrow(
            "SELECT org_id, session_id, id FROM messages ORDER BY id LIMIT 1"
        )
        assert row is not None
        for label, mime, size, data in (
            ("mime", "image/svg+xml", 3, b"abc"),
            ("size", "image/png", 4, b"abc"),
            ("empty", "image/png", 0, b""),
            ("large", "image/png", 10 * 1024 * 1024 + 1, b"x" * (10 * 1024 * 1024 + 1)),
        ):
            try:
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO attachments (org_id, session_id, message_id, name, mime_type,"
                        " size_bytes, sha256, data) VALUES ($1, $2, $3, 'a', $4, $5, 'h', $6)",
                        row["org_id"],
                        row["session_id"],
                        row["id"],
                        mime,
                        size,
                        data,
                    )
            except asyncpg.CheckViolationError:
                failed.append(label)
    finally:
        await connection.close()
    return failed


def test_the_upgrade_adds_the_table_and_the_toolset() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        config = alembic_config(url)
        command.downgrade(config, "0014")
        assert asyncio.run(_table(url))["exists"] is False
        asyncio.run(_prepare(url))
        command.upgrade(config, "0015")
        names = ["profile-a", "profile-a2", "profile-b"]
        assert asyncio.run(_toolsets(url, names)) == [
            [*OLD, "media"],
            WITHOUT_SESSIONS,  # Not the default list: a person changed it.
            CUSTOM,
        ]
        assert asyncio.run(_table(url)) == {"exists": True, "rls": True, "policies": 1}
        grants = asyncio.run(_grants(url))
        assert {grantee for grantee, _ in grants} == {"authenticated"}
        assert "data" not in {column for _, column in grants}
        assert {"id", "org_id", "session_id", "name", "mime_type"} <= {c for _, c in grants}
        assert asyncio.run(_checks(url)) == ["mime", "size", "empty", "large"]
        command.downgrade(config, "0014")
        assert asyncio.run(_toolsets(url, names)) == [OLD, WITHOUT_SESSIONS, CUSTOM]
        assert asyncio.run(_table(url))["exists"] is False
