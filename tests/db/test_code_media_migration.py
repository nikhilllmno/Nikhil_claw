"""Migration 0027: code execution and generated media (docs/architecture.md, Sections 10.2.1 and
11.8; roadmap M4.8 and M4.9)."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

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
]
CUSTOM = ["core", "web"]


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


def _toolsets(url: str) -> dict[str, list[str]]:
    rows = asyncio.run(_fetch(url, "SELECT name, toolsets::text AS t FROM agent_profiles"))
    return {row["name"]: json.loads(row["t"]) for row in rows}


def _table(url: str) -> dict[str, object]:
    rows = asyncio.run(
        _fetch(
            url,
            "SELECT to_regclass('public.generated_media') IS NOT NULL AS exists,"
            " (SELECT relrowsecurity FROM pg_class"
            "  WHERE oid = to_regclass('public.generated_media')) AS rls,"
            " (SELECT count(*) FROM pg_policies WHERE tablename = 'generated_media') AS policies",
        )
    )
    return dict(rows[0])


def _grants(url: str) -> set[tuple[str, str]]:
    rows = asyncio.run(
        _fetch(
            url,
            "SELECT grantee, column_name FROM information_schema.column_privileges"
            " WHERE table_schema = 'public' AND table_name = 'generated_media'"
            " AND grantee IN ('anon', 'authenticated', 'PUBLIC')",
        )
    )
    return {(row["grantee"], row["column_name"]) for row in rows}


async def _run(url: str) -> asyncpg.Record:
    rows = await _fetch(url, "SELECT org_id, session_id, id FROM runs ORDER BY id LIMIT 1")
    return rows[0]


async def _bad_media(url: str) -> list[str]:
    """Tries bad rows in a transaction that is rolled back. Gives the failed constraints."""
    run = await _run(url)
    connection = await asyncpg.connect(url)
    failed: list[str] = []
    try:
        for label, kind, mime, size, data in (
            ("kind", "video", "image/png", 3, b"abc"),
            ("mime", "image", "image/jpeg", 3, b"abc"),
            ("size", "image", "image/png", 4, b"abc"),
            ("empty", "audio", "audio/ogg", 0, b""),
            ("large", "image", "image/png", 10 * 1024 * 1024 + 1, b"x" * (10 * 1024 * 1024 + 1)),
        ):
            try:
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO generated_media (org_id, session_id, run_id, kind,"
                        " mime_type, size_bytes, sha256, data)"
                        " VALUES ($1, $2, $3, $4, $5, $6, 'h', $7)",
                        run["org_id"],
                        run["session_id"],
                        run["id"],
                        kind,
                        mime,
                        size,
                        data,
                    )
            except asyncpg.CheckViolationError:
                failed.append(label)
    finally:
        await connection.close()
    return failed


async def _media_call(url: str, endpoint: str) -> None:
    rows = await _fetch(
        url,
        "SELECT r.org_id, r.session_id, r.id, s.workspace_id FROM runs r"
        " JOIN sessions s ON s.id = r.session_id ORDER BY r.id LIMIT 1",
    )
    run = rows[0]
    await _execute(
        url,
        "INSERT INTO model_calls (call_id, org_id, workspace_id, session_id, run_id, provider,"
        " model, endpoint, status_code, input_tokens, output_tokens, cost_usd, duration_ms)"
        " VALUES (gen_random_uuid(), $1, $2, $3, $4, 'openai', 'gpt-image-2.6-flare', $5, 200,"
        " 10, 10, $6, 5)",
        run["org_id"],
        run["workspace_id"],
        run["session_id"],
        run["id"],
        endpoint,
        Decimal("0.001"),
    )


def test_the_upgrade_adds_the_table_the_endpoints_and_the_toolset() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        config = alembic_config(url)
        command.downgrade(config, "0026")
        assert _table(url)["exists"] is False
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
                json.dumps(CUSTOM),
            )
        )
        command.upgrade(config, "0027")
        toolsets = _toolsets(url)
        assert toolsets["profile-a"] == [*OLD, "code_exec"]
        assert toolsets["profile-a2"] == CUSTOM  # a person changed it
        assert _table(url) == {"exists": True, "rls": True, "policies": 1}
        grants = _grants(url)
        assert {grantee for grantee, _ in grants} == {"authenticated"}
        assert "data" not in {column for _, column in grants}
        assert {"id", "org_id", "session_id", "run_id", "kind"} <= {c for _, c in grants}
        assert asyncio.run(_bad_media(url)) == ["kind", "mime", "size", "empty", "large"]
        asyncio.run(_media_call(url, "images"))
        asyncio.run(_media_call(url, "speech"))
        # The downgrade keeps the records of the media calls: they are money that was spent.
        command.downgrade(config, "0026")
        assert _toolsets(url)["profile-a"] == OLD
        assert _table(url)["exists"] is False
        rows = asyncio.run(
            _fetch(url, "SELECT count(*) AS n FROM model_calls WHERE endpoint = 'images'")
        )
        assert rows[0]["n"] == 1
        try:
            asyncio.run(_media_call(url, "images"))
        except asyncpg.CheckViolationError:
            pass
        else:
            raise AssertionError("the old check accepted a new media call")
        command.upgrade(config, "head")
