"""The input, the config and the model of a review run (docs/architecture.md, Section 9.4)."""

from __future__ import annotations

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Message, RunEvent, Session
from sama.orchestrator.queue import ClaimedRun, RunQueue
from sama.prompt import SYSTEM_PROMPT, PromptAssembler
from sama.review.policy import (
    MESSAGE_MAX_CHARS,
    TOOL_NAME_MAX_CHARS,
    TOOLS_LINE_MAX_CHARS,
    TRANSCRIPT_MAX_CHARS,
)
from sama.review.policy import SYSTEM_PROMPT as REVIEW_SYSTEM_PROMPT
from sama.review.transcript import EMPTY, HEADER, OMITTED, _bounded, _tools_line
from sama.runtime.claude import _mcp_servers
from sama.runtime.contract import McpServerConfig, RunConfig, RuntimeKind, Usage
from sama.runtime.runner import with_run_token
from tests.orchestrator.conftest import Tenant
from tests.review.support import OWNER, complete_chat, set_counters, set_profile
from tests.review.test_writes import TOOLSETS

pytestmark = pytest.mark.usefixtures("tenant")

SECRET = "ghp_" + "a1B2" * 9
SAMA_SERVER = McpServerConfig(name="sama", url="http://tools.test/mcp")


async def claim_review(engine: AsyncEngine, queue: RunQueue | None = None) -> ClaimedRun:
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


async def add_tool_names(engine: AsyncEngine, run: ClaimedRun, names: list[str]) -> None:
    async with engine.begin() as connection:
        for seq, name in enumerate(names):
            await connection.execute(
                insert(RunEvent).values(
                    org_id=run.org_id,
                    session_id=run.session_id,
                    run_id=run.run_id,
                    seq=10_000 + seq,
                    type="tool_call_started",
                    payload={"name": name},
                )
            )


async def test_a_tool_name_is_redacted_and_capped(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    await set_counters(engine, tenant, turns=9)
    run = await complete_chat(engine, tenant, queue=queue)
    await add_tool_names(engine, run, [f"mcp.{SECRET}.go", "n" * 500])
    review = await claim_review(engine, queue)
    assert SECRET not in review.input
    assert "ghp_" not in review.input
    assert "n" * (TOOL_NAME_MAX_CHARS + 1) not in review.input
    (line,) = [x for x in review.input.splitlines() if x.startswith("(tools of this turn")]
    assert len(line) <= TOOLS_LINE_MAX_CHARS


async def test_many_tools_give_one_bounded_line(engine: AsyncEngine, tenant: Tenant) -> None:
    queue = RunQueue(engine)
    await set_counters(engine, tenant, turns=9)
    run = await complete_chat(engine, tenant, queue=queue)
    await add_tool_names(engine, run, [f"mcp__server__tool_{n:04d}" for n in range(300)])
    review = await claim_review(engine, queue)
    (line,) = [x for x in review.input.splitlines() if x.startswith("(tools of this turn")]
    assert len(line) <= TOOLS_LINE_MAX_CHARS
    assert line.startswith("(tools of this turn: mcp__server__tool_0000")
    assert line.endswith(" more)")
    assert "tool_0299" not in line


def test_the_tools_line_counts_the_names_that_it_drops() -> None:
    assert _tools_line(["a", "b"]) == "(tools of this turn: a, b)"
    names = [f"tool_{n:03d}" for n in range(200)]
    line = _tools_line(names)
    shown = line.count("tool_")
    assert len(line) <= TOOLS_LINE_MAX_CHARS
    assert line.endswith(f", and {200 - shown} more)")


def test_one_line_over_the_limit_stays_inside_it() -> None:
    text = _bounded(["[user] " + "z" * (TRANSCRIPT_MAX_CHARS * 2)])
    assert len(text) <= TRANSCRIPT_MAX_CHARS
    assert text.startswith("[user] z")


def test_the_note_of_omitted_lines_is_inside_the_limit() -> None:
    lines = [f"{n:03d} " + "y" * 900 for n in range(100)]
    text = _bounded(lines)
    assert len(text) <= TRANSCRIPT_MAX_CHARS
    assert text.startswith(OMITTED + "\n")
    assert text.endswith(lines[-1])
    assert lines[0] not in text


def test_a_long_newest_line_and_older_lines_are_inside_the_limit() -> None:
    lines = ["old " * 50, "[assistant] " + "w" * (TRANSCRIPT_MAX_CHARS * 2)]
    text = _bounded(lines)
    assert len(text) <= TRANSCRIPT_MAX_CHARS
    assert text.startswith(OMITTED)
    assert "w" in text


def test_lines_that_fit_have_no_note() -> None:
    assert _bounded(["a", "b"]) == "a\nb"
    assert _bounded([]) == EMPTY


async def test_the_review_model_is_the_cheap_default_of_the_runtime(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant)
    review = await claim_review(engine)
    assert review.model == "fake-review"
    assert review.model != "fake-model"


async def test_the_review_model_of_the_profile_comes_first(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await set_profile(engine, tenant, review_model="my-cheap-model")
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant)
    assert (await claim_review(engine)).model == "my-cheap-model"


async def test_the_queue_setting_replaces_the_default_model(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine, review_models={"fake": "queue-model"})
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant, queue=queue)
    assert (await claim_review(engine, queue)).model == "queue-model"


async def test_a_runtime_without_a_cheap_model_uses_the_model_of_the_profile(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine, review_models={})
    queue._review_models = {}  # an empty setting keeps the defaults, so this clears them
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant, queue=queue)
    assert (await claim_review(engine, queue)).model == "fake-model"


async def test_the_review_config_is_fresh_and_is_not_stored_on_the_session(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await set_profile(engine, tenant, toolsets=TOOLSETS)
    queue = RunQueue(engine)
    assembler = PromptAssembler(engine, mcp_servers=(SAMA_SERVER,))
    await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids["s1"], text="hi", user_id=tenant.ids["mia"]
    )
    chat = await queue.claim(OWNER)
    assert chat is not None
    chat_config = await assembler(chat)
    await set_counters(engine, tenant, turns=9)
    assert await queue.complete(chat, OWNER, output="ok", runtime_state=None, usage=Usage())
    review = await claim_review(engine, queue)
    config = await assembler(review)
    assert config.system == REVIEW_SYSTEM_PROMPT
    assert config.system != chat_config.system == SYSTEM_PROMPT
    assert (config.soul, config.context_files) == ("", ())
    assert config.mcp_servers == chat_config.mcp_servers == (SAMA_SERVER,)
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


async def test_the_review_config_gives_the_runtime_the_sama_server_and_the_run_token(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    """The Claude adapter registers tools only from ``mcp_servers`` (strict MCP config), so a
    review with no server could not call ``memory`` or ``skill_manage`` (Section 9.4)."""
    await set_profile(engine, tenant, toolsets=TOOLSETS)
    queue = RunQueue(engine)
    await set_counters(engine, tenant, turns=9)
    await complete_chat(engine, tenant, queue=queue)
    review = await claim_review(engine, queue)
    config = await PromptAssembler(engine, mcp_servers=(SAMA_SERVER,))(review)
    # The runner sets the bearer, as for a chat run. The runtime gets it from the config.
    wired = _mcp_servers(with_run_token(config, "run-token"))
    assert wired == {
        "sama": {
            "type": "http",
            "url": "http://tools.test/mcp",
            "headers": {"Authorization": "Bearer run-token"},
        }
    }
    # No server of a user: the assembler adds nothing but the servers that the control plane
    # gives it.
    assert [server.name for server in config.mcp_servers] == ["sama"]
