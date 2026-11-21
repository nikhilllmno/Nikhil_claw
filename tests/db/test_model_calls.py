"""The Postgres store of the model proxy (docs/architecture.md, Section 16.6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import ModelCall, Org
from sama.modelproxy import PostgresUsageStore, TokenCounts
from sama.modelproxy.prices import Provider
from sama.modelproxy.store import CallRecord, OrgLimits
from sama.modelproxy.usage import Endpoint
from sama.vault.store import KeySource
from tests.db.world import World

pytestmark = pytest.mark.usefixtures("world")

NOW = datetime.now(UTC)
LATER = datetime(2100, 1, 1, tzinfo=UTC)
"""After every row, also the seeded rows, which get ``now()`` when the test seeds them."""


def call(world: World, session: str, *, cost: str, at: datetime = NOW, **ids: str) -> CallRecord:
    """A call in the seeded session ``session`` (see tests/db/world.py)."""
    org = {"a1": "org-a", "a2": "org-a", "b1": "org-b"}[session.split("-")[0]]
    workspace = session.split("-")[0]
    owner = session.split("-")[1]
    return CallRecord(
        org_id=ids.get("org_id", str(world.ids[org])),
        workspace_id=str(world.ids[workspace]),
        session_id=str(world.ids[session]),
        run_id=ids.get("run_id", str(world.ids[f"run:{session}"])),
        user_id=None if owner == "shared" else str(world.ids[owner]),
        provider=Provider.ANTHROPIC,
        model="claude-sonnet-5-5",
        endpoint=Endpoint.MESSAGES,
        status_code=200,
        counts=TokenCounts(input=100, output=20, cache_read=5, cache_write=3, cache_write_long=2),
        key_source=KeySource.SAMA,
        cost_usd=Decimal(cost),
        duration_ms=50,
        created_at=at,
    )


async def test_the_store_records_and_adds_up_the_calls(engine: AsyncEngine, world: World) -> None:
    store = PostgresUsageStore(engine)
    org_a, run = str(world.ids["org-a"]), str(world.ids["run:a1-alice"])
    before = await store.run_spend(org_a, run)
    await store.record(call(world, "a1-alice", cost="0.25"))
    await store.record(call(world, "a1-alice", cost="0.5", at=NOW - timedelta(days=40)))
    spend = await store.run_spend(org_a, run)
    assert spend.tokens - before.tokens == 2 * 130
    assert spend.cost_usd - before.cost_usd == Decimal("0.75")
    month = NOW - timedelta(days=1)
    alice = str(world.ids["alice"])
    assert await store.user_cost(org_a, alice, month) >= Decimal("0.25")
    assert await store.user_cost(org_a, alice, LATER) == 0
    assert await store.org_cost(str(world.ids["org-b"]), LATER) == 0


async def test_the_limits_come_from_the_org_row(engine: AsyncEngine, world: World) -> None:
    store = PostgresUsageStore(engine)
    org_b = world.ids["org-b"]
    assert await store.org_limits(str(org_b)) == OrgLimits()
    async with engine.begin() as connection:
        await connection.execute(
            update(Org).where(Org.id == org_b).values(limits={"model_usd_per_month": "12.5"})
        )
    assert (await store.org_limits(str(org_b))).model_usd_per_month == Decimal("12.5")


async def test_a_call_cannot_name_a_run_of_another_org(engine: AsyncEngine, world: World) -> None:
    store = PostgresUsageStore(engine)
    wrong = call(world, "a1-alice", cost="0.1", run_id=str(world.ids["run:b1-bob"]))
    with pytest.raises(IntegrityError):
        await store.record(wrong)


async def test_a_negative_cost_is_refused(engine: AsyncEngine, world: World) -> None:
    async with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            await connection.execute(
                text("UPDATE model_calls SET cost_usd = -1 WHERE org_id = :org"),
                {"org": world.ids["org-a"]},
            )


async def test_a_second_write_of_the_same_record_does_nothing(
    engine: AsyncEngine, world: World
) -> None:
    store = PostgresUsageStore(engine)
    org_b, run = str(world.ids["org-b"]), str(world.ids["run:b1-bob"])
    before = await store.run_spend(org_b, run)
    record = call(world, "b1-bob", cost="0.5")
    await store.record(record)
    await store.record(record)
    after = await store.run_spend(org_b, run)
    assert after.cost_usd - before.cost_usd == Decimal("0.5")


async def test_the_store_keeps_the_key_source_and_counts_both(
    engine: AsyncEngine, world: World
) -> None:
    """M6.7: a call with the key of the org costs the same and counts in the same budgets."""
    store = PostgresUsageStore(engine)
    org_b, run = str(world.ids["org-b"]), str(world.ids["run:b1-bob"])
    before = await store.run_spend(org_b, run)
    own = call(world, "b1-bob", cost="0.2").model_copy(update={"key_source": KeySource.ORG})
    await store.record(own)
    await store.record(call(world, "b1-bob", cost="0.3"))
    after = await store.run_spend(org_b, run)
    assert after.cost_usd - before.cost_usd == Decimal("0.5")
    async with engine.connect() as connection:
        source = await connection.scalar(
            select(ModelCall.key_source).where(ModelCall.call_id == own.call_id)
        )
    assert source == "org"


async def test_a_key_source_outside_the_list_is_refused(engine: AsyncEngine, world: World) -> None:
    async with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            await connection.execute(
                text("UPDATE model_calls SET key_source = 'other' WHERE org_id = :org"),
                {"org": world.ids["org-a"]},
            )


@pytest.mark.parametrize("region", ["", "AP-SOUTH-1", "ap-south", "mars", "ap-south-1; x"])
async def test_an_org_region_must_be_a_region_id(
    engine: AsyncEngine, world: World, region: str
) -> None:
    async with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            await connection.execute(
                update(Org).where(Org.id == world.ids["org-a"]).values(region=region)
            )
