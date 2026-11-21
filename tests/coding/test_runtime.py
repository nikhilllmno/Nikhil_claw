"""``CodingAgentRuntime``, ``CodingService``, and ``CodingTools`` with a local sandbox and a fake
CLI (``tests/coding/fake_cli.py``)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from sama.coding.agents import CodingAgent
from sama.coding.runtime import CODING_AGENT_RUN, CodingAgentRuntime
from sama.coding.service import CodingRequest, CodingService
from sama.coding.tool import CodingTools
from sama.orchestrator.sandboxed import SandboxedRuntime
from sama.runtime.contract import (
    ApprovalDecision,
    ApprovalRequired,
    Budget,
    CompactRequest,
    FailureCode,
    ResumeRequest,
    RunCompleted,
    RunConfig,
    RunEvent,
    RunFailed,
    RunRequest,
    RuntimeKind,
    SubagentEvent,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    validate_event_stream,
)
from sama.tenancy import RunTokenSigner
from sama.tenancy.run_token import RunTokenClaims
from tests.coding.conftest import calls, make_repo, session_sandbox, set_mode
from tests.support import collect, make_scope


@dataclass(frozen=True)
class _Verdict:
    action: str
    reason: str = ""


@dataclass
class FakeGate:
    action: str = "allow"
    seen: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    async def check(
        self,
        claims: RunTokenClaims,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        child: bool = False,
    ) -> _Verdict:
        self.seen.append((call_id, tool_name, dict(arguments)))
        return _Verdict(self.action, f"the policy says {self.action}")


def _runtime(
    sandboxes: SandboxedRuntime,
    signer: RunTokenSigner,
    kind: RuntimeKind = RuntimeKind.CLAUDE_CODE,
    gate: FakeGate | None = None,
) -> CodingAgentRuntime:
    service = CodingService(model_url="http://proxy.test")
    return CodingAgentRuntime(
        kind=kind, sandboxes=sandboxes, signer=signer, service=service, gate=gate
    )


def _state(folder: str) -> bytes:
    return json.dumps({"v": 1, "sessions": {}, "folder": folder}).encode()


def _request(
    text: str,
    *,
    run_id: str = "run-1",
    kind: RuntimeKind = RuntimeKind.CLAUDE_CODE,
    state: bytes | None = None,
    model: str = "claude-sonnet-4-6",
) -> RunRequest:
    return RunRequest(
        scope=make_scope(run_id),
        input=text,
        config=RunConfig(runtime=kind, model=model),
        budget=Budget(max_wall_time_s=600),
        runtime_state=state,
    )


async def test_a_run_streams_the_cli_and_ends_with_the_diff(
    sandboxes: SandboxedRuntime, signer: RunTokenSigner
) -> None:
    sandbox = await session_sandbox(sandboxes)
    repo = Path(sandbox.workdir) / "proj"
    make_repo(repo)
    gate = FakeGate()
    runtime = _runtime(sandboxes, signer, gate=gate)
    events = await collect(runtime.run(_request("make a change", state=_state(str(repo)))))
    validate_event_stream(events)
    assert isinstance(events[0], TextDelta)
    assert "I will change the file." in events[0].text
    assert any(isinstance(e, ToolCallStarted) and e.name == "Write" for e in events)
    assert any(isinstance(e, ToolCallFinished) and e.call_id == "toolu_1" for e in events)
    final = events[-1]
    assert isinstance(final, RunCompleted)
    assert "Done: I changed change.txt." in final.output
    assert "+claude: make a change" in final.output
    assert final.usage.output_tokens == 30
    tail = events[-2]
    assert isinstance(tail, TextDelta)
    assert "```diff" in tail.text
    (call,) = calls(sandbox)
    assert call["env_ok"] is True
    assert call["cwd"] == str(repo)
    assert call["prompt"] == "make a change"
    assert gate.seen[0][1] == CODING_AGENT_RUN
    assert gate.seen[0][2] == {
        "agent": "claude_code",
        "repo": "",
        "prompt": "make a change",
        "open_pr": False,
    }
    # The next run resumes the CLI session of the folder.
    assert final.runtime_state is not None
    again = await collect(
        runtime.run(_request("one more", run_id="run-2", state=final.runtime_state))
    )
    assert isinstance(again[-1], RunCompleted)
    second = calls(sandbox)[1]["argv"]
    assert isinstance(second, list)
    assert second[-2:] == ["--resume", "11111111-2222-3333-4444-555555555555"]


async def test_codex_runs_through_the_same_runtime(
    sandboxes: SandboxedRuntime, signer: RunTokenSigner
) -> None:
    sandbox = await session_sandbox(sandboxes)
    repo = Path(sandbox.workdir) / "proj"
    make_repo(repo)
    runtime = _runtime(sandboxes, signer, kind=RuntimeKind.CODEX)
    request = _request(
        "add a file", kind=RuntimeKind.CODEX, state=_state(str(repo)), model="claude-haiku-4-5"
    )
    events = await collect(runtime.run(request))
    validate_event_stream(events)
    final = events[-1]
    assert isinstance(final, RunCompleted)
    assert "Done: I added change.txt." in final.output
    assert "+codex: add a file" in final.output
    assert final.usage.cache_read_tokens == 20
    (call,) = calls(sandbox)
    argv = call["argv"]
    assert isinstance(argv, list)
    # A Claude model of the session is not an OpenAI model: Codex uses its default.
    assert argv[argv.index("--model") + 1] == "gpt-6.1-terra"
    assert call["env_ok"] is True


async def test_ask_pauses_and_the_approval_starts_the_cli(
    sandboxes: SandboxedRuntime, signer: RunTokenSigner
) -> None:
    sandbox = await session_sandbox(sandboxes)
    runtime = _runtime(sandboxes, signer, gate=FakeGate("ask"))
    request = _request("make a change --pr")
    events = await collect(runtime.run(request))
    (paused,) = events
    assert isinstance(paused, ApprovalRequired)
    assert paused.tool_name == CODING_AGENT_RUN
    assert paused.arguments["open_pr"] is True
    assert calls(sandbox) == []
    resume = ResumeRequest(
        scope=request.scope,
        config=request.config,
        approval_id=paused.approval_id,
        decision=ApprovalDecision.APPROVE,
        budget=request.budget,
        runtime_state=paused.runtime_state,
    )
    events = await collect(runtime.resume(resume))
    final = events[-1]
    assert isinstance(final, RunCompleted)
    # The work folder is no repository: no diff, and no pull request.
    assert "not a git repository" in final.output
    assert "No pull request" in final.output
    assert len(calls(sandbox)) == 1
    # The approval works once.
    (again,) = await collect(runtime.resume(resume))
    assert isinstance(again, RunFailed)
    assert again.code is FailureCode.INVALID_REQUEST


async def test_a_rejected_request_does_not_start_the_cli(
    sandboxes: SandboxedRuntime, signer: RunTokenSigner
) -> None:
    sandbox = await session_sandbox(sandboxes)
    runtime = _runtime(sandboxes, signer, gate=FakeGate("ask"))
    request = _request("make a change")
    (paused,) = await collect(runtime.run(request))
    assert isinstance(paused, ApprovalRequired)
    resume = ResumeRequest(
        scope=request.scope,
        config=request.config,
        approval_id=paused.approval_id,
        decision=ApprovalDecision.REJECT,
        runtime_state=paused.runtime_state,
    )
    (done,) = await collect(runtime.resume(resume))
    assert isinstance(done, RunCompleted)
    assert "not approved" in done.output
    assert calls(sandbox) == []


async def test_deny_fails_the_run(sandboxes: SandboxedRuntime, signer: RunTokenSigner) -> None:
    runtime = _runtime(sandboxes, signer, gate=FakeGate("deny"))
    (failed,) = await collect(runtime.run(_request("x")))
    assert isinstance(failed, RunFailed)
    assert failed.code is FailureCode.TOOL_ERROR
    assert "refused" in failed.message


async def test_compact_and_an_empty_task_are_refused(
    sandboxes: SandboxedRuntime, signer: RunTokenSigner
) -> None:
    runtime = _runtime(sandboxes, signer)
    compact = _request("").model_copy(update={"compact": CompactRequest()})
    (failed,) = await collect(runtime.run(compact))
    assert isinstance(failed, RunFailed)
    assert failed.code is FailureCode.INVALID_REQUEST
    (empty,) = await collect(runtime.run(_request("--pr")))
    assert isinstance(empty, RunFailed)
    assert empty.code is FailureCode.INVALID_REQUEST


async def test_a_cli_error_fails_the_run_and_keeps_the_session(
    sandboxes: SandboxedRuntime, signer: RunTokenSigner
) -> None:
    sandbox = await session_sandbox(sandboxes)
    set_mode(sandbox, "error")
    runtime = _runtime(sandboxes, signer)
    events = await collect(runtime.run(_request("x")))
    final = events[-1]
    assert isinstance(final, RunFailed)
    assert final.code is FailureCode.MODEL_ERROR
    assert "error_max_turns" in final.message
    assert final.runtime_state is not None


async def test_a_lost_cli_session_starts_a_new_one(
    sandboxes: SandboxedRuntime, signer: RunTokenSigner
) -> None:
    sandbox = await session_sandbox(sandboxes)
    set_mode(sandbox, "fail_resume")
    runtime = _runtime(sandboxes, signer)
    workdir = sandbox.workdir
    state = json.dumps({"v": 1, "sessions": {workdir: "gone"}, "folder": None}).encode()
    events = await collect(runtime.run(_request("x", state=state)))
    assert isinstance(events[-1], RunCompleted)
    first, second = calls(sandbox)
    assert "--resume" in first["argv"]  # type: ignore[operator]
    assert "--resume" not in second["argv"]  # type: ignore[operator]


async def test_a_cancel_kills_the_cli(sandboxes: SandboxedRuntime, signer: RunTokenSigner) -> None:
    sandbox = await session_sandbox(sandboxes)
    set_mode(sandbox, "hang")
    runtime = _runtime(sandboxes, signer)
    request = _request("x")
    stream = runtime.run(request)
    events: list[Any] = []

    async def read() -> None:
        async for event in stream:
            events.append(event)

    reader = asyncio.create_task(read())
    for _ in range(200):
        if calls(sandbox):
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    started = asyncio.get_running_loop().time()
    await runtime.cancel(request.scope.run_id)
    await asyncio.wait_for(reader, timeout=10)
    assert asyncio.get_running_loop().time() - started < 10
    final = events[-1]
    assert isinstance(final, RunFailed)
    assert final.code is FailureCode.CANCELLED


async def test_a_timeout_kills_the_cli(
    sandboxes: SandboxedRuntime, signer: RunTokenSigner, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = await session_sandbox(sandboxes)
    set_mode(sandbox, "hang")
    claims = signer.verify(sandboxes.issue_token(make_scope(), Budget()))
    service = CodingService(model_url="http://proxy.test")
    monkeypatch.setattr("sama.coding.service.MIN_TIMEOUT_S", 0.5)
    outcome = await service.run(
        sandbox,
        claims,
        "token",
        CodingRequest(CodingAgent.CLAUDE_CODE, "x"),
        model="claude-sonnet-4-6",
        timeout_s=1,
    )
    assert outcome.timed_out
    assert not outcome.ok


async def test_the_tool_streams_progress_and_marks_the_result_untrusted(
    sandboxes: SandboxedRuntime, signer: RunTokenSigner
) -> None:
    sandbox = await session_sandbox(sandboxes)
    make_repo(Path(sandbox.workdir) / "proj")
    seen: list[SubagentEvent] = []

    class Sink:
        async def emit(self, org_id: str, run_id: str, event: RunEvent) -> bool:
            assert (org_id, run_id) == ("org-1", "run-1")
            assert isinstance(event, SubagentEvent)
            seen.append(event)
            return True

    token = sandboxes.issue_token(make_scope(), Budget())
    claims = signer.verify(token)
    tools = CodingTools(
        CodingService(model_url="http://proxy.test"), sandboxes.session_sandbox, progress=Sink()
    )
    text = await tools.run(claims, token, agent=CodingAgent.CLAUDE_CODE, prompt="hello")
    assert '<untrusted_content source="claude_code">' in text
    assert "session=11111111-2222-3333-4444-555555555555" in text
    assert [event.text for event in seen[:2]] == ["I will change the file.", "Write change.txt"]
    assert seen[-1].done
    assert {event.subagent for event in seen} == {"claude_code"}
    assert len({event.call_id for event in seen}) == 1


@dataclass
class SlowGate(FakeGate):
    """Holds each check until ``release`` is set."""

    release: asyncio.Event = field(default_factory=asyncio.Event)
    entered: asyncio.Event = field(default_factory=asyncio.Event)

    async def check(
        self,
        claims: RunTokenClaims,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        child: bool = False,
    ) -> _Verdict:
        self.entered.set()
        await self.release.wait()
        return await super().check(claims, call_id, tool_name, arguments, child=child)


async def test_a_cancel_during_the_gate_check_does_not_start_the_cli(
    sandboxes: SandboxedRuntime, signer: RunTokenSigner
) -> None:
    sandbox = await session_sandbox(sandboxes)
    gate = SlowGate()
    runtime = _runtime(sandboxes, signer, gate=gate)
    request = _request("x")
    reader = asyncio.create_task(collect(runtime.run(request)))
    await gate.entered.wait()
    await runtime.cancel(request.scope.run_id)
    gate.release.set()
    (final,) = await asyncio.wait_for(reader, timeout=10)
    assert isinstance(final, RunFailed)
    assert final.code is FailureCode.CANCELLED
    assert calls(sandbox) == []
