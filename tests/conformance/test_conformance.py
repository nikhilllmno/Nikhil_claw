"""The runtime conformance suite (docs/architecture.md, Section 5.5; roadmap M1.5).

Every runtime target runs the same tests. The tests check only contract events and the
records of the MCP tool server, so a runtime passes the same way on the scripted model and on
a real model.
"""

from __future__ import annotations

import asyncio
import time
import uuid

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
    validate_event_stream,
)
from tests.conformance.targets import (
    CANCEL_LIMIT_S,
    LIVE_MAX_COST_USD,
    NOTE_FILE,
    WRITE_TOOL,
    Scenario,
    Target,
    new_word,
)
from tests.conformance.tool_server import LOOKUP, SEND_NOTE, tool_id
from tests.support import collect, make_scope

BUDGET = Budget(max_cost_usd=LIVE_MAX_COST_USD, max_wall_time_s=120)


def scope() -> RunScope:
    return make_scope(f"run-{uuid.uuid4().hex[:8]}")


def request(
    target: Target,
    text: str,
    *,
    run_scope: RunScope | None = None,
    state: bytes | None = None,
    budget: Budget = BUDGET,
) -> RunRequest:
    return RunRequest(
        scope=run_scope or scope(),
        input=text,
        config=target.config,
        budget=budget,
        runtime_state=state,
    )


async def run(
    target: Target,
    text: str,
    *,
    run_scope: RunScope | None = None,
    state: bytes | None = None,
    budget: Budget = BUDGET,
) -> list[RunEvent]:
    events = await collect(
        target.runtime.run(request(target, text, run_scope=run_scope, state=state, budget=budget))
    )
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
        budget=BUDGET,
        runtime_state=paused.runtime_state,
    )


async def resume(
    target: Target, paused: ApprovalRequired, run_scope: RunScope, decision: ApprovalDecision
) -> list[RunEvent]:
    request_ = resume_request(target, paused, run_scope, decision)
    events = await collect(target.runtime.resume(request_))
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


def _done(target: Target, scenario: Scenario) -> list[str]:
    """What the gated call left behind: sent notes, or the content of the written file."""
    if scenario is Scenario.NOTE:
        return list(target.tools.notes)
    if target.workdir is None:
        return []
    path = target.workdir / NOTE_FILE
    return [path.read_text().strip()] if path.exists() else []


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
    assert _done(target, scenario) == []
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
        assert _done(target, scenario) == [word]


@GATED
async def test_reject_does_not_run_the_gated_call(target: Target, scenario: Scenario) -> None:
    word = new_word()
    paused, run_scope = await _pause(target, scenario, word)
    events = await resume(target, paused, run_scope, ApprovalDecision.REJECT)
    completed(events)
    assert _done(target, scenario) == []
    if target.calls_mcp:
        assert SEND_NOTE not in target.tools.calls


async def test_an_approval_resumes_only_once(target: Target) -> None:
    paused, run_scope = await _pause(target, Scenario.NOTE, new_word())
    completed(await resume(target, paused, run_scope, ApprovalDecision.APPROVE))
    # A refused request reports no usage, so it is not checked against the paused usage.
    request_ = resume_request(target, paused, run_scope, ApprovalDecision.APPROVE)
    again = await collect(target.runtime.resume(request_))
    validate_event_stream(again)
    assert isinstance(again[-1], RunFailed)
    assert again[-1].code is FailureCode.INVALID_REQUEST


async def test_cancel_ends_the_run_within_ten_seconds(target: Target) -> None:
    run_scope = scope()
    stream = target.runtime.run(request(target, target.prompt(Scenario.SLOW), run_scope=run_scope))
    events: list[RunEvent] = []
    async with asyncio.timeout(60):
        async for event in stream:
            events.append(event)
            if isinstance(event, TextDelta):
                break
    started = time.monotonic()
    await target.runtime.cancel(run_scope.run_id)
    async with asyncio.timeout(CANCEL_LIMIT_S):
        events.extend([event async for event in stream])
    assert time.monotonic() - started < CANCEL_LIMIT_S
    validate_event_stream(events)
    last = events[-1]
    assert isinstance(last, RunFailed), last
    assert last.code is FailureCode.CANCELLED


async def test_budget_stops_the_run(target: Target) -> None:
    # The tool call uses the only turn, so the model cannot answer with the result.
    events = await run(target, target.prompt(Scenario.BUDGET), budget=Budget(max_turns=1))
    last = events[-1]
    assert isinstance(last, RunFailed), last
    assert last.code is FailureCode.BUDGET_EXCEEDED


async def test_compaction_streams_a_compacted_event(target: Target) -> None:
    first = completed(await run(target, target.prompt(Scenario.TEXT, new_word())))
    events = await run(target, target.prompt(Scenario.COMPACT), state=first.runtime_state)
    compacted = [e for e in events if isinstance(e, Compacted)]
    assert len(compacted) == 1
    completed(events)
