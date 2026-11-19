"""Migration 0005 on a database that has active runs (docs/architecture.md, Section 4.3)."""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
from alembic import command

from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import seed


async def _set_status(url: str, run_ids: dict[str, uuid.UUID]) -> None:
    connection = await asyncpg.connect(url)
    try:
        for status, run_id in run_ids.items():
            await connection.execute("UPDATE runs SET status = $1 WHERE id = $2", status, run_id)
    finally:
        await connection.close()


async def _rows(url: str, run_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[str, str | None]]:
    connection = await asyncpg.connect(url)
    try:
        rows = await connection.fetch(
            "SELECT id, status, failure_code FROM runs WHERE id = ANY($1::uuid[])", run_ids
        )
    finally:
        await connection.close()
    return {row["id"]: (row["status"], row["failure_code"]) for row in rows}


def test_the_upgrade_ends_the_active_runs() -> None:
    with migrated_database() as url:
        world = asyncio.run(seed(url))
        runs = [value for label, value in world.ids.items() if label.startswith("run:")]
        assert len(runs) >= 3
        config = alembic_config(url)
        command.downgrade(config, "0004")
        # Before 0005 a running or paused run needs no lease.
        asyncio.run(_set_status(url, {"running": runs[0], "paused": runs[1]}))
        command.upgrade(config, "head")
        rows = asyncio.run(_rows(url, runs[:3]))
        assert rows[runs[0]] == ("failed", "internal")
        assert rows[runs[1]] == ("failed", "internal")
        assert rows[runs[2]][0] == "queued"
