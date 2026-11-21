"""The runs of the coding agents in the queue (docs/architecture.md, Section 15.4): a
``/claude <task>`` run keeps its coding agent, the direct mode of a session sends its chat runs
to the coding agent, a compact run and a review stay on the sama runtime, and the prompt
assembler keeps the runtime of the profile in the frozen config."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.coding.agents import CodingAgent
from sama.coding.commands import DIRECT_OFF, NOT_DIRECT, parse_command, set_direct
from sama.db.models import Run, Session
from sama.orchestrator.queue import RunQueue
from sama.prompt import PromptAssembler
from sama.runtime.contract import RuntimeKind
from tests.orchestrator.conftest import Tenant
from tests.review.support import OWNER, set_profile


async def _enqueue(
    queue: RunQueue, tenant: Tenant, text: str = "hi", coding_agent: str | None = None
) -> None:
    await queue.enqueue(
        org_id=tenant.org,
        session_id=tenant.ids["s1"],
        text=text,
        user_id=tenant.ids["mia"],
        coding_agent=coding_agent,
    )


async def test_a_coding_run_keeps_its_agent_and_the_session_config_keeps_the_profile(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await set_profile(engine, tenant, runtime="claude_agent_sdk", model="claude-sonnet-4-5")
    await _enqueue(queue, tenant, "fix it", coding_agent="codex")
    run = await queue.claim(OWNER)
    assert run is not None
    assert run.runtime == "codex"
    assert run.input.endswith("fix it")
    config = await PromptAssembler(engine)(run)
    # The run gets the coding runtime and an OpenAI model; the session keeps the profile.
    assert config.runtime is RuntimeKind.CODEX
    assert config.model == "gpt-6.1-terra"
    async with engine.connect() as connection:
        frozen = (
            await connection.execute(
                select(Session.run_config).where(Session.id == tenant.ids["s1"])
            )
        ).scalar_one()
    assert frozen["runtime"] == "claude_agent_sdk"
    assert frozen["model"] == "claude-sonnet-4-5"


async def test_the_direct_mode_sends_chat_runs_to_the_agent_until_sama(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await set_profile(engine, tenant, runtime="claude_agent_sdk", model="claude-sonnet-4-5")
    answer = await set_direct(engine, tenant.org, tenant.ids["s1"], CodingAgent.CLAUDE_CODE)
    assert "Claude Code" in answer
    await _enqueue(queue, tenant)
    run = await queue.claim(OWNER)
    assert run is not None
    assert run.runtime == "claude_code"
    async with engine.begin() as connection:
        await connection.execute(
            update(Run)
            .where(Run.id == run.run_id)
            .values(status="cancelled", lease_owner=None, lease_expires_at=None)
        )
    assert await set_direct(engine, tenant.org, tenant.ids["s1"], None) == DIRECT_OFF
    assert await set_direct(engine, tenant.org, tenant.ids["s1"], None) == NOT_DIRECT
    await _enqueue(queue, tenant)
    run = await queue.claim(OWNER)
    assert run is not None
    assert run.runtime == "claude_agent_sdk"


async def test_a_compact_run_stays_on_the_sama_runtime(engine: AsyncEngine, tenant: Tenant) -> None:
    await set_profile(engine, tenant, runtime="claude_agent_sdk", model="claude-sonnet-4-5")
    async with engine.begin() as connection:
        await connection.execute(
            update(Session).where(Session.id == tenant.ids["s1"]).values(coding_agent="codex")
        )
        await connection.execute(
            update(Run).where(Run.session_id == tenant.ids["s1"]).values(status="cancelled")
        )
    queue = RunQueue(engine)
    # A compact run needs an earlier run with a state; write one directly.
    async with engine.begin() as connection:
        await connection.execute(
            insert(Run).values(
                org_id=tenant.org,
                session_id=tenant.ids["s1"],
                agent_profile_id=tenant.ids["profile"],
                runtime="claude_agent_sdk",
                status="completed",
                runtime_state="e30=",
                finished_at=datetime.now(UTC),
            )
        )
    await queue.enqueue_compact(
        org_id=tenant.org, session_id=tenant.ids["s1"], user_id=tenant.ids["mia"]
    )
    run = await queue.claim(OWNER)
    assert run is not None
    assert run.compact
    assert run.runtime == "claude_agent_sdk"


def test_parse_command() -> None:
    assert parse_command("/claude") is not None
    command = parse_command("/codex@sama_bot  octo/demo fix it")
    assert command is not None
    assert command.agent is CodingAgent.CODEX
    assert command.task == "octo/demo fix it"
    back = parse_command("/sama")
    assert back is not None
    assert back.agent is None
    assert back.switch
    assert parse_command("/sama new") is None
    assert parse_command("/claudex") is None
    assert parse_command("hello /claude") is None
