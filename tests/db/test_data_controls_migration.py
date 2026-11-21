"""Migration 0032: data controls (docs/architecture.md, Section 17.3)."""

from __future__ import annotations

import asyncio

import asyncpg
from alembic import command

from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import seed


async def _fetch(url: str, query: str, *args: object) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


def _rows(url: str, query: str, *args: object) -> list[asyncpg.Record]:
    return asyncio.run(_fetch(url, query, *args))


def _state(url: str) -> tuple[bool, bool, list[str]]:
    table = _rows(
        url,
        "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public'"
        " AND table_name = 'data_purges'",
    )
    column = _rows(
        url,
        "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'"
        " AND table_name = 'orgs' AND column_name = 'retention_days'",
    )
    policies = _rows(url, "SELECT policyname FROM pg_policies WHERE tablename = 'data_purges'")
    return bool(table), bool(column), sorted(row["policyname"] for row in policies)


def test_the_upgrade_adds_the_purges_and_the_setting_and_the_downgrade_removes_them() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        assert _state(url) == (True, True, ["data_purges_select"])
        config = alembic_config(url)
        command.downgrade(config, "0031")
        assert _state(url) == (False, False, [])
        command.upgrade(config, "head")
        assert _state(url) == (True, True, ["data_purges_select"])
