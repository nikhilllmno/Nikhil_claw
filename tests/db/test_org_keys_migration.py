"""Migration 0033: org keys and regions (docs/architecture.md, Sections 16.6 and 17.3)."""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
from alembic import command

from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import seed


async def _fetch(url: str, query: str, *args: object) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


async def _execute(url: str, query: str) -> None:
    connection = await asyncpg.connect(url)
    try:
        await connection.execute(query)
    finally:
        await connection.close()


def _column(url: str) -> list[asyncpg.Record]:
    return asyncio.run(
        _fetch(
            url,
            "SELECT column_default, is_nullable FROM information_schema.columns"
            " WHERE table_name = 'model_calls' AND column_name = 'key_source'",
        )
    )


def _checks(url: str) -> set[str]:
    rows = asyncio.run(
        _fetch(
            url,
            "SELECT conname FROM pg_constraint WHERE conname IN"
            " ('ck_model_calls_key_source', 'ck_orgs_region')",
        )
    )
    return {row["conname"] for row in rows}


def test_the_upgrade_adds_the_column_and_the_checks_and_the_downgrade_removes_them() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        [column] = _column(url)
        assert (column["column_default"], column["is_nullable"]) == ("'sama'::text", "NO")
        assert _checks(url) == {"ck_model_calls_key_source", "ck_orgs_region"}
        with pytest.raises(asyncpg.CheckViolationError):
            asyncio.run(_execute(url, "UPDATE orgs SET region = 'Mars'"))
        config = alembic_config(url)
        command.downgrade(config, "0032")
        assert _column(url) == []
        assert _checks(url) == set()
        command.upgrade(config, "head")
        assert len(_column(url)) == 1
        assert _checks(url) == {"ck_model_calls_key_source", "ck_orgs_region"}
