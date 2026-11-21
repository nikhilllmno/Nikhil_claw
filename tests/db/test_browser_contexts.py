"""The Postgres store of the persistent browser contexts (docs/architecture.md, Section 14.1)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast

import pytest
from sqlalchemy import Table, delete, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.browser.contexts import MemoryContextStore, PostgresContextStore
from sama.db.models import PersistentBrowserContext
from tests.db.world import World

pytestmark = pytest.mark.usefixtures("world")


@pytest.fixture(autouse=True)
async def keep_the_seeded_contexts(engine: AsyncEngine) -> AsyncIterator[None]:
    """The RLS tests expect the seeded rows, so each test puts them back."""
    table = cast(Table, PersistentBrowserContext.__table__)
    async with engine.connect() as connection:
        rows = [dict(r) for r in (await connection.execute(select(table))).mappings()]
    yield
    async with engine.begin() as connection:
        await connection.execute(delete(table))
        if rows:
            await connection.execute(insert(table), rows)


async def test_get_claim_and_forget(engine: AsyncEngine, world: World) -> None:
    store = PostgresContextStore(engine)
    org, carol = str(world.ids["org-a"]), str(world.ids["carol"])
    assert await store.get(org, carol, "cloud") is None
    assert await store.claim(org, carol, "cloud", "ctx-carol-1") == "ctx-carol-1"
    # The first stored context wins.
    assert await store.claim(org, carol, "cloud", "ctx-carol-2") == "ctx-carol-1"
    assert await store.get(org, carol, "cloud") == "ctx-carol-1"
    # Each target has its own context.
    assert await store.get(org, carol, "my_chrome") is None
    # forget removes the row only if it still has that id.
    await store.forget(org, carol, "cloud", "ctx-carol-2")
    assert await store.get(org, carol, "cloud") == "ctx-carol-1"
    await store.forget(org, carol, "cloud", "ctx-carol-1")
    assert await store.get(org, carol, "cloud") is None


async def test_get_marks_the_context_as_used(engine: AsyncEngine, world: World) -> None:
    store = PostgresContextStore(engine)
    table = PersistentBrowserContext
    org, alice = world.ids["org-a"], world.ids["alice"]

    async def last_used() -> object:
        async with engine.connect() as connection:
            row = await connection.execute(
                select(table.last_used_at).where(table.org_id == org, table.user_id == alice)
            )
            return row.scalar_one()

    before = await last_used()
    assert await store.get(str(org), str(alice), "cloud") == "ctx-alice"
    assert await last_used() != before


async def test_a_user_of_another_org_gets_nothing(engine: AsyncEngine, world: World) -> None:
    store = PostgresContextStore(engine)
    # alice is in org A. Her context is not found with org B, even with her user id.
    assert await store.get(str(world.ids["org-b"]), str(world.ids["alice"]), "cloud") is None
    await store.forget(str(world.ids["org-b"]), str(world.ids["alice"]), "cloud", "ctx-alice")
    assert await store.get(str(world.ids["org-a"]), str(world.ids["alice"]), "cloud") == (
        "ctx-alice"
    )


async def test_parallel_claims_store_one_context(engine: AsyncEngine, world: World) -> None:
    store = PostgresContextStore(engine)
    org, dave = str(world.ids["org-a"]), str(world.ids["dave"])
    results = await asyncio.gather(
        *(store.claim(org, dave, "cloud", f"ctx-dave-{n}") for n in range(5))
    )
    assert len(set(results)) == 1
    assert await store.get(org, dave, "cloud") == results[0]


async def test_the_memory_store_has_the_same_rules() -> None:
    store = MemoryContextStore()
    assert await store.get("o", "u", "cloud") is None
    assert await store.claim("o", "u", "cloud", "a") == "a"
    assert await store.claim("o", "u", "cloud", "b") == "a"
    await store.forget("o", "u", "cloud", "b")
    assert await store.get("o", "u", "cloud") == "a"
    await store.forget("o", "u", "cloud", "a")
    assert await store.get("o", "u", "cloud") is None
