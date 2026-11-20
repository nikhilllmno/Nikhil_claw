"""Tests of the busy controls of the run queue: ``/steer`` notes and the queued state
(docs/architecture.md, Section 4.3)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Message, Org, Run, RunSteer
from sama.orchestrator.queue import (
    MAX_PENDING_STEERS,
    ClaimedRun,
    QueueError,
    RateLimitedError,
    RunQueue,
    steer_block,
)
from sama.runtime.contract import FailureCode, Usage
from tests.orchestrator.conftest import Tenant

OWNER = "worker-a"


async def enqueue(queue: RunQueue, tenant: Tenant, text: str = "hi") -> uuid.UUID:
    return await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids["s1"], text=text, user_id=tenant.ids["mia"]
    )


async def steer(queue: RunQueue, tenant: Tenant, text: str) -> uuid.UUID | None:
    return await queue.steer(
        org_id=tenant.org, session_id=tenant.ids["s1"], text=text, user_id=tenant.ids["mia"]
    )


async def claim(queue: RunQueue) -> ClaimedRun:
    run = await queue.claim(OWNER)
    assert run is not None
    return run


async def complete(queue: RunQueue, run: ClaimedRun) -> None:
    assert await queue.complete(run, OWNER, output="done", runtime_state=None, usage=Usage())


async def notes(engine: AsyncEngine, run_id: uuid.UUID) -> list[Any]:
    async with engine.connect() as connection:
        return list(
            (
                await connection.execute(
                    select(RunSteer.text, RunSteer.status, RunSteer.requeued_run_id)
                    .where(RunSteer.run_id == run_id)
                    .order_by(RunSteer.created_at)
                )
            ).all()
        )


async def user_text(engine: AsyncEngine, run_id: uuid.UUID) -> Any:
    async with engine.connect() as connection:
        return (
            await connection.execute(
                select(Message.content, Message.author_user_id).where(
                    Message.run_id == run_id, Message.role == "user"
                )
            )
        ).one()


async def test_a_message_behind_an_open_run_waits(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    first = await enqueue(queue, tenant)
    assert not await queue.waits(org_id=tenant.org, run_id=first)
    second = await enqueue(queue, tenant)
    assert await queue.waits(org_id=tenant.org, run_id=second)
    await claim(queue)
    assert await queue.waits(org_id=tenant.org, run_id=second)
    assert not await queue.waits(org_id=uuid.uuid4(), run_id=second)


async def test_steer_without_an_active_run_returns_none(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    assert await steer(queue, tenant, "go left") is None
    await enqueue(queue, tenant)  # queued, not active
    assert await steer(queue, tenant, "go left") is None


async def test_the_gate_delivers_the_notes_of_a_running_run_once(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    run_id = await enqueue(queue, tenant)
    run = await claim(queue)
    assert await steer(queue, tenant, "use the token sk-ant-api03-SECRETSECRETSECRET") == run_id
    assert await steer(queue, tenant, "and be brief") == run_id
    taken = await queue.take_steers(org_id=tenant.org, run_id=run_id)
    assert taken.startswith(steer_block(["x"]).split("\n\n")[0])
    assert taken.index("use the token") < taken.index("and be brief")
    assert "SECRETSECRET" not in taken
    assert await queue.take_steers(org_id=tenant.org, run_id=run_id) == ""
    assert [row.status for row in await notes(engine, run_id)] == ["delivered", "delivered"]
    await complete(queue, run)
    # Nothing is left, so no new run.
    assert await queue.requeued_run(org_id=tenant.org, run_id=run_id) is None


async def test_the_notes_of_another_org_are_not_taken(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    run_id = await enqueue(queue, tenant)
    await claim(queue)
    await steer(queue, tenant, "go left")
    assert await queue.take_steers(org_id=uuid.uuid4(), run_id=run_id) == ""
    assert [row.status for row in await notes(engine, run_id)] == ["pending"]


async def test_a_paused_run_keeps_its_notes_for_the_resume(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    run_id = await enqueue(queue, tenant)
    run = await claim(queue)
    assert await queue.pause(
        run,
        OWNER,
        approval_id="call-1",
        tool_name="Bash",
        arguments={"command": "ls"},
        reason="r",
        runtime_state=None,
        usage=Usage(),
    )
    assert await steer(queue, tenant, "after the approval, stop") == run_id
    assert await queue.take_steers(org_id=tenant.org, run_id=run_id) == ""
    assert [row.status for row in await notes(engine, run_id)] == ["pending"]


async def test_notes_that_the_run_did_not_read_become_the_next_message(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    run_id = await enqueue(queue, tenant)
    later = await enqueue(queue, tenant, "a queued question")
    run = await claim(queue)
    await steer(queue, tenant, "one")
    await steer(queue, tenant, "two")
    await complete(queue, run)
    follow = await queue.requeued_run(org_id=tenant.org, run_id=run_id)
    assert follow is not None
    assert follow != later
    rows = await notes(engine, run_id)
    assert [(row.status, row.requeued_run_id) for row in rows] == [("requeued", follow)] * 2
    content, author = await user_text(engine, follow)
    assert (content, author) == ({"text": "one\n\ntwo"}, tenant.ids["mia"])
    # The queued question keeps its place: it runs before the notes.
    assert (await claim(queue)).run_id == later


async def test_a_user_over_the_rate_cannot_steer_but_the_requeue_skips_the_check(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    async with engine.begin() as connection:
        await connection.execute(
            update(Org)
            .where(Org.id == tenant.org)
            .values(limits={"user_messages_per_minute": 1, "org_messages_per_minute": 100})
        )
    run_id = await enqueue(queue, tenant)
    run = await claim(queue)
    with pytest.raises(RateLimitedError):
        await steer(queue, tenant, "one")
    # A note from before the limit: the requeue does not check the rate again.
    async with engine.begin() as connection:
        await connection.execute(
            insert(RunSteer).values(
                org_id=tenant.org, session_id=tenant.ids["s1"], run_id=run_id, text="early"
            )
        )
    await complete(queue, run)
    assert await queue.requeued_run(org_id=tenant.org, run_id=run_id) is not None


async def test_a_run_takes_at_most_the_pending_limit(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    await enqueue(queue, tenant)
    await claim(queue)
    for index in range(MAX_PENDING_STEERS):
        await steer(queue, tenant, f"note {index}")
    with pytest.raises(QueueError, match="notes"):
        await steer(queue, tenant, "one more")


@pytest.mark.parametrize("text", ["", "   ", "x" * 4001])
async def test_a_note_needs_a_text_of_a_valid_size(
    engine: AsyncEngine, tenant: Tenant, text: str
) -> None:
    queue = RunQueue(engine)
    await enqueue(queue, tenant)
    await claim(queue)
    with pytest.raises(QueueError):
        await steer(queue, tenant, text)


async def test_the_sweep_drops_the_notes_of_a_failed_run(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    run_id = await enqueue(queue, tenant)
    run = await claim(queue)
    await steer(queue, tenant, "go left")
    assert await queue.fail(
        run, OWNER, code=FailureCode.CANCELLED, message="stopped", usage=Usage()
    )
    assert await queue.sweep_steers() == 1
    assert [row.status for row in await notes(engine, run_id)] == ["dropped"]
    assert await queue.sweep_steers() == 0


async def test_the_sweep_sends_the_notes_that_complete_left(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    run_id = await enqueue(queue, tenant)
    run = await claim(queue)
    await complete(queue, run)
    # A note that came in after the check of ``complete``: the run is completed now.
    async with engine.begin() as connection:
        await connection.execute(
            insert(RunSteer).values(
                org_id=tenant.org,
                session_id=tenant.ids["s1"],
                run_id=run_id,
                user_id=tenant.ids["mia"],
                text="late",
            )
        )
    assert await queue.sweep_steers() == 1
    follow = await queue.requeued_run(org_id=tenant.org, run_id=run_id)
    assert follow is not None
    assert (await user_text(engine, follow))[0] == {"text": "late"}
    async with engine.connect() as connection:
        status = (await connection.execute(select(Run.status).where(Run.id == follow))).scalar_one()
    assert status == "queued"


async def test_a_run_that_stops_takes_no_notes(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    run_id = await enqueue(queue, tenant)
    await claim(queue)
    await queue.cancel(org_id=tenant.org, run_id=run_id)
    assert await steer(queue, tenant, "too late") is None
