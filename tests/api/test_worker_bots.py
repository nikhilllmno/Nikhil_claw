"""The worker bot routes and the daily budget of a bot (docs/architecture.md, Section 11.6)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AuditEntry, Membership, ModelCall, Session, User, WorkerBot
from sama.orchestrator.queue import ClaimedRun, RunQueue
from sama.runtime.contract import Usage
from sama.tenancy.authority import load_authority
from sama.workerbots import BOT_GONE, BUDGET_REACHED, bot_limit, spent_today, utc_day_start
from tests.api.conftest import bearer
from tests.api.test_app import add_user, mia
from tests.integrations.support import add_slack
from tests.orchestrator.conftest import Tenant

pytestmark = pytest.mark.usefixtures("tenant")


def bots(tenant: Tenant) -> str:
    return f"/workspaces/{tenant.ids['ws']}/worker-bots"


def body(tenant: Tenant, **changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": "Support bot",
        "agent_profile_id": str(tenant.ids["profile"]),
        "channel_id": "CSUPPORT1",
        "daily_budget_usd": "2.50",
    }
    values.update(changes)
    return values


async def workspace_admin(engine: AsyncEngine, tenant: Tenant) -> dict[str, str]:
    user_id, auth_id = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(User).values(
                id=user_id, org_id=tenant.org, auth_user_id=auth_id, display_name="ada"
            )
        )
        for workspace_id, role in ((None, "member"), (tenant.ids["ws"], "admin")):
            await connection.execute(
                insert(Membership).values(
                    org_id=tenant.org, user_id=user_id, workspace_id=workspace_id, role=role
                )
            )
    return bearer(auth_id)


async def test_a_workspace_admin_manages_a_worker_bot(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await add_slack(engine, tenant)
    admin = await workspace_admin(engine, tenant)
    created = await client.post(bots(tenant), json=body(tenant), headers=admin)
    assert created.status_code == 201, created.text
    bot = created.json()
    assert (bot["channel_id"], bot["daily_budget_usd"], bot["status"]) == (
        "CSUPPORT1",
        "2.5000",
        "active",
    )
    assert bot["spent_today_usd"] == "0"
    assert bot["integration_id"] == str(tenant.ids["slack"])

    listed = await client.get(bots(tenant), headers=mia(tenant))
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [bot["id"]], "a member lists the bots"

    again = await client.post(bots(tenant), json=body(tenant), headers=admin)
    assert again.status_code == 409
    assert "already has a worker bot" in again.json()["detail"]

    changed = await client.patch(
        f"{bots(tenant)}/{bot['id']}",
        json={"status": "disabled", "daily_budget_usd": "1"},
        headers=admin,
    )
    assert changed.status_code == 200, changed.text
    assert (changed.json()["status"], changed.json()["daily_budget_usd"]) == ("disabled", "1.0000")

    gone = await client.delete(f"{bots(tenant)}/{bot['id']}", headers=admin)
    assert gone.status_code == 204
    missing = await client.delete(f"{bots(tenant)}/{bot['id']}", headers=admin)
    assert missing.status_code == 404
    async with engine.connect() as connection:
        actions = list(
            await connection.scalars(
                select(AuditEntry.action)
                .where(AuditEntry.org_id == tenant.org, AuditEntry.target_type == "worker_bot")
                .order_by(AuditEntry.created_at)
            )
        )
    assert actions == ["worker_bot.created", "worker_bot.updated", "worker_bot.deleted"]


async def test_a_member_cannot_manage_a_worker_bot(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await add_slack(engine, tenant)
    response = await client.post(bots(tenant), json=body(tenant), headers=mia(tenant))
    assert response.status_code == 403
    stranger = bearer(await add_user(engine, tenant, workspace_member=False))
    hidden = await client.get(bots(tenant), headers=stranger)
    assert hidden.status_code == 404, "a user outside the workspace does not see it"


async def test_an_org_admin_manages_a_worker_bot(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await add_slack(engine, tenant)
    admin = bearer(await add_user(engine, tenant, workspace_member=False, role="admin"))
    response = await client.post(bots(tenant), json=body(tenant), headers=admin)
    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    "changes",
    [
        {"daily_budget_usd": "0"},
        {"daily_budget_usd": "10001"},
        {"daily_budget_usd": "-1"},
        {"channel_id": "not a channel"},
        {"channel_id": "DMIA0001"},
        {"name": ""},
        {"name": "x" * 81},
        {"extra": True},
    ],
)
async def test_a_bad_worker_bot_is_refused(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant, changes: dict[str, Any]
) -> None:
    await add_slack(engine, tenant)
    admin = bearer(await add_user(engine, tenant, workspace_member=False, role="owner"))
    response = await client.post(bots(tenant), json=body(tenant, **changes), headers=admin)
    assert response.status_code == 422


async def test_a_worker_bot_needs_slack_and_a_profile_of_the_workspace(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    owner = bearer(await add_user(engine, tenant, workspace_member=False, role="owner"))
    no_slack = await client.post(bots(tenant), json=body(tenant), headers=owner)
    assert no_slack.status_code == 409
    await add_slack(engine, tenant)
    wrong = await client.post(
        bots(tenant), json=body(tenant, agent_profile_id=str(uuid.uuid4())), headers=owner
    )
    assert wrong.status_code == 422
    assert "agent profile" in wrong.json()["detail"]
    empty = await client.patch(f"{bots(tenant)}/{uuid.uuid4()}", json={}, headers=owner)
    assert empty.status_code == 422
    unknown = await client.patch(
        f"{bots(tenant)}/{uuid.uuid4()}", json={"name": "x"}, headers=owner
    )
    assert unknown.status_code == 404


# -- the daily budget --------------------------------------------------------------------------


async def add_bot(engine: AsyncEngine, tenant: Tenant, budget: str = "1") -> uuid.UUID:
    integration_id = await add_slack(engine, tenant)
    bot_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(WorkerBot).values(
                id=bot_id,
                org_id=tenant.org,
                workspace_id=tenant.ids["ws"],
                agent_profile_id=tenant.ids["profile"],
                integration_id=integration_id,
                external_id="CBOT00001",
                name="bot",
                daily_budget_usd=Decimal(budget),
            )
        )
        await connection.execute(
            update(Session)
            .where(Session.id.in_([tenant.ids["s1"], tenant.ids["s2"]]))
            .values(worker_bot_id=bot_id)
        )
    return bot_id


async def spend(
    engine: AsyncEngine,
    tenant: Tenant,
    run_id: uuid.UUID,
    session: str,
    cost: str,
    *,
    at: datetime | None = None,
) -> None:
    values: dict[str, Any] = {
        "call_id": uuid.uuid4(),
        "org_id": tenant.org,
        "workspace_id": tenant.ids["ws"],
        "session_id": tenant.ids[session],
        "run_id": run_id,
        "provider": "anthropic",
        "model": "m",
        "endpoint": "messages",
        "status_code": 200,
        "cost_usd": Decimal(cost),
        "duration_ms": 1,
    }
    if at is not None:
        values["created_at"] = at
    async with engine.begin() as connection:
        await connection.execute(insert(ModelCall).values(**values))


async def authority(engine: AsyncEngine, tenant: Tenant, run_id: uuid.UUID) -> Any:
    async with engine.connect() as connection:
        return await load_authority(connection, tenant.org, run_id)


async def running(engine: AsyncEngine, tenant: Tenant, session: str) -> ClaimedRun:
    queue = RunQueue(engine)
    run_id = await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids[session], text="x", user_id=tenant.ids["mia"]
    )
    claimed = await queue.claim("w-bot")
    assert claimed is not None
    assert claimed.run_id == run_id
    return claimed


async def test_a_bot_run_stays_below_the_rest_of_the_daily_budget(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    bot_id = await add_bot(engine, tenant, "1")
    claimed = await running(engine, tenant, "s1")
    first = claimed.run_id
    await spend(engine, tenant, first, "s1", "0.30")
    yesterday = utc_day_start() - timedelta(minutes=1)
    await spend(engine, tenant, first, "s1", "5", at=yesterday)
    assert await RunQueue(engine).complete(
        claimed, "w-bot", output="ok", runtime_state=None, usage=Usage()
    )
    second = (await running(engine, tenant, "s2")).run_id
    await spend(engine, tenant, second, "s2", "0.10")
    async with engine.connect() as connection:
        assert await spent_today(connection, tenant.org, bot_id) == Decimal("0.40")
        limit = await bot_limit(connection, tenant.org, bot_id, second)
    assert (limit.problem, limit.max_cost_usd) == (None, Decimal("0.70"))
    found = await authority(engine, tenant, second)
    assert found.problem is None
    assert found.max_cost_usd == Decimal("0.70"), "the run's own cost counts against it"

    await spend(engine, tenant, first, "s1", "0.70")
    async with engine.connect() as connection:
        spent = await bot_limit(connection, tenant.org, bot_id, second)
    assert spent.problem == BUDGET_REACHED
    assert (await authority(engine, tenant, second)).problem == BUDGET_REACHED


async def test_a_run_that_crosses_midnight_gets_the_budget_of_the_new_day(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    """The run spent 0.80 of a budget of 1 yesterday. Today the bot spent nothing, so the run
    can spend 1 more: its limit is the cost of yesterday plus the full budget of today."""
    bot_id = await add_bot(engine, tenant, "1")
    run_id = (await running(engine, tenant, "s1")).run_id
    yesterday = utc_day_start() - timedelta(minutes=1)
    await spend(engine, tenant, run_id, "s1", "0.80", at=yesterday)
    async with engine.connect() as connection:
        assert await spent_today(connection, tenant.org, bot_id) == Decimal(0)
    found = await authority(engine, tenant, run_id)
    assert found.problem is None
    assert found.max_cost_usd == Decimal("1.80")
    await spend(engine, tenant, run_id, "s1", "0.25")
    assert (await authority(engine, tenant, run_id)).max_cost_usd == Decimal("1.80")


async def test_a_disabled_bot_stops_its_runs(engine: AsyncEngine, tenant: Tenant) -> None:
    bot_id = await add_bot(engine, tenant)
    run_id = (await running(engine, tenant, "s1")).run_id
    assert (await authority(engine, tenant, run_id)).problem is None
    async with engine.begin() as connection:
        await connection.execute(
            update(WorkerBot).where(WorkerBot.id == bot_id).values(status="disabled")
        )
    assert (await authority(engine, tenant, run_id)).problem == BOT_GONE


async def test_a_run_without_a_bot_has_no_bot_limit(engine: AsyncEngine, tenant: Tenant) -> None:
    await add_bot(engine, tenant, "0.01")
    run_id = (await running(engine, tenant, "s3")).run_id
    await spend(engine, tenant, run_id, "s3", "9")
    assert (await authority(engine, tenant, run_id)).problem is None


def test_the_utc_day_starts_at_midnight() -> None:
    moment = datetime(2026, 11, 20, 23, 59, tzinfo=UTC)
    assert utc_day_start(moment) == datetime(2026, 11, 20, tzinfo=UTC)
