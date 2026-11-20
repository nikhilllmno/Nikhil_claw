"""Tests of the runner protocol (src/sama/runtime/runner.py) with the fake runtime."""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator, Sequence

import pytest
from pydantic import ValidationError

from sama.runtime import (
    ApprovalDecision,
    ApprovalRequired,
    FailureCode,
    ResumeRequest,
    RunCompleted,
    RunEvent,
    RunFailed,
    RunRequest,
    RuntimeKind,
    TextDelta,
    Usage,
    run_event_adapter,
    validate_event_stream,
)
from sama.runtime.fake import FakeScript
from sama.runtime.runner import (
    FORBIDDEN_ENV,
    RUN_TOKEN_ENV,
    GatePolicy,
    Output,
    RunnerJob,
    build_runtime,
    claude_cli_env,
    forbidden_env,
    serve,
    with_run_token,
)
from tests.support import make_config, make_scope

ENV = {RUN_TOKEN_ENV: "token-1"}


def _job(*scripts: FakeScript, run_id: str = "run-1") -> RunnerJob:
    request = RunRequest(scope=make_scope(run_id), input="hi", config=make_config())
    return RunnerJob(op="run", run=request, fake_scripts=scripts)


def _reply(text: str) -> FakeScript:
    usage = Usage(input_tokens=3, output_tokens=1)
    return FakeScript(
        events=(
            TextDelta(text=text, usage=usage),
            RunCompleted(output=text, usage=usage, runtime_state=b"\x00\xff"),
        )
    )


def _slow() -> FakeScript:
    words = [TextDelta(text=f"w{i} ", usage=Usage(output_tokens=i)) for i in range(1, 51)]
    done = RunCompleted(output="done", usage=Usage(output_tokens=50))
    return FakeScript(events=(*words, done), delay_s=0.05)


async def _lines(items: Sequence[str], *, hold: asyncio.Event | None = None) -> AsyncIterator[str]:
    for item in items:
        yield item
    if hold is not None:
        await hold.wait()


async def _serve(
    lines: AsyncIterator[str], env: dict[str, str] | None = None
) -> tuple[int, list[RunEvent]]:
    buffer = io.StringIO()
    code = await serve(lines, Output(buffer), ENV if env is None else env)
    events = [run_event_adapter.validate_json(line) for line in buffer.getvalue().splitlines()]
    return code, events


async def test_a_run_streams_events_one_per_line() -> None:
    hold = asyncio.Event()
    code, events = await _serve(_lines([_job(_reply("hello")).model_dump_json()], hold=hold))
    assert code == 0
    validate_event_stream(events)
    assert isinstance(events[-1], RunCompleted)
    assert events[-1].runtime_state == b"\x00\xff"  # bytes survive the JSON line


async def test_a_pause_ends_the_stream_with_the_approval() -> None:
    usage = Usage(input_tokens=1, output_tokens=1)
    paused = ApprovalRequired(
        approval_id="a1", call_id="c1", tool_name="send", usage=usage, runtime_state=b"{}"
    )
    after = (RunCompleted(output="ok", usage=usage),)
    script = FakeScript(events=(paused,), on_approve=after, on_reject=after)
    lines = _lines([_job(script).model_dump_json()], hold=asyncio.Event())
    code, events = await _serve(lines)
    assert code == 0
    assert events == [paused]


async def test_the_fake_cannot_resume_in_a_new_process() -> None:
    # The fake keeps its paused runs in memory. The Claude adapter resumes from runtime_state,
    # so the conformance suite covers resume through the runner with claude-runner.
    request = ResumeRequest(
        scope=make_scope("run-9"),
        config=make_config(),
        approval_id="a1",
        decision=ApprovalDecision.APPROVE,
    )
    job = RunnerJob(op="resume", resume=request)
    code, events = await _serve(_lines([job.model_dump_json()], hold=asyncio.Event()))
    assert code == 0
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.INVALID_REQUEST


async def test_a_cancel_line_cancels_the_run() -> None:
    slow = _slow()
    lines = _lines([_job(slow).model_dump_json(), json.dumps({"op": "cancel"})])
    code, events = await asyncio.wait_for(_serve(lines), timeout=5)
    assert code == 0
    validate_event_stream(events)
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.CANCELLED


async def test_the_end_of_stdin_cancels_the_run() -> None:
    slow = _slow()
    code, events = await asyncio.wait_for(_serve(_lines([_job(slow).model_dump_json()])), 5)
    assert code == 0
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.CANCELLED


async def test_unknown_control_lines_are_ignored() -> None:
    lines = _lines(
        [_job(_reply("ok")).model_dump_json(), "not json", json.dumps({"op": "dance"})],
        hold=asyncio.Event(),
    )
    code, events = await _serve(lines)
    assert code == 0
    assert isinstance(events[-1], RunCompleted)


async def test_no_job_is_an_invalid_request() -> None:
    code, events = await _serve(_lines([]))
    assert code == 2
    assert isinstance(events[0], RunFailed)
    assert events[0].code is FailureCode.INVALID_REQUEST


async def test_a_bad_fake_script_is_an_invalid_request() -> None:
    job = _job(_reply("ok")).model_dump(mode="json")
    job["fake_scripts"][0]["events"] = job["fake_scripts"][0]["events"][:1]  # no terminal event
    code, events = await _serve(_lines([json.dumps(job)]))
    assert code == 2
    assert isinstance(events[0], RunFailed)
    assert events[0].code is FailureCode.INVALID_REQUEST


async def test_a_bad_job_is_an_invalid_request() -> None:
    code, events = await _serve(_lines(['{"op": "run"}']))
    assert code == 2
    assert isinstance(events[0], RunFailed)
    assert "not valid" in events[0].message


@pytest.mark.parametrize("name", sorted(FORBIDDEN_ENV))
async def test_a_forbidden_variable_stops_the_runner(name: str) -> None:
    secret = "sk-should-not-be-here"
    code, events = await _serve(
        _lines([_job(_reply("x")).model_dump_json()]), {**ENV, name: secret}
    )
    assert code == 3
    assert isinstance(events[0], RunFailed)
    assert events[0].code is FailureCode.SANDBOX_ERROR
    assert name in events[0].message
    assert secret not in events[0].message


async def test_the_runner_needs_a_run_token() -> None:
    code, events = await _serve(_lines([_job(_reply("x")).model_dump_json()]), {})
    assert code == 3
    assert isinstance(events[0], RunFailed)
    assert RUN_TOKEN_ENV in events[0].message


async def test_an_unsupported_runtime_is_an_invalid_request() -> None:
    request = RunRequest(
        scope=make_scope(), input="hi", config=make_config(RuntimeKind.OPENAI_AGENTS_SDK)
    )
    job = RunnerJob(op="run", run=request)
    code, events = await _serve(_lines([job.model_dump_json()]))
    assert code == 2
    assert isinstance(events[0], RunFailed)
    assert events[0].code is FailureCode.INVALID_REQUEST


def test_a_job_has_exactly_one_request() -> None:
    request = RunRequest(scope=make_scope(), input="hi", config=make_config())
    with pytest.raises(ValidationError):
        RunnerJob(op="resume", run=request)
    with pytest.raises(ValidationError):
        RunnerJob(op="run")


def test_the_run_token_is_the_bearer_of_every_mcp_server() -> None:
    config = with_run_token(make_config(token="old"), "run-token-2")
    assert config.mcp_servers
    for server in config.mcp_servers:
        assert server.bearer_token is not None
        assert server.bearer_token.get_secret_value() == "run-token-2"
    assert "run-token-2" not in config.model_dump_json()


def test_the_cli_uses_the_run_token_as_the_model_key() -> None:
    env = claude_cli_env("tok", "https://proxy.example/")
    assert env["ANTHROPIC_API_KEY"] == "tok"
    assert env["ANTHROPIC_BASE_URL"] == "https://proxy.example/anthropic"
    assert "ANTHROPIC_BASE_URL" not in claude_cli_env("tok", None)


def test_forbidden_env_ignores_empty_values() -> None:
    assert forbidden_env({"OPENAI_API_KEY": "", "PATH": "/bin"}) == []
    assert forbidden_env({"OPENAI_API_KEY": "x", "E2B_API_KEY": "y"}) == [
        "E2B_API_KEY",
        "OPENAI_API_KEY",
    ]


def test_the_claude_runtime_gets_the_static_gate(tmp_path: object) -> None:
    pytest.importorskip("claude_agent_sdk")
    request = RunRequest(
        scope=make_scope(), input="hi", config=make_config(RuntimeKind.CLAUDE_AGENT_SDK)
    )
    job = RunnerJob(op="run", run=request, gate=GatePolicy(ask=("Bash",), deny=("Write",)))
    runtime = build_runtime(job, {**ENV, "SAMA_WORKDIR": str(tmp_path)})
    assert runtime.kind is RuntimeKind.CLAUDE_AGENT_SDK


def test_the_claude_runtime_gets_the_remote_gate(tmp_path: object) -> None:
    pytest.importorskip("claude_agent_sdk")
    from sama.runtime.remote_gate import RemoteGate

    request = RunRequest(
        scope=make_scope(), input="hi", config=make_config(RuntimeKind.CLAUDE_AGENT_SDK)
    )
    job = RunnerJob(op="run", run=request, gate_url="http://tools:8001/gate")
    runtime = build_runtime(job, {**ENV, "SAMA_WORKDIR": str(tmp_path)})
    gate = runtime._gate  # type: ignore[attr-defined]
    assert isinstance(gate, RemoteGate)
    assert "http://tools:8001/gate" in repr(gate)


def test_a_job_has_a_gate_url_or_a_static_gate() -> None:
    request = RunRequest(scope=make_scope(), input="hi", config=make_config())
    with pytest.raises(ValidationError, match="gate_url or a static gate"):
        RunnerJob(op="run", run=request, gate=GatePolicy(ask=("Bash",)), gate_url="http://x/gate")
    assert RunnerJob(op="run", run=request, gate_url="http://x/gate").gate_url


def test_the_claude_runtime_gets_an_archiver_when_the_job_has_an_archive_url(
    tmp_path: object,
) -> None:
    pytest.importorskip("claude_agent_sdk")
    from sama.runtime.archive import RemoteArchiver

    request = RunRequest(
        scope=make_scope(), input="hi", config=make_config(RuntimeKind.CLAUDE_AGENT_SDK)
    )
    env = {**ENV, "SAMA_WORKDIR": str(tmp_path)}
    plain = build_runtime(RunnerJob(op="run", run=request, gate_url="http://t/gate"), env)
    assert plain._archiver is None  # type: ignore[attr-defined]
    job = RunnerJob(op="run", run=request, gate_url="http://t/gate", archive_url="http://t/archive")
    archiver = build_runtime(job, env)._archiver  # type: ignore[attr-defined]
    assert isinstance(archiver, RemoteArchiver)
    assert "http://t/archive" in repr(archiver)
    static = build_runtime(
        RunnerJob(op="run", run=request, gate=GatePolicy(), archive_url="http://t/archive"), env
    )
    assert isinstance(static._archiver, RemoteArchiver)  # type: ignore[attr-defined]
