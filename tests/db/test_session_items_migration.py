"""Migration 0024: the session items of the OpenAI runtime (docs/architecture.md, Section 5.3)."""

from __future__ import annotations

import asyncio

import asyncpg
from alembic import command

from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import seed

TABLE = "runtime_session_items"


async def _fetch(url: str, query: str, *args: object) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


def _exists(url: str) -> bool:
    rows = asyncio.run(
        _fetch(
            url,
            "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public'"
            " AND table_name = $1",
            TABLE,
        )
    )
    return bool(rows)


def test_the_migration_goes_down_and_up_with_items() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        assert _exists(url)
        rows = asyncio.run(_fetch(url, f"SELECT count(*) AS n FROM {TABLE}"))  # noqa: S608
        assert rows[0]["n"] > 0
        policies = asyncio.run(
            _fetch(url, "SELECT policyname FROM pg_policies WHERE tablename = $1", TABLE)
        )
        assert [row["policyname"] for row in policies] == [f"{TABLE}_select"]

        config = alembic_config(url)
        command.downgrade(config, "0023")
        assert not _exists(url)
        command.upgrade(config, "head")
        assert _exists(url)
