"""The runtime conformance suite (docs/architecture.md, Section 5.5; roadmap M1.5).

Every runtime target runs the same tests. The tests check only contract events and the
records of the MCP tool server, so a runtime passes the same way on the scripted model and on
a real model.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncGenerator
from typing import cast

import pytest

from sama.runtime import (
    ApprovalDecision,
    ApprovalRequired,
    Budget,
    Compacted,
    FailureCode,
    ResumeRequest,
    RunCompleted,
    RunEvent,
    RunFailed,
    RunRequest,
    RunScope,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    Usage,
    validate_event_stream,
)
from tests.conformance.targets import (
    CANCEL_LIMIT_S,
    WRITE_TOOL,
    Scenario,
    Target,
    new_word,
)
from tests.conformance.tool_server import LOOKUP, SEND_NOTE, tool_id
from tests.live_budget import LIVE_SPEND
from tests.support import collect, make_scope

LIVE_TARGET = "claude-live"
SCRIPTED_WALL_TIME_S = 30.0
"""A scripted run takes under a second. A hang fails the test after this time."""


def budget_for(
    target: Target, *, max_turns: int | None = None, already_spent: Usage | None = None
) -> Budget:
    """The run budget. A live target gets the rest of the shared cost cap (tests/live_budget.py).
    The scripted model is free, so other targets get only the limits of the test."""
    if target.name == LIVE_TARGET:
        return LIVE_SPEND.budget(max_turns=max_turns, already_spent=already_spent)
    return Budget(max_turns=max_turns, max_wall_time_s=SCRIPTED_WALL_TIME_S)


def record_spend(
    target: Target, events: list[RunEvent], *, already_spent: Usage | None = None
) -> None:
    if target.name == LIVE_TARGET:
        LIVE_SPEND.record(events, already_spent=already_spent)


def scope() -> RunScope:
    return make_scope(f"run-{uuid.uuid4().hex[:8]}")


def request(
    target: Target,
    text: str,
    *,
    run_scope: RunScope | None = None,
    state: bytes | None = None,
    max_turns: int | None = None,
) -> RunRequest:
    return RunRequest(
        scope=run_scope or scope(),
        input=text,
        config=target.config,
        budget=budget_for(target, max_turns=max_turns),
        runtime_state=state,
    )


async def run(
    target: Target,
    text: str,
    *,
    run_scope: RunScope | None = None,
    state: bytes | None = None,
    max_turns: int | None = None,
) -> list[RunEvent]:
    run_request = request(target, text, run_scope=run_scope, state=state, max_turns=max_turns)
    events = await collect(target.runtime.run(run_request))
    record_spend(target, events)
    validate_event_stream(events)
    return events


def resume_request(
    target: Target, paused: ApprovalRequired, run_scope: RunScope, decision: ApprovalDecision
) -> ResumeRequest:
    return ResumeRequest(
        scope=run_scope,
        config=target.config,
        approval_id=paused.approval_id,
        decision=decision,
        reason="The note is not needed." if decision is ApprovalDecision.REJECT else None,
        budget=budget_for(target, already_spent=paused.usage),
        runtime_state=paused.runtime_state,
    )


async def resume(
    target: Target, paused: ApprovalRequired, run_scope: RunScope, decision: ApprovalDecision
) -> list[RunEvent]:
    request_ = resume_request(target, paused, run_scope, decision)
    events = await collect(target.runtime.resume(request_))
    record_spend(target, events, already_spent=paused.usage)
    validate_event_stream(events, start=paused.usage)
    return events


def completed(events: list[RunEvent]) -> RunCompleted:
    last = events[-1]
    assert isinstance(last, RunCompleted), last
    return last


def text_of(events: list[RunEvent]) -> str:
    return "".join(e.text for e in events if isinstance(e, TextDelta))


async def test_streams_text_and_ends_with_one_terminal_event(target: Target) -> None:
    word = new_word()
    events = await run(target, target.prompt(Scenario.TEXT, word))
    done = completed(events)
    assert any(isinstance(e, TextDelta) for e in events)
    assert word in done.output
    assert word in text_of(events)
    assert done.usage.output_tokens > 0


async def test_resumes_the_session_from_runtime_state(target: Target) -> None:
    word = new_word()
    first = completed(await run(target, target.prompt(Scenario.REMEMBER, word)))
    assert first.runtime_state is not None
    second = completed(await run(target, target.prompt(Scenario.RECALL), state=first.runtime_state))
    assert word in second.output


async def test_calls_an_mcp_tool_and_uses_the_result(target: Target) -> None:
    events = await run(target, target.prompt(Scenario.LOOKUP))
    # A real model can call other tools first, for example ToolSearch to load MCP tools.
    started = [e for e in events if isinstance(e, ToolCallStarted) and e.name == tool_id(LOOKUP)]
    assert len(started) == 1
    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    lookup = [e for e in finished if e.call_id == started[0].call_id]
    assert len(lookup) == 1
    assert not lookup[0].is_error
    assert target.tools.secret in completed(events).output
    if target.calls_mcp:
        assert target.tools.calls == [LOOKUP]


GATED = pytest.mark.parametrize("scenario", [Scenario.NOTE, Scenario.WRITE], ids=["mcp", "native"])


async def _done(target: Target, scenario: Scenario, run_scope: RunScope) -> list[str]:
    """What the gated call left behind: sent notes, or the content of the written file."""
    if scenario is Scenario.NOTE:
        return list(target.tools.notes)
    note = await target.read_note(run_scope)
    return [] if note is None else [note]


async def _pause(
    target: Target, scenario: Scenario, word: str
) -> tuple[ApprovalRequired, RunScope]:
    run_scope = scope()
    events = await run(target, target.prompt(scenario, word), run_scope=run_scope)
    paused = events[-1]
    assert isinstance(paused, ApprovalRequired), paused
    if scenario is Scenario.NOTE:
        assert paused.tool_name == tool_id(SEND_NOTE)
        assert paused.arguments.get("text") == word
    else:
        assert paused.tool_name == WRITE_TOOL
        assert word in str(paused.arguments.get("command"))
    assert await _done(target, scenario, run_scope) == []
    return paused, run_scope


@GATED
async def test_approve_runs_the_gated_call(target: Target, scenario: Scenario) -> None:
    word = new_word()
    paused, run_scope = await _pause(target, scenario, word)
    events = await resume(target, paused, run_scope, ApprovalDecision.APPROVE)
    completed(events)
    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert [e.call_id for e in finished] == [paused.call_id]
    assert not finished[0].is_error
    if target.calls_mcp:
        assert await _done(target, scenario, run_scope) == [word]


@GATED
async def test_reject_does_not_run_the_gated_call(target: Target, scenario: Scenario) -> None:
    word = new_word()
    paused, run_scope = await _pause(target, scenario, word)
    events = await resume(target, paused, run_scope, ApprovalDecision.REJECT)
    completed(events)
    assert await _done(target, scenario, run_scope) == []
    if target.calls_mcp:
        assert SEND_NOTE not in target.tools.calls


@GATED
async def test_an_approval_resumes_only_once(target: Target, scenario: Scenario) -> None:
    word = new_word()
    paused, run_scope = await _pause(target, scenario, word)
    completed(await resume(target, paused, run_scope, ApprovalDecision.APPROVE))
    if target.calls_mcp:
        assert await _done(target, scenario, run_scope) == [word]
    done_before, calls_before = await _done(target, scenario, run_scope), list(target.tools.calls)
    if scenario is Scenario.WRITE:
        await target.remove_note(run_scope)  # A second run of the call would write it again.
    # A refused request reports no usage, so it is not checked against the paused usage.
    request_ = resume_request(target, paused, run_scope, ApprovalDecision.APPROVE)
    again = await collect(target.runtime.resume(request_))
    validate_event_stream(again)
    assert isinstance(again[-1], RunFailed)
    assert again[-1].code is FailureCode.INVALID_REQUEST
    # The replay ran nothing.
    assert not any(isinstance(e, ToolCallStarted | ToolCallFinished) for e in again)
    assert target.tools.calls == calls_before
    if scenario is Scenario.NOTE:
        assert await _done(target, scenario, run_scope) == done_before
    else:
        assert await _done(target, scenario, run_scope) == []


async def test_cancel_ends_the_run_within_ten_seconds(target: Target) -> None:
    run_scope = scope()
    run_request = request(target, target.prompt(Scenario.SLOW), run_scope=run_scope)
    # Every adapter returns an async generator. aclosing() stops it if the test fails.
    stream = cast(AsyncGenerator[RunEvent], target.runtime.run(run_request))
    events: list[RunEvent] = []
    async with contextlib.aclosing(stream):
        async with asyncio.timeout(60):
            async for event in stream:
                events.append(event)
                if isinstance(event, TextDelta):
                    break
        started = time.monotonic()
        # One limit for the cancel call and the rest of the stream (contract: 10 seconds).
        async with asyncio.timeout(CANCEL_LIMIT_S):
            await target.runtime.cancel(run_scope.run_id)
            events.extend([event async for event in stream])
        elapsed = time.monotonic() - started
    assert elapsed < CANCEL_LIMIT_S
    record_spend(target, events)
    validate_event_stream(events)
    last = events[-1]
    assert isinstance(last, RunFailed), last
    assert last.code is FailureCode.CANCELLED


async def test_budget_stops_the_run(target: Target) -> None:
    # The tool call uses the only turn, so the model cannot answer with the result.
    events = await run(target, target.prompt(Scenario.BUDGET), max_turns=1)
    last = events[-1]
    assert isinstance(last, RunFailed), last
    assert last.code is FailureCode.BUDGET_EXCEEDED


async def test_compaction_streams_a_compacted_event(target: Target) -> None:
    first = completed(await run(target, target.prompt(Scenario.TEXT, new_word())))
    events = await run(target, target.prompt(Scenario.COMPACT), state=first.runtime_state)
    compacted = [e for e in events if isinstance(e, Compacted)]
    assert len(compacted) == 1
    completed(events)
