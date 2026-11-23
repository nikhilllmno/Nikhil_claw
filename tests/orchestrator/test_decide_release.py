"""``GateService.decide_with_scope`` stores the decision and queues the run again in one transaction
(docs/architecture.md, Section 4.4)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import Approval, SessionToolAllow
from sama.gate.service import ApprovalError, GateService
from tests.orchestrator.conftest import Tenant
from tests.orchestrator.test_worker import Harness, approval_script

pytestmark = pytest.mark.usefixtures("tenant")


async def paused(engine: AsyncEngine, tenant: Tenant) -> tuple[Harness, uuid.UUID]:
    harness = Harness(engine)
    harness.runtime.add_script(approval_script())
    run_id = await harness.send(tenant, "s1", "clean up")
    assert await harness.worker.run_once()
    assert await harness.status_is(run_id, "paused")
    return harness, run_id


async def status(engine: AsyncEngine, run_id: uuid.UUID) -> str:
    async with engine.connect() as connection:
        value = (
            await connection.execute(select(Approval.status).where(Approval.run_id == run_id))
        ).scalar_one()
    return str(value)


async def test_the_decision_and_the_release_are_one_transaction(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    harness, run_id = await paused(engine, tenant)
    gate = GateService(engine)

    async def release(connection: AsyncConnection) -> list[uuid.UUID]:
        return await harness.queue.release_decided_in(connection, org_id=tenant.org, run_id=run_id)

    decision = await gate.decide_with_scope(
        org_id=tenant.org,
        run_id=run_id,
        call_id="call-9",
        user_id=tenant.ids["mia"],
        approve=True,
        release=release,
    )
    assert decision.status == "approved"
    assert decision.scope == "once"
    assert decision.released == (run_id,)
    assert decision.proposal is False
    assert await harness.status_is(run_id, "queued")


async def test_a_failed_release_undoes_the_decision(engine: AsyncEngine, tenant: Tenant) -> None:
    harness, run_id = await paused(engine, tenant)
    gate = GateService(engine)

    async def release(connection: AsyncConnection) -> list[uuid.UUID]:
        raise RuntimeError("the release failed")

    with pytest.raises(RuntimeError):
        await gate.decide_with_scope(
            org_id=tenant.org,
            run_id=run_id,
            call_id="call-9",
            user_id=tenant.ids["mia"],
            approve=True,
            release=release,
        )
    assert await status(engine, run_id) == "pending"
    assert await harness.status_is(run_id, "paused")


async def test_a_user_outside_the_workspace_cannot_decide(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    _harness, run_id = await paused(engine, tenant)
    with pytest.raises(ApprovalError, match="cannot decide"):
        await GateService(engine).decide_with_scope(
            org_id=tenant.org,
            run_id=run_id,
            call_id="call-9",
            user_id=uuid.uuid4(),
            approve=True,
        )
    assert await status(engine, run_id) == "pending"


async def test_deciding_twice_fails(engine: AsyncEngine, tenant: Tenant) -> None:
    _harness, run_id = await paused(engine, tenant)
    gate = GateService(engine)
    args = {
        "org_id": tenant.org,
        "run_id": run_id,
        "call_id": "call-9",
        "user_id": tenant.ids["mia"],
        "approve": False,
    }
    assert (await gate.decide_with_scope(**args)).status == "rejected"  # type: ignore[arg-type]
    with pytest.raises(ApprovalError, match="already rejected"):
        await gate.decide_with_scope(**args)  # type: ignore[arg-type]


async def test_a_session_scope_and_the_release_are_one_transaction(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    harness, run_id = await paused(engine, tenant)

    async def release(connection: AsyncConnection) -> list[uuid.UUID]:
        return await harness.queue.release_decided_in(connection, org_id=tenant.org, run_id=run_id)

    decision = await GateService(engine).decide_with_scope(
        org_id=tenant.org,
        run_id=run_id,
        call_id="call-9",
        user_id=tenant.ids["mia"],
        approve=True,
        scope="session",
        release=release,
    )
    assert (decision.status, decision.scope) == ("approved", "session")
    assert decision.released == (run_id,)
    async with engine.connect() as connection:
        allowed = (
            (
                await connection.execute(
                    select(SessionToolAllow.tool).where(SessionToolAllow.org_id == tenant.org)
                )
            )
            .scalars()
            .all()
        )
    assert allowed == ["Bash"]


async def test_a_failed_release_leaves_no_session_rule(engine: AsyncEngine, tenant: Tenant) -> None:
    _harness, run_id = await paused(engine, tenant)

    async def release(connection: AsyncConnection) -> list[uuid.UUID]:
        raise RuntimeError("the release failed")

    with pytest.raises(RuntimeError):
        await GateService(engine).decide_with_scope(
            org_id=tenant.org,
            run_id=run_id,
            call_id="call-9",
            user_id=tenant.ids["mia"],
            approve=True,
            scope="session",
            release=release,
        )
    async with engine.connect() as connection:
        allowed = (
            (
                await connection.execute(
                    select(SessionToolAllow.tool).where(SessionToolAllow.org_id == tenant.org)
                )
            )
            .scalars()
            .all()
        )
    assert allowed == []
    assert await status(engine, run_id) == "pending"
