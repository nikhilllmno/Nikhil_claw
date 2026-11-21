"""Busy modes and parallel runs in the run queue (docs/architecture.md, Section 4.3, M7.1)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AgentProfile, Message, Run
from sama.gateway.sessions import busy_text, stop_reply
from sama.orchestrator.queue import ClaimedRun, RunQueue
from sama.runtime.contract import FailureCode, Usage
from tests.orchestrator.conftest import Tenant

OWNER = "worker-a"


async def set_mode(engine: AsyncEngine, tenant: Tenant, mode: str, limit: int = 3) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == tenant.ids["profile"])
            .values(busy_mode=mode, max_parallel_runs=limit)
        )


async def send(
    queue: RunQueue, tenant: Tenant, text: str, *, session: str = "s1", parallel: bool = False
) -> uuid.UUID:
    return await queue.enqueue(
        org_id=tenant.org,
        session_id=tenant.ids[session],
        text=text,
        user_id=tenant.ids["mia"],
        parallel=parallel,
    )


async def claim(queue: RunQueue) -> ClaimedRun:
    run = await queue.claim(OWNER)
    assert run is not None
    return run


async def finish(queue: RunQueue, run: ClaimedRun, output: str, state: bytes | None) -> None:
    assert await queue.complete(run, OWNER, output=output, runtime_state=state, usage=Usage())


async def row(engine: AsyncEngine, run_id: uuid.UUID) -> Any:
    async with engine.connect() as connection:
        return (await connection.execute(select(Run).where(Run.id == run_id))).one()


async def test_the_default_mode_is_steer_and_a_second_message_waits(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await send(queue, tenant, "first")
    first = await claim(queue)
    second = await send(queue, tenant, "second")
    assert (await row(engine, second)).allow_parallel is False
    assert await queue.claim(OWNER) is None
    assert await queue.waits(org_id=tenant.org, run_id=second)
    assert first.side is False


async def test_the_parallel_mode_starts_a_side_run_with_the_transcript(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await send(queue, tenant, "hello")
    old = await claim(queue)
    await finish(queue, old, "hello back", b"state-1")
    await set_mode(engine, tenant, "parallel")
    await send(queue, tenant, "write the long report")
    slow = await claim(queue)
    assert slow.side is False
    assert slow.runtime_state == b"state-1"
    quick = await send(queue, tenant, "what time is it in Paris?")
    assert not await queue.waits(org_id=tenant.org, run_id=quick)
    side = await claim(queue)
    assert side.run_id == quick
    assert side.side is True
    assert side.runtime_state is None
    assert "[user] hello" in side.input
    assert "[assistant] hello back" in side.input
    assert "- write the long report" in side.input
    assert side.input.endswith("The new request:\nwhat time is it in Paris?")
    stored = await row(engine, quick)
    assert stored.side is True
    assert stored.context_seq is not None


async def test_bg_starts_a_side_run_in_the_steer_mode(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    await send(queue, tenant, "main task")
    await claim(queue)
    background = await send(queue, tenant, "background task", parallel=True)
    side = await claim(queue)
    assert side.run_id == background
    assert side.side is True


async def test_the_limit_of_the_profile_holds_further_runs(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await set_mode(engine, tenant, "parallel", limit=2)
    await send(queue, tenant, "one")
    first = await claim(queue)
    await send(queue, tenant, "two")
    await claim(queue)
    third = await send(queue, tenant, "three")
    assert await queue.claim(OWNER) is None
    assert await queue.waits(org_id=tenant.org, run_id=third)
    await finish(queue, first, "done one", b"s")
    started = await claim(queue)
    assert started.run_id == third
    assert started.side is True


async def test_a_side_run_does_not_give_its_state_and_the_main_run_catches_up(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await set_mode(engine, tenant, "parallel")
    await send(queue, tenant, "slow request")
    slow = await claim(queue)
    await send(queue, tenant, "quick request")
    quick = await claim(queue)
    assert quick.side
    # The quick run answers first, then the slow run.
    await finish(queue, quick, "quick answer", b"side-state")
    await finish(queue, slow, "slow answer", b"main-state")
    await send(queue, tenant, "next request")
    following = await claim(queue)
    assert following.side is False
    assert following.runtime_state == b"main-state"
    assert "<parallel_answers>" in following.input
    assert "[user] quick request\n[assistant] quick answer" in following.input
    assert following.input.endswith("next request")
    await finish(queue, following, "next answer", b"main-state-2")
    # The state of the next run has the catch-up, so the run after it gets none.
    await send(queue, tenant, "and then")
    after = await claim(queue)
    assert after.runtime_state == b"main-state-2"
    assert "<parallel_answers>" not in after.input
    assert after.input == "and then"


async def test_the_replies_join_the_transcript_in_the_order_they_end(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await set_mode(engine, tenant, "parallel")
    await send(queue, tenant, "slow")
    slow = await claim(queue)
    await send(queue, tenant, "quick")
    quick = await claim(queue)
    await finish(queue, quick, "quick answer", None)
    await finish(queue, slow, "slow answer", None)

    async with engine.connect() as connection:
        texts = (
            await connection.execute(
                select(Message.role, Message.content["text"].astext)
                .where(Message.session_id == tenant.ids["s1"])
                .order_by(Message.seq)
            )
        ).all()
    assert [tuple(t) for t in texts] == [
        ("user", "slow"),
        ("user", "quick"),
        ("assistant", "quick answer"),
        ("assistant", "slow answer"),
    ]


async def test_a_review_waits_for_the_side_runs(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    await set_mode(engine, tenant, "parallel")
    await send(queue, tenant, "one")
    first = await claim(queue)
    await send(queue, tenant, "two")
    side = await claim(queue)
    await finish(queue, first, "a", b"s")
    async with engine.begin() as connection:
        await connection.execute(
            insert(Run).values(
                org_id=tenant.org,
                session_id=tenant.ids["s1"],
                agent_profile_id=tenant.ids["profile"],
                runtime="fake",
                kind="review",
                unattended=True,
                review_from_seq=0,
                review_to_seq=1,
            )
        )
    assert await queue.claim(OWNER) is None
    await finish(queue, side, "b", None)
    review = await claim(queue)
    assert review.kind == "review"


async def test_a_paused_main_run_resumes_next_to_a_side_run(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await set_mode(engine, tenant, "parallel")
    await send(queue, tenant, "deploy")
    main = await claim(queue)
    assert (
        await queue.pause(
            main,
            OWNER,
            approval_id="call-1",
            tool_name="Bash",
            arguments={"command": "make deploy"},
            reason="deploys",
            runtime_state=b"paused",
            usage=Usage(),
        )
        == "paused"
    )
    await send(queue, tenant, "meanwhile")
    side = await claim(queue)
    assert side.side
    async with engine.begin() as connection:
        await connection.execute(
            update(Run)
            .where(Run.id == main.run_id)
            .values(status="queued", resume_decision="approve")
        )
    resumed = await claim(queue)
    assert resumed.run_id == main.run_id
    assert resumed.resume is not None
    assert resumed.side is False
    assert resumed.runtime_state == b"paused"


async def test_a_message_joins_the_running_run_in_the_steer_mode(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    session = tenant.ids["s1"]
    assert (
        await queue.steer(org_id=tenant.org, session_id=session, text="x", busy_mode=True) is None
    )
    await send(queue, tenant, "task")
    run = await claim(queue)
    joined = await queue.steer(
        org_id=tenant.org, session_id=session, text="also this", busy_mode=True
    )
    assert joined == run.run_id
    assert "also this" in await queue.take_steers(org_id=tenant.org, run_id=run.run_id)
    # A message longer than a note does not join: it starts or queues a run.
    long = "x" * 5000
    assert (
        await queue.steer(org_id=tenant.org, session_id=session, text=long, busy_mode=True) is None
    )
    # In the parallel mode, a message never joins.
    await set_mode(engine, tenant, "parallel")
    assert (
        await queue.steer(org_id=tenant.org, session_id=session, text="y", busy_mode=True) is None
    )


async def test_a_note_goes_to_the_newest_open_run(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    await send(queue, tenant, "one")
    await claim(queue)
    await send(queue, tenant, "two", parallel=True)
    newest = await claim(queue)
    steered = await queue.steer(org_id=tenant.org, session_id=tenant.ids["s1"], text="note")
    assert steered == newest.run_id


async def test_stop_stops_one_run_or_all_runs(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    await set_mode(engine, tenant, "parallel")
    session = tenant.ids["s1"]
    await send(queue, tenant, "first task")
    first = await claim(queue)
    await send(queue, tenant, "second task")
    second = await claim(queue)
    runs = await queue.open_runs(org_id=tenant.org, session_id=session)
    assert [(r.number, r.run_id, r.side) for r in runs] == [
        (1, first.run_id, False),
        (2, second.run_id, True),
    ]
    notice = await busy_text(
        queue, tenant.org, session, second.run_id, steer="/steer", stop="/stop"
    )
    assert notice is not None
    assert "/stop 2 stops only this run" in notice
    wrong = await stop_reply(engine, queue, tenant.org, session, "7")
    assert "Open runs:" in wrong
    assert "2. working - second task" in wrong
    one = await stop_reply(engine, queue, tenant.org, session, "2")
    assert one == "Stopped run 2: second task"
    assert (await row(engine, second.run_id)).cancel_requested_at is not None
    assert (await row(engine, first.run_id)).cancel_requested_at is None
    await send(queue, tenant, "third task")
    assert await stop_reply(engine, queue, tenant.org, session, "") == "Stopped 3 runs."
    assert await stop_reply(engine, queue, tenant.org, None, "") == "No run is open now."


async def test_claims_at_the_same_time_keep_the_limit(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    await set_mode(engine, tenant, "parallel", limit=2)
    for n in range(4):
        await send(queue, tenant, f"task {n}")
    claimed: list[uuid.UUID] = []
    for _ in range(4):
        results = await asyncio.gather(*(queue.claim(f"w{n}") for n in range(6)))
        claimed += [run.run_id for run in results if run is not None]
    assert len(claimed) == 2
    async with engine.connect() as connection:
        running = (
            await connection.execute(
                select(Run.id, Run.side).where(
                    Run.session_id == tenant.ids["s1"], Run.status == "running"
                )
            )
        ).all()
    assert sorted(r.side for r in running) == [False, True]


async def test_a_failed_main_run_does_not_lose_the_catch_up(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await set_mode(engine, tenant, "parallel")
    await send(queue, tenant, "slow")
    slow = await claim(queue)
    await send(queue, tenant, "quick")
    quick = await claim(queue)
    await finish(queue, quick, "quick answer", None)
    await finish(queue, slow, "slow answer", b"main-1")
    await send(queue, tenant, "next")
    failing = await claim(queue)
    assert "quick answer" in failing.input
    # The failure state is the conversation before the run, without the catch-up.
    assert await queue.fail(
        failing,
        OWNER,
        code=FailureCode.MODEL_ERROR,
        message="boom",
        usage=Usage(),
        runtime_state=b"main-1-kept",
    )
    await send(queue, tenant, "again")
    retry = await claim(queue)
    assert retry.runtime_state == b"main-1-kept"
    assert "quick answer" in retry.input


async def test_a_compaction_does_not_consume_the_catch_up(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await set_mode(engine, tenant, "parallel")
    await send(queue, tenant, "slow")
    slow = await claim(queue)
    await send(queue, tenant, "quick")
    quick = await claim(queue)
    await finish(queue, quick, "quick answer", None)
    await finish(queue, slow, "slow answer", b"main-1")
    await queue.enqueue_compact(
        org_id=tenant.org, session_id=tenant.ids["s1"], user_id=tenant.ids["mia"]
    )
    compact = await claim(queue)
    assert compact.compact
    await finish(queue, compact, "", b"compacted")
    await send(queue, tenant, "after the compaction")
    following = await claim(queue)
    assert following.runtime_state == b"compacted"
    assert "quick answer" in following.input
