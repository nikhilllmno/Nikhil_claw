"""The input, the config and the model of a review run (docs/architecture.md, Section 9.4)."""

from __future__ import annotations

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Message, Session
from sama.orchestrator.queue import RunQueue
from sama.prompt import SYSTEM_PROMPT, PromptAssembler
from sama.review.policy import MESSAGE_MAX_CHARS, TRANSCRIPT_MAX_CHARS
from sama.review.policy import SYSTEM_PROMPT as REVIEW_SYSTEM_PROMPT
from sama.review.transcript import HEADER
from sama.runtime.contract import RunConfig, RuntimeKind, Usage
from tests.orchestrator.conftest import Tenant
from tests.review.support import OWNER, complete_chat, set_counters, set_profile
from tests.review.test_writes import TOOLSETS

pytestmark = pytest.mark.usefixtures("tenant")

SECRET = "ghp_" + "a1B2" * 9


async def claim_review(engine: AsyncEngine, queue: RunQueue | None = None) -> object:
    run = await (queue or RunQueue(engine)).claim(OWNER)
    assert run is not None
    assert run.kind == "review"
    return run


async def test_the_review_reads_only_the_new_part_of_the_transcript(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await complete_chat(engine, tenant, text="old question", output="old answer", queue=queue)
    async with engine.begin() as connection:
        cursor = (
            await connection.execute(
                select(Message.seq)
                .order_by(Message.seq.desc())
                .where(Message.org_id == tenant.org)
                .limit(1)
            )
        ).scalar_one()
        await connection.execute(
            update(Session).where(Session.id == tenant.ids["s1"]).values(review_cursor_seq=cursor)
        )
    await set_counters(engine, tenant, turns=9)
    run = await complete_chat(
        engine, tenant, text="how do we deploy", output="run make deploy", tools=2, queue=queue
    )
    review = await queue.claim(OWNER)
    assert review is not None
    assert review.kind == "review"
    assert review.input.startswith(HEADER)
    assert "old question" not in review.input
    assert "[user] how do we deploy" in review.input
    assert "[assistant] run make deploy" in review.input
    assert "(tools of this turn: Read, Bash)" in review.input
    assert (review.user_id, review.soul, review.runtime_state, review.resume) == (
        None,
        "",
        None,
        None,
    )
    assert review.run_id != run.run_id


async def test_the_transcript_is_redacted_and_bounded(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant, text=f"my token is {SECRET}", output="ok", queue=queue)
    review = await queue.claim(OWNER)
    assert review is not None
    assert SECRET not in review.input
    assert "[user] my token is" in review.input


async def test_a_long_message_is_cut_and_the_oldest_messages_go_first(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    big = "x" * (MESSAGE_MAX_CHARS * 3)
    async with engine.begin() as connection:
        for n in range(20):
            await connection.execute(
                insert(Message).values(
                    org_id=tenant.org,
                    session_id=tenant.ids["s1"],
                    role="user",
                    content={"text": f"{n:02d} {big}"},
                )
            )
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant, text="last one", queue=queue)
    review = await queue.claim(OWNER)
    assert review is not None
    assert len(review.input) <= len(HEADER) + TRANSCRIPT_MAX_CHARS + 200
    assert "[older messages are not shown]" in review.input
    assert "[user] last one" in review.input
    assert "[user] 00 " not in review.input
    assert "x" * (MESSAGE_MAX_CHARS + 1) not in review.input


async def test_the_review_model_is_the_cheap_default_of_the_runtime(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant)
    review = await claim_review(engine)
    assert review.model == "fake-review"  # type: ignore[attr-defined]
    assert review.model != "fake-model"  # type: ignore[attr-defined]


async def test_the_review_model_of_the_profile_comes_first(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await set_profile(engine, tenant, review_model="my-cheap-model")
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant)
    assert (await claim_review(engine)).model == "my-cheap-model"  # type: ignore[attr-defined]


async def test_the_queue_setting_replaces_the_default_model(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine, review_models={"fake": "queue-model"})
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant, queue=queue)
    assert (await claim_review(engine, queue)).model == "queue-model"  # type: ignore[attr-defined]


async def test_a_runtime_without_a_cheap_model_uses_the_model_of_the_profile(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine, review_models={})
    queue._review_models = {}  # an empty setting keeps the defaults, so this clears them
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant, queue=queue)
    assert (await claim_review(engine, queue)).model == "fake-model"  # type: ignore[attr-defined]


async def test_the_review_config_is_fresh_and_is_not_stored_on_the_session(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await set_profile(engine, tenant, toolsets=TOOLSETS)
    queue = RunQueue(engine)
    assembler = PromptAssembler(engine)
    await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids["s1"], text="hi", user_id=tenant.ids["mia"]
    )
    chat = await queue.claim(OWNER)
    assert chat is not None
    chat_config = await assembler(chat)
    await set_counters(engine, tenant, turns=9)
    assert await queue.complete(chat, OWNER, output="ok", runtime_state=None, usage=Usage())
    review = await claim_review(engine, queue)
    config = await assembler(review)  # type: ignore[arg-type]
    assert config.system == REVIEW_SYSTEM_PROMPT
    assert config.system != chat_config.system == SYSTEM_PROMPT
    assert (config.soul, config.context_files, config.mcp_servers) == ("", (), ())
    assert chat_config.soul == "You are a test agent."
    assert config.runtime is RuntimeKind.FAKE
    assert config.model == "fake-review"
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                select(Session.run_config, Session.config_fingerprint).where(
                    Session.id == tenant.ids["s1"]
                )
            )
        ).one()
    assert RunConfig.model_validate(row.run_config) == chat_config
    assert row.config_fingerprint == chat_config.fingerprint()
