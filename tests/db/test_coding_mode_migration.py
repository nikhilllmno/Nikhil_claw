"""Migration 0042: coding mode (docs/architecture.md, Section 15.5)."""

from __future__ import annotations

import asyncio

import asyncpg
from alembic import command

from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import seed

TABLES = ("session_repos", "session_plans")


async def _fetch(url: str, query: str, *args: object) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


def _policies(url: str, table: str) -> list[str]:
    rows = asyncio.run(
        _fetch(url, "SELECT policyname FROM pg_policies WHERE tablename = $1", table)
    )
    return sorted(row["policyname"] for row in rows)


def _columns(url: str, table: str) -> set[str]:
    rows = asyncio.run(
        _fetch(
            url,
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public'"
            " AND table_name = $1",
            table,
        )
    )
    return {row["column_name"] for row in rows}


def test_the_upgrade_adds_the_tables_with_rls_and_the_downgrade_removes_them() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        assert "plan_mode" in _columns(url, "sessions")
        for table in TABLES:
            assert _policies(url, table) == [f"{table}_select"]
        config = alembic_config(url)
        command.downgrade(config, "0041")
        assert "plan_mode" not in _columns(url, "sessions")
        for table in TABLES:
            assert _columns(url, table) == set()
        command.upgrade(config, "head")
        for table in TABLES:
            assert _policies(url, table) == [f"{table}_select"]
