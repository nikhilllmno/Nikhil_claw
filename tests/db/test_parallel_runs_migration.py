"""Migration 0039: busy modes and parallel runs (docs/architecture.md, Section 4.3, M7.1)."""

from __future__ import annotations

import asyncio

import asyncpg
from alembic import command

from tests.db.conftest import alembic_config, migrated_database


async def _fetch(url: str, query: str, *args: object) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


def _columns(url: str, table: str) -> set[str]:
    rows = asyncio.run(
        _fetch(
            url,
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = $1",
            table,
        )
    )
    return {row["column_name"] for row in rows}


def _indexes(url: str) -> set[str]:
    rows = asyncio.run(_fetch(url, "SELECT indexname FROM pg_indexes WHERE tablename = 'runs'"))
    return {row["indexname"] for row in rows}


def test_the_upgrade_adds_the_columns_and_the_downgrade_removes_them() -> None:
    with migrated_database() as url:
        assert {"busy_mode", "max_parallel_runs"} <= _columns(url, "agent_profiles")
        assert {"allow_parallel", "side", "context_seq"} <= _columns(url, "runs")
        assert "ix_runs_one_main_per_session" in _indexes(url)
        assert "ix_runs_one_active_per_session" not in _indexes(url)
        config = alembic_config(url)
        command.downgrade(config, "0038")
        assert not {"busy_mode", "max_parallel_runs"} & _columns(url, "agent_profiles")
        assert not {"allow_parallel", "side", "context_seq"} & _columns(url, "runs")
        assert "ix_runs_one_active_per_session" in _indexes(url)
        command.upgrade(config, "head")
        assert "ix_runs_one_main_per_session" in _indexes(url)
