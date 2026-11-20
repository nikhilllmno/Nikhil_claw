"""A profile switches runtime while runs wait in the queue (docs/architecture.md, Section 11.4).
The claim gives a run a runtime and a model that fit together."""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Session
from sama.orchestrator.queue import RunQueue
from sama.runtime.contract import RunConfig, RuntimeKind
from tests.orchestrator.conftest import Tenant
from tests.review.support import OWNER, complete_chat, set_counters, set_profile

OPENAI = {"runtime": "openai_agents_sdk", "model": "gpt-6.2-sol"}


async def _enqueue(queue: RunQueue, tenant: Tenant) -> None:
    await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids["s1"], text="hi", user_id=tenant.ids["mia"]
    )


async def test_a_first_run_takes_the_runtime_of_the_profile_at_the_claim(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await set_profile(engine, tenant, runtime="claude_agent_sdk", model="claude-sonnet-4-5")
    await _enqueue(queue, tenant)
    # The admin switches the runtime while the first run of the session waits.
    await set_profile(engine, tenant, **OPENAI)
    run = await queue.claim(OWNER)
    assert run is not None
    assert (run.runtime, run.model) == ("openai_agents_sdk", "gpt-6.2-sol")


async def test_a_frozen_session_keeps_its_runtime_at_the_claim(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    config = RunConfig(runtime=RuntimeKind.CLAUDE_AGENT_SDK, model="claude-sonnet-4-5", soul="x")
    async with engine.begin() as connection:
        await connection.execute(
            update(Session)
            .where(Session.id == tenant.ids["s1"])
            .values(run_config=config.model_dump(mode="json"), config_fingerprint="f-1")
        )
    await set_profile(engine, tenant, runtime="claude_agent_sdk", model="claude-sonnet-4-5")
    await _enqueue(queue, tenant)
    await set_profile(engine, tenant, **OPENAI)
    run = await queue.claim(OWNER)
    assert run is not None
    assert run.runtime == "claude_agent_sdk"


async def test_a_review_model_of_another_runtime_falls_back_to_the_default(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await set_profile(
        engine,
        tenant,
        runtime="claude_agent_sdk",
        model="claude-sonnet-4-5",
        review_model="claude-sonnet-4-6",
    )
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant, queue=queue)
    # The switch comes after the review is in the queue, with a review model of the new runtime.
    await set_profile(engine, tenant, **OPENAI, review_model="gpt-6.2-astra")
    review = await queue.claim(OWNER)
    assert review is not None
    assert review.kind == "review"
    assert (review.runtime, review.model) == ("claude_agent_sdk", "claude-haiku-4-5")


async def test_a_review_model_of_the_runtime_comes_first(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await set_profile(
        engine,
        tenant,
        runtime="claude_agent_sdk",
        model="claude-sonnet-4-5",
        review_model="claude-sonnet-4-6",
    )
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant, queue=queue)
    review = await queue.claim(OWNER)
    assert review is not None
    assert review.model == "claude-sonnet-4-6"
