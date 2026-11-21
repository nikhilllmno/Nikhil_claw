"""Tests of the model quotas of a plan in the run queue (docs/architecture.md, Section 17.5)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import ModelCall, Org
from sama.orchestrator.queue import QuotaExceededError, RunQueue
from tests.orchestrator.conftest import Tenant


async def spend(
    engine: AsyncEngine,
    tenant: Tenant,
    queue: RunQueue,
    cost: str,
    *,
    user: bool = True,
    at: datetime | None = None,
) -> None:
    """One model call of ``cost`` USD in a cancelled run of ``s1``."""
    run_id = await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids["s1"], text="x", unattended=True
    )
    await queue.cancel(org_id=tenant.org, run_id=run_id)
    values: dict[str, object] = {
        "call_id": uuid.uuid4(),
        "org_id": tenant.org,
        "workspace_id": tenant.ids["ws"],
        "session_id": tenant.ids["s1"],
        "run_id": run_id,
        "user_id": tenant.ids["mia"] if user else None,
        "provider": "anthropic",
        "model": "claude-x",
        "endpoint": "messages",
        "status_code": 200,
        "cost_usd": Decimal(cost),
        "duration_ms": 10,
    }
    if at is not None:
        values["created_at"] = at
    async with engine.begin() as connection:
        await connection.execute(insert(ModelCall).values(**values))


async def set_plan(engine: AsyncEngine, tenant: Tenant, plan: str, **limits: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Org).where(Org.id == tenant.org).values(plan=plan, limits=limits)
        )


async def send(queue: RunQueue, tenant: Tenant, *, user: bool = True) -> uuid.UUID:
    return await queue.enqueue(
        org_id=tenant.org,
        session_id=tenant.ids["s2"],
        text="hi",
        user_id=tenant.ids["mia"] if user else None,
        unattended=not user,
    )


async def test_the_org_quota_of_the_month_stops_a_new_message(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await spend(engine, tenant, queue, "4.99", user=False)
    await set_plan(engine, tenant, "free", model_usd_per_month="5.00")
    run_id = await send(queue, tenant)
    await queue.cancel(org_id=tenant.org, run_id=run_id)
    await spend(engine, tenant, queue, "0.01", user=False)
    with pytest.raises(QuotaExceededError) as error:
        await send(queue, tenant)
    assert error.value.quota == "model_usd_per_month"
    assert str(error.value) == (
        "The org used its model quota of USD 5.00 for this month on the Free plan. An org"
        " admin can change the plan on the Billing page."
    )
    # A job of the org is also a new run.
    with pytest.raises(QuotaExceededError):
        await send(queue, tenant, user=False)


async def test_the_spend_of_last_month_does_not_count(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    now = datetime.now(UTC)
    last_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(
        seconds=1
    )
    await spend(engine, tenant, queue, "50", at=last_month)
    await set_plan(engine, tenant, "pro", model_usd_per_month="10", model_usd_per_user_day="10")
    await send(queue, tenant)


async def test_the_user_quota_of_the_day_stops_only_that_user(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    yesterday = datetime.now(UTC).replace(hour=0, minute=0, second=0) - timedelta(seconds=1)
    await spend(engine, tenant, queue, "5", at=yesterday)
    await spend(engine, tenant, queue, "1.50")
    await spend(engine, tenant, queue, "9", user=False)
    await set_plan(engine, tenant, "pro", model_usd_per_user_day="2.00")
    run_id = await send(queue, tenant)
    await queue.cancel(org_id=tenant.org, run_id=run_id)
    await spend(engine, tenant, queue, "0.50")
    with pytest.raises(QuotaExceededError) as error:
        await send(queue, tenant)
    assert error.value.quota == "model_usd_per_user_day"
    assert "USD 2.00 for today on the Pro plan" in str(error.value)
    # A job has no user: the quota of the user does not apply.
    await send(queue, tenant, user=False)


async def test_no_quota_means_no_check(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    await spend(engine, tenant, queue, "1000")
    await set_plan(engine, tenant, "team")
    await send(queue, tenant)
