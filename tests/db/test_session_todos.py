"""The Postgres store of the ``todo`` tool (docs/architecture.md, Sections 16.7 and 17)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import cast

import pytest
from sqlalchemy import Table, delete, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import SessionTodo
from sama.tenancy import RunTokenClaims
from sama.tools.todo import PostgresTodoStore, TodoError, TodoItem, TodoStatus
from tests.db.world import World

pytestmark = pytest.mark.usefixtures("world")


@pytest.fixture(autouse=True)
async def keep_the_seeded_todos(engine: AsyncEngine) -> AsyncIterator[None]:
    """The tests change rows of the shared seed. Other test files (for example the RLS tests)
    expect the seeded rows, so each test puts them back."""
    table = cast(Table, SessionTodo.__table__)
    async with engine.connect() as connection:
        rows = [dict(r) for r in (await connection.execute(select(table))).mappings()]
    yield
    async with engine.begin() as connection:
        await connection.execute(delete(table))
        if rows:
            await connection.execute(insert(table), rows)


def claims(world: World, session: str, run: str | None = None) -> RunTokenClaims:
    org = {"a1": "org-a", "a2": "org-a", "b1": "org-b"}[session.split("-")[0]]
    return RunTokenClaims(
        org_id=str(world.ids[org]),
        workspace_id=str(world.ids[session.split("-")[0]]),
        session_id=str(world.ids[session]),
        run_id=run or str(uuid.uuid4()),
        token_id="t-1",
        expires_at=2_000_000_000,
    )


async def test_the_store_creates_reads_and_changes_a_list(
    engine: AsyncEngine, world: World
) -> None:
    store = PostgresTodoStore(engine)
    me = claims(world, "a2-shared")
    async with engine.begin() as connection:
        await connection.execute(
            delete(SessionTodo).where(SessionTodo.session_id == uuid.UUID(me.session_id))
        )
    assert await store.read(me) == []
    one = TodoItem(id="1", content="Plan", status=TodoStatus.IN_PROGRESS)
    assert await store.change(me, lambda items: [*items, one]) == [one]
    assert await store.read(me) == [one]
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                select(SessionTodo.run_id).where(SessionTodo.session_id == uuid.UUID(me.session_id))
            )
        ).scalar_one()
    assert str(row) == me.run_id
    # Another org with the same session id sees nothing.
    other = me.model_copy(update={"org_id": str(world.ids["org-b"])})
    assert await store.read(other) == []


async def test_concurrent_changes_do_not_lose_items(engine: AsyncEngine, world: World) -> None:
    store = PostgresTodoStore(engine)
    me = claims(world, "b1-shared")
    await store.change(me, lambda items: [])

    async def add(n: int) -> None:
        await store.change(me, lambda items: [*items, TodoItem(id=str(n), content="x")])

    await asyncio.gather(*(add(n) for n in range(8)))
    assert sorted(i.id for i in await store.read(me)) == [str(n) for n in range(8)]


async def test_a_failed_change_keeps_the_list(engine: AsyncEngine, world: World) -> None:
    store = PostgresTodoStore(engine)
    me = claims(world, "a1-shared")
    before = await store.read(me)

    def bad(items: list[TodoItem]) -> list[TodoItem]:
        raise TodoError("no")

    with pytest.raises(TodoError):
        await store.change(me, bad)
    assert await store.read(me) == before


async def test_a_session_of_another_workspace_is_refused(engine: AsyncEngine, world: World) -> None:
    store = PostgresTodoStore(engine)
    wrong = claims(world, "a2-shared").model_copy(update={"workspace_id": str(world.ids["a1"])})
    async with engine.begin() as connection:
        await connection.execute(
            delete(SessionTodo).where(SessionTodo.session_id == uuid.UUID(wrong.session_id))
        )
    with pytest.raises(IntegrityError):
        await store.change(wrong, lambda items: [])


async def test_the_seeded_rows_are_back(engine: AsyncEngine, world: World) -> None:
    """Runs after the tests that change the seed (pytest keeps the file order)."""
    async with engine.connect() as connection:
        sessions = {
            str(r.session_id) for r in await connection.execute(select(SessionTodo.session_id))
        }
    assert str(world.ids["a2-shared"]) in sessions
