"""When a run ends, sama counts and queues a background review (docs/architecture.md,
Section 9.4)."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import Message, Run, Session
from sama.orchestrator.queue import ClaimedRun, RunQueue
from sama.review.policy import MAX_COST_USD, MAX_TURNS, MAX_WALL_TIME_S
from sama.review.trigger import after_completed_run, review_budget
from sama.runtime.contract import Usage
from tests.orchestrator.conftest import Tenant
from tests.review.support import (
    OWNER,
    complete_chat,
    counters,
    fail_chat,
    reviews,
    set_counters,
    set_limits,
    set_profile,
    spend,
)

pytestmark = pytest.mark.usefixtures("tenant")


async def max_seq(engine: AsyncEngine, tenant: Tenant, session: str = "s1") -> int:
    async with engine.connect() as connection:
        value = (
            await connection.execute(
                select(Message.seq)
                .where(Message.session_id == tenant.ids[session])
                .order_by(Message.seq.desc())
                .limit(1)
            )
        ).scalar_one()
    return int(value)


# -- counters and thresholds ---------------------------------------------------------------


async def test_ten_user_turns_queue_one_review_and_reset_the_counters(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    for turn in range(1, 10):
        await complete_chat(engine, tenant)
        assert await counters(engine, tenant) == (turn, 0, 0)
        assert await reviews(engine, tenant) == []
    await complete_chat(engine, tenant)
    (review,) = await reviews(engine, tenant)
    assert (review.status, review.unattended, review.triggered_by_user_id) == (
        "queued",
        True,
        None,
    )
    last = await max_seq(engine, tenant)
    assert (review.review_from_seq, review.review_to_seq) == (0, last)
    assert await counters(engine, tenant) == (0, 0, last)


async def test_ten_tool_iterations_queue_a_review(engine: AsyncEngine, tenant: Tenant) -> None:
    await complete_chat(engine, tenant, tools=9)
    assert await counters(engine, tenant) == (1, 9, 0)
    assert await reviews(engine, tenant) == []
    await complete_chat(engine, tenant, tools=1)
    assert len(await reviews(engine, tenant)) == 1
    assert (await counters(engine, tenant))[:2] == (0, 0)


async def test_one_run_with_many_tool_calls_queues_a_review(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await complete_chat(engine, tenant, tools=12)
    assert len(await reviews(engine, tenant)) == 1


async def test_a_second_range_starts_after_the_first(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant)
    first_end = await max_seq(engine, tenant)
    review = await queue.claim(OWNER)
    assert review is not None
    assert review.kind == "review"
    assert await queue.complete(review, OWNER, output="", runtime_state=None, usage=Usage())
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant)
    second = (await reviews(engine, tenant))[1]
    assert (second.review_from_seq, second.review_to_seq) == (
        first_end,
        await max_seq(engine, tenant),
    )


async def test_failed_and_cancelled_runs_do_not_count(engine: AsyncEngine, tenant: Tenant) -> None:
    await fail_chat(engine, tenant)
    queue = RunQueue(engine)
    run_id = await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids["s1"], text="x", user_id=tenant.ids["mia"]
    )
    assert await queue.cancel(org_id=tenant.org, run_id=run_id) == "cancelled"
    assert await counters(engine, tenant) == (0, 0, 0)


async def test_a_run_of_another_session_counts_for_its_own_session(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await complete_chat(engine, tenant, "s1", tools=4)
    await complete_chat(engine, tenant, "s2")
    assert (await counters(engine, tenant, "s1"))[:2] == (1, 4)
    assert (await counters(engine, tenant, "s2"))[:2] == (1, 0)


# -- the settings that switch it off ----------------------------------------------------------


async def test_a_profile_with_the_setting_off_is_not_counted(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await set_profile(engine, tenant, background_review=False)
    for _ in range(11):
        await complete_chat(engine, tenant, tools=2)
    assert await counters(engine, tenant) == (0, 0, 0)
    assert await reviews(engine, tenant) == []


async def test_the_setting_is_on_by_default(engine: AsyncEngine, tenant: Tenant) -> None:
    async with engine.connect() as connection:
        from sama.db.models import AgentProfile

        row = (
            await connection.execute(
                select(AgentProfile.background_review, AgentProfile.review_model).where(
                    AgentProfile.id == tenant.ids["profile"]
                )
            )
        ).one()
    assert (row.background_review, row.review_model) == (True, None)


async def test_a_delegated_session_is_not_reviewed(engine: AsyncEngine, tenant: Tenant) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Session)
            .where(Session.id == tenant.ids["s2"])
            .values(parent_session_id=tenant.ids["s1"], lineage="delegation")
        )
    for _ in range(10):
        await complete_chat(engine, tenant, "s2")
    assert await counters(engine, tenant, "s2") == (0, 0, 0)
    assert await reviews(engine, tenant, "s2") == []


# -- recursion is off -------------------------------------------------------------------------


async def test_a_review_run_does_not_count_and_never_queues_a_review(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await set_counters(engine, tenant, turns=9, tools=9)
    await complete_chat(engine, tenant)
    queue = RunQueue(engine)
    review = await queue.claim(OWNER)
    assert review is not None
    assert review.kind == "review"
    await set_counters(engine, tenant, turns=9, tools=9)
    assert await queue.complete(review, OWNER, output="saved", runtime_state=None, usage=Usage())
    assert await counters(engine, tenant) == (9, 9, await max_seq(engine, tenant))
    assert len(await reviews(engine, tenant)) == 1
    async with engine.begin() as connection:
        assert await after_completed_run(connection, review) is None
    assert len(await reviews(engine, tenant)) == 1


async def test_a_review_run_writes_no_message(engine: AsyncEngine, tenant: Tenant) -> None:
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant)
    before = await max_seq(engine, tenant)
    queue = RunQueue(engine)
    review = await queue.claim(OWNER)
    assert review is not None
    assert await queue.complete(review, OWNER, output="saved", runtime_state=None, usage=Usage())
    assert await max_seq(engine, tenant) == before


# -- at most one running and one waiting -----------------------------------------------------


async def test_a_waiting_review_takes_the_next_trigger_instead_of_a_second_review(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant)
    (waiting,) = await reviews(engine, tenant)
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant)
    (again,) = await reviews(engine, tenant)
    assert again.id == waiting.id
    assert again.review_from_seq == waiting.review_from_seq
    assert again.review_to_seq == await max_seq(engine, tenant)
    assert again.review_to_seq > waiting.review_to_seq


async def test_one_review_runs_and_one_waits(engine: AsyncEngine, tenant: Tenant) -> None:
    """A running review keeps its session, so no chat run ends meanwhile and no trigger runs.
    The rule holds for any writer: a second waiting review is allowed, a third is not."""
    queue = RunQueue(engine)
    await set_counters(engine, tenant, turns=9)
    run = await complete_chat(engine, tenant)
    running = await queue.claim(OWNER)
    assert running is not None
    assert running.kind == "review"
    await set_counters(engine, tenant, turns=9)
    async with engine.begin() as connection:
        waiting = await _insert_review(connection, run)
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await _insert_review(connection, run)
    assert await queue.claim("w-other") is None, "the session has a running review"
    assert await queue.complete(running, OWNER, output="", runtime_state=None, usage=Usage())
    second = await queue.claim("w-other")
    assert second is not None
    assert (second.kind, second.run_id) == ("review", waiting)


async def _insert_review(connection: AsyncConnection, run: ClaimedRun) -> uuid.UUID:
    return (
        await connection.execute(
            insert(Run)
            .values(
                org_id=run.org_id,
                session_id=run.session_id,
                agent_profile_id=run.agent_profile_id,
                runtime=run.runtime,
                unattended=True,
                kind="review",
                review_from_seq=0,
                review_to_seq=1,
            )
            .returning(Run.id)
        )
    ).scalar_one()


async def test_concurrent_triggers_queue_one_review(engine: AsyncEngine, tenant: Tenant) -> None:
    """Twelve runs of one session end at the same time. The lock of the session row puts them
    in a sequence: the tenth reaches the threshold, the other two count after the reset."""
    queue = RunQueue(engine)
    run = await complete_chat(engine, tenant, queue=queue)
    await set_counters(engine, tenant, turns=0)

    async def end() -> object:
        async with engine.begin() as connection:
            return await after_completed_run(connection, run)

    results = await asyncio.gather(*(end() for _ in range(12)))
    assert len([r for r in results if r is not None]) == 1
    assert len(await reviews(engine, tenant)) == 1
    assert (await counters(engine, tenant))[:2] == (2, 0)


async def test_concurrent_triggers_at_the_threshold_queue_one_review(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    run = await complete_chat(engine, tenant)
    await set_counters(engine, tenant, turns=9)

    async def end() -> object:
        async with engine.begin() as connection:
            return await after_completed_run(connection, run)

    await asyncio.gather(*(end() for _ in range(8)))
    assert len(await reviews(engine, tenant)) == 1
    assert (await counters(engine, tenant))[:2] == (7, 0)


async def test_the_index_allows_one_waiting_review_per_session(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    """The unique index is the second guard, for a writer that skips the session lock."""
    run = await complete_chat(engine, tenant)

    async def add() -> None:
        async with engine.begin() as connection:
            await _insert_review(connection, run)

    results = await asyncio.gather(*(add() for _ in range(5)), return_exceptions=True)
    assert len([r for r in results if r is None]) == 1
    assert all(isinstance(r, IntegrityError) for r in results if r is not None)
    assert len(await reviews(engine, tenant)) == 1


async def test_two_sessions_each_have_their_own_review(engine: AsyncEngine, tenant: Tenant) -> None:
    for session in ("s1", "s2"):
        await set_counters(engine, tenant, session, turns=9)
        await complete_chat(engine, tenant, session)
    assert len(await reviews(engine, tenant, "s1")) == 1
    assert len(await reviews(engine, tenant, "s2")) == 1


# -- order of claims --------------------------------------------------------------------------


async def test_a_chat_run_goes_before_a_waiting_review(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant)
    chat_id = await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids["s1"], text="next", user_id=tenant.ids["mia"]
    )
    first = await queue.claim(OWNER)
    assert first is not None
    assert (first.kind, first.run_id) == ("chat", chat_id)
    assert await queue.claim("w-other") is None, "the review waits for the idle session"
    assert await queue.complete(first, OWNER, output="x", runtime_state=None, usage=Usage())
    second = await queue.claim(OWNER)
    assert second is not None
    assert second.kind == "review"


async def test_a_review_run_does_not_block_a_new_chat_message(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant)
    review = await queue.claim(OWNER)
    assert review is not None
    run_id = await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids["s1"], text="next", user_id=tenant.ids["mia"]
    )
    assert run_id is not None
    assert await queue.claim("w-other") is None
    assert await queue.complete(review, OWNER, output="", runtime_state=None, usage=Usage())
    chat = await queue.claim("w-other")
    assert chat is not None
    assert (chat.kind, chat.run_id) == ("chat", run_id)


async def test_the_runtime_state_comes_from_the_last_chat_run(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant, queue=queue, runtime_state=b"chat-state")
    review = await queue.claim(OWNER)
    assert review is not None
    assert review.runtime_state is None
    assert await queue.complete(
        review, OWNER, output="", runtime_state=b"review-state", usage=Usage()
    )
    await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids["s1"], text="next", user_id=tenant.ids["mia"]
    )
    chat = await queue.claim(OWNER)
    assert chat is not None
    assert chat.runtime_state == b"chat-state"


# -- the budget -------------------------------------------------------------------------------


async def test_a_spent_month_skips_the_review_and_keeps_the_counters(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await set_limits(engine, tenant, {"model_usd_per_month": "1.00"})
    run = await complete_chat(engine, tenant)
    await spend(engine, tenant, run, "1.00")
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant)
    assert await reviews(engine, tenant) == []
    assert (await counters(engine, tenant))[:2] == (10, 0)
    await set_limits(engine, tenant, {"model_usd_per_month": "5.00"})
    await complete_chat(engine, tenant)
    assert len(await reviews(engine, tenant)) == 1
    assert (await counters(engine, tenant))[:2] == (0, 0)


async def test_a_spent_day_of_the_user_skips_the_review(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await set_limits(engine, tenant, {"model_usd_per_user_day": "0.50"})
    run = await complete_chat(engine, tenant, "mine")
    await spend(engine, tenant, run, "0.60", user=tenant.ids["mia"])
    await set_counters(engine, tenant, "mine", turns=9)
    await complete_chat(engine, tenant, "mine")
    assert await reviews(engine, tenant, "mine") == []
    assert (await counters(engine, tenant, "mine"))[:2] == (10, 0)


async def test_the_day_limit_of_a_user_does_not_stop_the_review_of_another_user(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await set_limits(engine, tenant, {"model_usd_per_user_day": "0.50"})
    run = await complete_chat(engine, tenant, "mine")
    await spend(engine, tenant, run, "0.60", user=tenant.ids["mia"])
    # A shared session has no user for the day limit.
    await set_counters(engine, tenant, "s1", turns=9)
    await complete_chat(engine, tenant, "s1")
    assert len(await reviews(engine, tenant, "s1")) == 1


async def test_a_spend_below_the_limit_does_not_skip_the_review(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await set_limits(engine, tenant, {"model_usd_per_month": "1.00"})
    run = await complete_chat(engine, tenant)
    await spend(engine, tenant, run, "0.99")
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant)
    assert len(await reviews(engine, tenant)) == 1


async def test_the_review_budget_is_the_lower_of_the_profile_and_the_caps(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    assert review_budget({}) == {
        "max_turns": MAX_TURNS,
        "max_wall_time_s": MAX_WALL_TIME_S,
        "max_cost_usd": str(MAX_COST_USD),
    }
    low = review_budget({"max_turns": 3, "max_wall_time_s": 60, "max_cost_usd": "0.10"})
    assert (low["max_turns"], low["max_wall_time_s"], Decimal(low["max_cost_usd"])) == (
        3,
        60.0,
        Decimal("0.10"),
    )
    high = review_budget({"max_turns": 500, "max_wall_time_s": 9000, "max_cost_usd": "20"})
    assert (high["max_turns"], high["max_wall_time_s"]) == (MAX_TURNS, MAX_WALL_TIME_S)
    assert Decimal(high["max_cost_usd"]) == MAX_COST_USD
    assert review_budget({"max_tokens": 5000})["max_tokens"] == 5000
    assert "max_tokens" not in review_budget({"max_tokens": -1})
    assert review_budget("bad")["max_turns"] == MAX_TURNS
    # The budget is on the run.
    await set_profile(engine, tenant, budget={"max_cost_usd": "0.10"})
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant)
    (review,) = await reviews(engine, tenant)
    assert Decimal(review.budget["max_cost_usd"]) == Decimal("0.10")


# -- the other queue rules ignore review runs --------------------------------------------------


async def test_review_runs_do_not_count_for_the_rate_limit(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await set_limits(engine, tenant, {"org_messages_per_minute": 3})
    queue = RunQueue(engine)
    for _ in range(2):
        await complete_chat(engine, tenant, queue=queue)
    async with engine.begin() as connection:
        for _ in range(2):
            await connection.execute(
                insert(Run).values(
                    org_id=tenant.org,
                    session_id=tenant.ids["s2"],
                    agent_profile_id=tenant.ids["profile"],
                    runtime="fake",
                    unattended=True,
                    kind="review",
                    status="completed",
                    review_from_seq=0,
                    review_to_seq=0,
                )
            )
    await complete_chat(engine, tenant, queue=queue)
