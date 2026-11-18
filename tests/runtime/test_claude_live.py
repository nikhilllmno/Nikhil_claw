"""Live tests of the Claude adapter against the real CLI and the Anthropic API.

They cost money, so they run only with ``SAMA_LIVE_TESTS=1`` and ``ANTHROPIC_API_KEY``:

    SAMA_LIVE_TESTS=1 uv run --all-groups pytest -m live
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("claude_agent_sdk")

from sama.runtime import (
    ApprovalDecision,
    ApprovalRequired,
    FailureCode,
    ResumeRequest,
    RunCompleted,
    RunConfig,
    RunEvent,
    RunFailed,
    RunRequest,
    RunScope,
    RuntimeKind,
    TextDelta,
    ToolCallStarted,
    Usage,
    validate_event_stream,
)
from sama.runtime.claude import ClaudeRuntime, ClaudeSettings, GateDecision
from tests.live_budget import LIVE_SPEND
from tests.support import collect, make_scope

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("SAMA_LIVE_TESTS") != "1" or not os.environ.get("ANTHROPIC_API_KEY"),
        reason="set SAMA_LIVE_TESTS=1 and ANTHROPIC_API_KEY to run live tests",
    ),
]

MODEL = os.environ.get("SAMA_LIVE_CLAUDE_MODEL", "claude-haiku-4-5")


class AskForBash:
    """Asks for every Bash call. Allows the other tools."""

    async def check(
        self, scope: RunScope, tool_name: str, arguments: Mapping[str, Any]
    ) -> GateDecision:
        if tool_name == "Bash":
            return GateDecision("ask", "shell commands need approval")
        return GateDecision("allow")


def _config() -> RunConfig:
    return RunConfig(
        runtime=RuntimeKind.CLAUDE_AGENT_SDK,
        model=MODEL,
        soul="You are terse. Use tools when the user asks.",
    )


def _runtime(cwd: Path) -> ClaudeRuntime:
    return ClaudeRuntime(gate=AskForBash(), settings=ClaudeSettings(cwd=str(cwd)))


def _request(text: str, run_id: str, state: bytes | None = None) -> RunRequest:
    return RunRequest(
        scope=make_scope(run_id),
        input=text,
        config=_config(),
        runtime_state=state,
        budget=LIVE_SPEND.budget(),
    )


async def _collect(stream: AsyncIterator[RunEvent], start: Usage | None = None) -> list[RunEvent]:
    """Collects a stream and adds its cost to the shared live cap (tests/live_budget.py)."""
    events = await collect(stream)
    LIVE_SPEND.record(events, already_spent=start)
    return events


async def test_streams_text_and_resumes_the_session(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    first = await _collect(runtime.run(_request("Remember the word 'tamarind'. Reply OK.", "r1")))
    done = validate_event_stream(first)
    assert isinstance(done, RunCompleted), done
    assert any(isinstance(e, TextDelta) for e in first)
    second = await _collect(
        runtime.run(_request("Which word did I ask you to remember?", "r2", done.runtime_state))
    )
    answer = validate_event_stream(second)
    assert isinstance(answer, RunCompleted), answer
    assert "tamarind" in answer.output.lower()


def _exists(path: Path) -> bool:
    return path.exists()


async def _pause(runtime: ClaudeRuntime, marker: Path) -> ApprovalRequired:
    prompt = f"Run this exact shell command with the Bash tool: touch {marker}"
    events = await _collect(runtime.run(_request(prompt, "r-approval")))
    paused = validate_event_stream(events)
    assert isinstance(paused, ApprovalRequired), paused
    assert paused.tool_name == "Bash"
    assert any(isinstance(e, ToolCallStarted) for e in events)
    assert not _exists(marker)
    return paused


def _resume(paused: ApprovalRequired, decision: ApprovalDecision) -> ResumeRequest:
    return ResumeRequest(
        scope=make_scope("r-approval"),
        config=_config(),
        approval_id=paused.approval_id,
        decision=decision,
        reason="not now" if decision is ApprovalDecision.REJECT else None,
        runtime_state=paused.runtime_state,
        budget=LIVE_SPEND.budget(already_spent=paused.usage),
    )


async def test_approve_runs_the_deferred_call(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    marker = tmp_path / "approved.txt"
    paused = await _pause(runtime, marker)
    events = await _collect(runtime.resume(_resume(paused, ApprovalDecision.APPROVE)), paused.usage)
    assert isinstance(validate_event_stream(events, start=paused.usage), RunCompleted)
    assert _exists(marker)


async def test_reject_does_not_run_the_deferred_call(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    marker = tmp_path / "rejected.txt"
    paused = await _pause(runtime, marker)
    events = await _collect(runtime.resume(_resume(paused, ApprovalDecision.REJECT)), paused.usage)
    terminal = validate_event_stream(events, start=paused.usage)
    assert isinstance(terminal, (RunCompleted, ApprovalRequired)), terminal
    assert not _exists(marker)


async def test_cancel_ends_the_run_within_ten_seconds(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    prompt = "Write a 2000-word story about a lighthouse keeper."
    events: list[RunEvent] = []
    stream = runtime.run(_request(prompt, "r-cancel"))
    async for event in stream:
        events.append(event)
        if isinstance(event, TextDelta):
            break
    started = time.monotonic()
    cancel = asyncio.create_task(runtime.cancel("r-cancel"))
    events.extend([e async for e in stream])
    await cancel
    elapsed = time.monotonic() - started
    LIVE_SPEND.record(events)
    terminal = validate_event_stream(events)
    assert isinstance(terminal, RunFailed), terminal
    assert terminal.code is FailureCode.CANCELLED
    assert elapsed < 10
