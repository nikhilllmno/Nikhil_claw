"""Tests of ``delegate_task`` and ``delegate_status`` (docs/architecture.md, Section 13.2)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Message, Run, Session
from sama.delegation import (
    MAX_CHILDREN,
    ChildTask,
    DelegationError,
    DelegationService,
    child_budget,
    render_started,
    render_statuses,
)
from sama.orchestrator.queue import ClaimedRun, RunQueue
from sama.runtime.contract import FailureCode, Usage
from sama.tenancy import RunTokenClaims
from tests.orchestrator.conftest import Tenant, add_tenant

OWNER = "worker-d"


def claims(tenant: Tenant, session_id: uuid.UUID, run_id: uuid.UUID) -> RunTokenClaims:
    return RunTokenClaims(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(session_id),
        run_id=str(run_id),
        token_id="t-1",
        expires_at=2_000_000_000,
    )


async def parent_run(
    queue: RunQueue, tenant: Tenant, session: str = "s1"
) -> tuple[RunTokenClaims, ClaimedRun]:
    """A running chat run of ``session``."""
    run_id = await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids[session], text="hi", user_id=tenant.ids["mia"]
    )
    claimed = await queue.claim(OWNER)
    assert claimed is not None
    assert claimed.run_id == run_id
    return claims(tenant, tenant.ids[session], run_id), claimed


async def claim_child(queue: RunQueue, run_id: uuid.UUID) -> ClaimedRun:
    """Claims the next run, which must be ``run_id``. Use it when one run is queued."""
    claimed = await queue.claim(OWNER)
    assert claimed is not None
    assert claimed.run_id == run_id
    return claimed


async def rows(engine: AsyncEngine, query: Any) -> list[Any]:
    async with engine.connect() as connection:
        return list(await connection.execute(query))


def service(engine: AsyncEngine, queue: RunQueue) -> DelegationService:
    return DelegationService(engine, queue, poll_s=0.01)


async def test_a_batch_starts_one_child_session_and_one_run_for_each_task(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    me, _ = await parent_run(queue, tenant, "mine")
    children = await service(engine, queue).start(
        me,
        [
            ChildTask(goal="Find the bug", context="It is in parse()", max_turns=5),
            ChildTask(goal="Write the docs", max_cost_usd=Decimal("2.5")),
        ],
    )
    assert len(children) == 2
    sessions = await rows(
        engine,
        select(Session).where(Session.id.in_([c.session_id for c in children])),
    )
    for session in sessions:
        assert session.lineage == "delegation"
        assert session.parent_session_id == tenant.ids["mine"]
        assert session.parent_run_id == uuid.UUID(me.run_id)
        # A child of a private session is private to the same owner.
        assert (session.audience, session.owner_user_id) == ("private", tenant.ids["mia"])
        assert session.agent_profile_id == tenant.ids["profile"]
    runs = {
        r.session_id: r
        for r in await rows(engine, select(Run).where(Run.id.in_([c.run_id for c in children])))
    }
    first, second = (runs[c.session_id] for c in children)
    assert first.unattended
    assert second.unattended
    assert first.triggered_by_user_id == tenant.ids["mia"]
    assert first.budget["max_turns"] == 5
    assert first.budget["max_cost_usd"] == "1.00"
    assert second.budget["max_turns"] == 30
    assert second.budget["max_cost_usd"] == "2.5"
    prompt = (await rows(engine, select(Message.content).where(Message.run_id == first.id)))[0][0][
        "text"
    ]
    assert "Find the bug" in prompt
    assert "It is in parse()" in prompt
    assert "Started 2 child agents" in render_started(children)


async def test_the_profile_budget_caps_the_child_budget(engine: AsyncEngine) -> None:
    tenant = await add_tenant(engine, budget={"max_turns": 4, "max_cost_usd": "0.50"})
    queue = RunQueue(engine)
    me, _ = await parent_run(queue, tenant)
    [child] = await service(engine, queue).start(me, [ChildTask(goal="x", max_turns=50)])
    [run] = await rows(engine, select(Run.budget).where(Run.id == child.run_id))
    assert run.budget["max_turns"] == 4
    assert Decimal(run.budget["max_cost_usd"]) == Decimal("0.50")


def test_the_arguments_cannot_raise_the_budget_over_the_caps() -> None:
    budget = child_budget(ChildTask(goal="x", max_turns=1_000, max_cost_usd=Decimal("99")))
    assert budget["max_turns"] == 100
    assert budget["max_cost_usd"] == "10.00"
    assert budget["max_wall_time_s"] == 1_800.0


async def test_a_run_starts_at_most_ten_children(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    me, _ = await parent_run(queue, tenant)
    delegation = service(engine, queue)
    with pytest.raises(DelegationError, match="10 tasks or fewer"):
        await delegation.start(me, [ChildTask(goal=f"t{i}") for i in range(MAX_CHILDREN + 1)])
    await delegation.start(me, [ChildTask(goal=f"t{i}") for i in range(MAX_CHILDREN - 1)])
    with pytest.raises(DelegationError, match="10 children at most"):
        await delegation.start(me, [ChildTask(goal="a"), ChildTask(goal="b")])
    # The failed batch started nothing.
    found = await rows(
        engine, select(Session.id).where(Session.parent_run_id == uuid.UUID(me.run_id))
    )
    assert len(found) == MAX_CHILDREN - 1
    await delegation.start(me, [ChildTask(goal="last")])


async def test_a_child_cannot_delegate(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    me, parent = await parent_run(queue, tenant)
    delegation = service(engine, queue)
    [child] = await delegation.start(me, [ChildTask(goal="x")])
    await queue.complete(parent, OWNER, output="ok", runtime_state=None, usage=Usage())
    await claim_child(queue, child.run_id)
    with pytest.raises(DelegationError, match="cannot delegate"):
        await delegation.start(
            claims(tenant, child.session_id, child.run_id), [ChildTask(goal="y")]
        )


async def test_an_ended_run_cannot_delegate(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    me, parent = await parent_run(queue, tenant)
    await queue.complete(parent, OWNER, output="ok", runtime_state=None, usage=Usage())
    with pytest.raises(DelegationError, match="not active"):
        await service(engine, queue).start(me, [ChildTask(goal="x")])


async def test_status_and_wait_show_the_result_of_a_child(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    me, _ = await parent_run(queue, tenant)
    delegation = service(engine, queue)
    children = await delegation.start(me, [ChildTask(goal="one"), ChildTask(goal="two")])
    waited = await delegation.wait(me, children, timeout_s=0.05)
    assert [s.status for s in waited] == ["queued", "queued"]
    assert "still work" in render_statuses(waited, waited=True)

    # Both runs start in one transaction, so the claim order is not fixed.
    claimed: dict[uuid.UUID, ClaimedRun] = {}
    for _ in children:
        run = await queue.claim(OWNER)
        assert run is not None
        claimed[run.run_id] = run
    first, second = (claimed[c.run_id] for c in children)
    await queue.complete(
        first,
        OWNER,
        output="the answer is 42",
        runtime_state=None,
        usage=Usage(cost_usd=Decimal("0.25")),
    )
    await queue.fail(second, OWNER, code=FailureCode.MODEL_ERROR, message="it broke", usage=Usage())
    waited = await delegation.wait(me, children, timeout_s=5)
    assert [s.status for s in waited] == ["completed", "failed"]
    assert waited[0].result == "the answer is 42"
    assert waited[0].cost_usd == Decimal("0.25")
    assert waited[1].error == "it broke"
    text = render_statuses(waited, waited=True)
    assert "untrusted" in text
    assert "still work" not in text

    [one] = await delegation.status(me, children[0].session_id)
    assert one.status == "completed"
    assert len(await delegation.status(me)) == 2


async def test_cancel_stops_the_open_run_of_a_child(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    me, _ = await parent_run(queue, tenant)
    delegation = service(engine, queue)
    [child] = await delegation.start(me, [ChildTask(goal="x")])
    assert await delegation.cancel(me, child.session_id) == "cancelled"
    assert await delegation.cancel(me, child.session_id) == "ended"
    [status] = await delegation.status(me, child.session_id)
    assert status.status == "cancelled"


async def test_steer_sends_a_follow_up_run_with_the_same_budget(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    me, _ = await parent_run(queue, tenant)
    delegation = service(engine, queue)
    [child] = await delegation.start(me, [ChildTask(goal="x", max_turns=7)])
    run_id = await delegation.steer(me, child.session_id, "Also check the tests.")
    [run] = await rows(engine, select(Run).where(Run.id == run_id))
    assert run.session_id == child.session_id
    assert run.unattended
    assert run.budget["max_turns"] == 7
    [content] = (await rows(engine, select(Message.content).where(Message.run_id == run_id)))[0]
    assert "Also check the tests." in content["text"]


async def test_a_session_cannot_control_the_child_of_another_session(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    me, parent = await parent_run(queue, tenant)
    other, _ = await parent_run(queue, tenant, "s2")
    delegation = service(engine, queue)
    [child] = await delegation.start(me, [ChildTask(goal="x")])
    for call in (
        delegation.status(other, child.session_id),
        delegation.cancel(other, child.session_id),
        delegation.steer(other, child.session_id, "hi"),
    ):
        with pytest.raises(DelegationError, match="does not exist"):
            await call
    # Another org with the same ids sees nothing.
    stranger = me.model_copy(update={"org_id": str(uuid.uuid4())})
    with pytest.raises(DelegationError):
        await delegation.status(stranger, child.session_id)
    assert parent.run_id == uuid.UUID(me.run_id)


async def test_the_parent_run_must_be_a_run_of_the_parent_session(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    me, _ = await parent_run(queue, tenant)
    async with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="parent_run"):
            await connection.execute(
                update(Session)
                .where(Session.id == tenant.ids["s2"])
                .values(parent_run_id=uuid.UUID(me.run_id))
            )
