"""Migration 0030: busy controls and handoff (docs/architecture.md, Sections 4.3 and 11.1)."""

from __future__ import annotations

import asyncio

import asyncpg
from alembic import command

from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import seed

TABLES = ("run_steers", "session_handoffs")


async def _fetch(url: str, query: str, *args: object) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(query, *args)
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


def _policies(url: str, table: str) -> list[str]:
    rows = asyncio.run(
        _fetch(url, "SELECT policyname FROM pg_policies WHERE tablename = $1", table)
    )
    return sorted(row["policyname"] for row in rows)


def test_the_upgrade_adds_the_tables_with_rls_and_the_downgrade_removes_them() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        for table in TABLES:
            assert _exists(url, table)
            assert _policies(url, table) == [f"{table}_select"]
        config = alembic_config(url)
        command.downgrade(config, "0029")
        for table in TABLES:
            assert not _exists(url, table)
        command.upgrade(config, "head")
        for table in TABLES:
            assert _exists(url, table)
            assert _policies(url, table) == [f"{table}_select"]
