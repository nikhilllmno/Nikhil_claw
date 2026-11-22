"""Migration 0050: the model of a session (docs/architecture.md, Section 5.4; roadmap M8.2).

The column is in ``sessions``, which has ``org_id`` and its RLS policy. The tests check that the
policy and the grants cover the new column.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db import as_user
from tests.db.conftest import alembic_config, migrated_database
from tests.db.test_rls import INSUFFICIENT_PRIVILEGE, sqlstate
from tests.db.world import World, seed

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")


async def _fetch(url: str, query: str, *args: object) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


def _has_column(url: str) -> bool:
    rows = asyncio.run(
        _fetch(
            url,
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'"
            " AND table_name = 'sessions' AND column_name = 'model_override'",
        )
    )
    return bool(rows)


def test_the_upgrade_adds_the_column_and_the_downgrade_removes_it() -> None:
    with migrated_database() as url:
        assert _has_column(url)
        config = alembic_config(url)
        command.downgrade(config, "0047")
        assert not _has_column(url)
        command.upgrade(config, "head")
        assert _has_column(url)


def test_the_column_is_null_by_default_and_a_check_limits_the_text() -> None:
    with migrated_database() as url:
        world = asyncio.run(seed(url))
        [row] = asyncio.run(
            _fetch(url, "SELECT model_override FROM sessions WHERE id = $1", world.ids["a1-alice"])
        )
        assert row["model_override"] is None
        asyncio.run(
            _fetch(
                url,
                "UPDATE sessions SET model_override = $2 WHERE id = $1",
                world.ids["a1-alice"],
                "claude-sonnet-5-5",
            )
        )
        for bad in ("", "x" * 101):
            with pytest.raises(asyncpg.CheckViolationError):
                asyncio.run(
                    _fetch(
                        url,
                        "UPDATE sessions SET model_override = $2 WHERE id = $1",
                        world.ids["a1-alice"],
                        bad,
                    )
                )


@pytest.fixture
async def chosen(engine: AsyncEngine, world: World) -> World:
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE sessions SET model_override = 'claude-sonnet-5-5' WHERE id = :id"),
            {"id": world.ids["a1-alice"]},
        )
    return world


async def _models(engine: AsyncEngine, world: World, user: str) -> dict[str, str | None]:
    async with as_user(engine, world.auth[user]) as connection:
        rows = await connection.execute(text("SELECT id, model_override FROM sessions"))
        return {world.label(row.id): row.model_override for row in rows}


@pytest.mark.usefixtures("chosen")
async def test_a_member_reads_the_model_of_a_session_that_the_member_sees(
    engine: AsyncEngine, world: World
) -> None:
    assert (await _models(engine, world, "alice"))["a1-alice"] == "claude-sonnet-5-5"
    # A member of the same workspace does not see the private session of alice at all.
    assert "a1-alice" not in await _models(engine, world, "dave")


@pytest.mark.usefixtures("chosen")
async def test_a_user_of_another_org_reads_no_model(engine: AsyncEngine, world: World) -> None:
    seen = await _models(engine, world, "bob")
    assert "a1-alice" not in seen
    assert set(seen) == {"b1-shared", "b1-bob"}
    assert await _models(engine, world, "stranger") == {}


async def test_authenticated_cannot_write_the_model(engine: AsyncEngine, world: World) -> None:
    for user in ("alice", "carol"):
        with pytest.raises(DBAPIError) as error:
            async with as_user(engine, world.auth[user]) as connection:
                await connection.execute(text("UPDATE sessions SET model_override = 'x'"))
        assert sqlstate(error.value) == INSUFFICIENT_PRIVILEGE
