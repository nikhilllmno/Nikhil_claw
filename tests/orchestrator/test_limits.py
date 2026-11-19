"""Tests of the per-org limits of the run queue (docs/architecture.md, Section 4.3)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Org, Run
from sama.orchestrator.limits import QueueLimits
from sama.orchestrator.queue import RateLimitedError, RunQueue
from tests.orchestrator.conftest import Tenant, add_tenant


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, QueueLimits()),
        ({}, QueueLimits()),
        ("bad", QueueLimits()),
        (
            {"max_running_runs": 2, "user_messages_per_minute": 3, "org_messages_per_minute": 4},
            QueueLimits(2, 3, 4),
        ),
        (
            {
                "max_running_runs": 0,
                "user_messages_per_minute": True,
                "org_messages_per_minute": "9",
            },
            QueueLimits(),
        ),
        ({"max_running_runs": -1, "model_usd_per_month": 5}, QueueLimits()),
    ],
)
def test_limits_come_from_the_org_json(raw: Any, expected: QueueLimits) -> None:
    assert QueueLimits.from_json(raw) == expected


def test_the_defaults_match_the_docs() -> None:
    assert QueueLimits() == QueueLimits(5, 20, 120)


async def set_limits(engine: AsyncEngine, tenant: Tenant, **limits: int) -> None:
    async with engine.begin() as connection:
        await connection.execute(update(Org).where(Org.id == tenant.org).values(limits=limits))


async def send(queue: RunQueue, tenant: Tenant, session: str, user: bool = True) -> Any:
    return await queue.enqueue(
        org_id=tenant.org,
        session_id=tenant.ids[session],
        text="hi",
        user_id=tenant.ids["mia"] if user else None,
        unattended=not user,
    )


async def test_a_user_gets_a_message_rate_limit(engine: AsyncEngine, tenant: Tenant) -> None:
    await set_limits(engine, tenant, user_messages_per_minute=2)
    queue = RunQueue(engine)
    await send(queue, tenant, "s1")
    await send(queue, tenant, "s2")
    with pytest.raises(RateLimitedError, match="you sent 2 messages") as error:
        await send(queue, tenant, "s3")
    assert 1 <= error.value.retry_after_s <= 60
    await send(queue, tenant, "s3", user=False)  # a job is not a message of the user

    # Messages older than the window do not count.
    async with engine.begin() as connection:
        await connection.execute(
            update(Run)
            .where(Run.org_id == tenant.org)
            .values(created_at=Run.created_at - timedelta(seconds=61))
        )
    await send(queue, tenant, "s3")


async def test_an_org_gets_a_message_rate_limit(engine: AsyncEngine, tenant: Tenant) -> None:
    await set_limits(engine, tenant, org_messages_per_minute=2)
    queue = RunQueue(engine)
    await send(queue, tenant, "s1", user=False)
    await send(queue, tenant, "s2")
    with pytest.raises(RateLimitedError, match="the org sent 2 messages"):
        await send(queue, tenant, "s3")
    other = await add_tenant(engine)
    await send(queue, other, "s1")  # the limit is per org


async def test_concurrent_messages_do_not_pass_the_limit(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await set_limits(engine, tenant, user_messages_per_minute=3)
    queue = RunQueue(engine)
    results = await asyncio.gather(
        *(send(queue, tenant, s) for s in ("s1", "s2", "s3", "mine", "s1", "s2")),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, BaseException) for r in results) == 3
    assert all(isinstance(r, RateLimitedError) for r in results if isinstance(r, BaseException))


async def test_an_org_runs_at_most_its_running_limit(engine: AsyncEngine, tenant: Tenant) -> None:
    await set_limits(engine, tenant, max_running_runs=2)
    other = await add_tenant(engine)
    queue = RunQueue(engine)
    for session in ("s1", "s2", "s3", "mine"):
        await send(queue, tenant, session)
    waiting = await send(queue, other, "s1")
    results = await asyncio.gather(*(queue.claim(f"w{n}") for n in range(8)))
    claimed = [run for run in results if run is not None]
    mine = [run for run in claimed if run.org_id == tenant.org]
    assert len(mine) == 2, "the org runs 2 runs at most"
    assert [run.run_id for run in claimed if run.org_id == other.org] == [waiting]

    # A run that ends frees a place.
    assert await queue.claim("w9") is None
    async with engine.begin() as connection:
        await connection.execute(
            update(Run)
            .where(Run.id == mine[0].run_id)
            .values(status="cancelled", lease_owner=None, lease_expires_at=None)
        )
    freed = await queue.claim("w10")
    assert freed is not None
    assert freed.org_id == tenant.org
