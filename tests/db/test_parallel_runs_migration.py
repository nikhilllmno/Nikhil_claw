"""Migration 0043: busy modes and parallel runs (docs/architecture.md, Section 4.3, M7.1)."""

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
        command.downgrade(config, "0042")
        assert not {"busy_mode", "max_parallel_runs"} & _columns(url, "agent_profiles")
        assert not {"allow_parallel", "side", "context_seq"} & _columns(url, "runs")
        assert "ix_runs_one_active_per_session" in _indexes(url)
        command.upgrade(config, "head")
        assert "ix_runs_one_main_per_session" in _indexes(url)


def test_the_downgrade_ends_side_runs_and_drops_their_state() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        session = asyncio.run(_fetch(url, "SELECT id, org_id, agent_profile_id FROM sessions"))[0]
        ids = []
        for side, status in ((False, "completed"), (True, "completed"), (True, "paused")):
            [inserted] = asyncio.run(
                _fetch(
                    url,
                    "INSERT INTO runs (org_id, session_id, agent_profile_id, runtime, status,"
                    " runtime_state, side, pending_approval_id, finished_at) VALUES ($1, $2, $3,"
                    " 'fake', $4, 'state', $5, CASE WHEN $4 = 'paused' THEN 'call-1' END,"
                    " CASE WHEN $4 = 'completed' THEN now() END) RETURNING id",
                    session["org_id"],
                    session["id"],
                    session["agent_profile_id"],
                    status,
                    side,
                )
            )
            ids.append(inserted["id"])
        command.downgrade(alembic_config(url), "0042")
        rows = asyncio.run(
            _fetch(url, "SELECT id, status, runtime_state FROM runs WHERE id = ANY($1)", ids)
        )
        states = {row["id"]: (row["status"], row["runtime_state"]) for row in rows}
        assert [states[run_id] for run_id in ids] == [
            ("completed", "state"),
            ("completed", None),
            ("failed", None),
        ]
