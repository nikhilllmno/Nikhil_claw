"""The model of a session in the config of a run (Section 5.4, roadmap M8.2)."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Run, Session
from sama.orchestrator.queue import RunQueue
from sama.orchestrator.worker import Worker
from sama.prompt import PromptAssembler
from sama.runtime.contract import RunConfig, RuntimeKind, RuntimeRegistry
from sama.runtime.fake import FakeRuntime
from tests.orchestrator.conftest import Tenant
from tests.orchestrator.test_prompt_assembly import OWNER, claimed, end
from tests.review.support import set_profile

CLAUDE = "claude-sonnet-5"
OTHER = "claude-haiku-5-5"


async def choose(engine: AsyncEngine, tenant: Tenant, model: str | None) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Session).where(Session.id == tenant.ids["s1"]).values(model_override=model)
        )


async def frozen(engine: AsyncEngine, tenant: Tenant) -> tuple[RunConfig, str]:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                select(Session.run_config, Session.config_fingerprint).where(
                    Session.id == tenant.ids["s1"]
                )
            )
        ).one()
    return RunConfig.model_validate(row.run_config), row.config_fingerprint


async def test_a_choice_before_the_first_run_applies_and_the_frozen_config_keeps_the_profile(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await set_profile(engine, tenant, runtime="claude_agent_sdk", model=CLAUDE)
    await choose(engine, tenant, OTHER)
    config = await PromptAssembler(engine)(await claimed(engine, tenant))
    assert config.model == OTHER
    stored, fingerprint = await frozen(engine, tenant)
    assert stored.model == CLAUDE
    assert fingerprint == stored.fingerprint()


async def test_a_choice_applies_from_the_next_run_and_does_not_change_the_prefix(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await set_profile(engine, tenant, runtime="claude_agent_sdk", model=CLAUDE)
    assembler = PromptAssembler(engine)
    first = await claimed(engine, tenant)
    before = await assembler(first)
    await end(engine, first)
    assert before.model == CLAUDE

    await choose(engine, tenant, OTHER)
    second = await claimed(engine, tenant)
    after = await assembler(second)
    await end(engine, second)
    assert after.model == OTHER
    assert after.render_prefix() == before.render_prefix()
    assert after.model_copy(update={"model": CLAUDE}) == before
    assert (await frozen(engine, tenant))[0] == before

    await choose(engine, tenant, None)
    third = await claimed(engine, tenant)
    assert (await assembler(third)).model == CLAUDE


async def test_the_openai_runtime_uses_the_choice_too(engine: AsyncEngine, tenant: Tenant) -> None:
    await set_profile(engine, tenant, runtime="openai_agents_sdk", model="gpt-6.2-sol")
    assembler = PromptAssembler(engine)
    first = await claimed(engine, tenant)
    assert (await assembler(first)).model == "gpt-6.2-sol"
    await end(engine, first)
    await choose(engine, tenant, "gpt-6-luna")
    assert (await assembler(await claimed(engine, tenant))).model == "gpt-6-luna"


async def test_a_model_that_the_price_table_lost_is_ignored(
    engine: AsyncEngine, tenant: Tenant, caplog: pytest.LogCaptureFixture
) -> None:
    await set_profile(engine, tenant, runtime="claude_agent_sdk", model=CLAUDE)
    assembler = PromptAssembler(engine)
    first = await claimed(engine, tenant)
    await assembler(first)
    await end(engine, first)
    await choose(engine, tenant, "claude-gone-9")
    with caplog.at_level(logging.WARNING, logger="sama.prompt"):
        config = await assembler(await claimed(engine, tenant))
    assert config.model == CLAUDE
    assert "not known" in caplog.text


async def test_a_coding_agent_run_ignores_the_choice(engine: AsyncEngine, tenant: Tenant) -> None:
    await set_profile(engine, tenant, runtime="claude_agent_sdk", model=CLAUDE)
    await choose(engine, tenant, OTHER)
    queue = RunQueue(engine)
    await queue.enqueue(
        org_id=tenant.org,
        session_id=tenant.ids["s1"],
        text="fix it",
        user_id=tenant.ids["mia"],
        coding_agent="claude_code",
    )
    run = await queue.claim(OWNER)
    assert run is not None
    config = await PromptAssembler(engine)(run)
    assert config.runtime is RuntimeKind.CLAUDE_CODE
    assert config.model == CLAUDE


async def test_the_worker_runs_with_the_chosen_model_and_records_its_fingerprint(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await set_profile(engine, tenant, runtime="fake", model=CLAUDE)
    runtime = FakeRuntime()
    registry = RuntimeRegistry()
    registry.register(runtime)
    queue = RunQueue(engine)
    worker = Worker(queue, registry, owner=OWNER, config=PromptAssembler(engine), flush_s=0.0)
    for model in (None, OTHER):
        await choose(engine, tenant, model)
        await queue.enqueue(
            org_id=tenant.org, session_id=tenant.ids["s1"], text="hi", user_id=tenant.ids["mia"]
        )
        assert await worker.run_once()
    assert [request.config.model for request in runtime.requests] == [CLAUDE, OTHER]
    async with engine.connect() as connection:
        prints = (
            (
                await connection.execute(
                    select(Run.config_fingerprint)
                    .where(Run.org_id == tenant.org)
                    .order_by(Run.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert prints == [request.config.fingerprint() for request in runtime.requests]
    assert prints[0] != prints[1]
