"""Tests of the runner protocol (src/sama/runtime/runner.py) with the fake runtime."""

from __future__ import annotations

import asyncio
import io
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

import sama.runtime.runner as runner_module
import sama.warm as warm_module
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
    fence,
    run_event_adapter,
    validate_event_stream,
)
from sama.runtime.checkpoints import CheckpointGate
from sama.runtime.fake import FakeScript
from sama.runtime.runner import (
    FORBIDDEN_ENV,
    RUN_TOKEN_ENV,
    TIMING_PREFIX,
    WORKDIR_ENV,
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


async def test_an_openai_job_without_a_session_url_is_an_invalid_request() -> None:
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
    checkpoints = runtime._gate  # type: ignore[attr-defined]
    # A checkpoint gate wraps the gate: a snapshot comes before each file change (M6.4).
    assert isinstance(checkpoints, CheckpointGate)
    assert checkpoints._checkpoints.workdir == str(tmp_path)
    gate = checkpoints._inner
    assert isinstance(gate, RemoteGate)
    assert "http://tools:8001/gate" in repr(gate)
    assert gate._steer  # the Claude runtime takes the notes of /steer


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


def test_the_openai_runtime_uses_the_control_plane_for_items_and_the_proxy_for_models(
    tmp_path: object,
) -> None:
    pytest.importorskip("agents")
    from sama.runtime.archive import RemoteArchiver
    from sama.runtime.remote_gate import RemoteGate
    from sama.runtime.session_items import RemoteSessionItems

    request = RunRequest(
        scope=make_scope(), input="hi", config=make_config(RuntimeKind.OPENAI_AGENTS_SDK)
    )
    job = RunnerJob(
        op="run",
        run=request,
        gate_url="http://t/gate",
        archive_url="http://t/archive",
        session_url="http://t/session-items",
    )
    env = {**ENV, "SAMA_WORKDIR": str(tmp_path), "SAMA_MODEL_URL": "http://proxy:8002/"}
    runtime = build_runtime(job, env)
    assert runtime.kind is RuntimeKind.OPENAI_AGENTS_SDK
    settings = runtime._settings  # type: ignore[attr-defined]
    assert settings.model_url == "http://proxy:8002/openai/v1"
    assert settings.api_key == "token-1"
    assert settings.cwd == str(tmp_path)
    assert "token-1" not in repr(settings)
    items = runtime._items  # type: ignore[attr-defined]
    assert isinstance(items, RemoteSessionItems)
    assert "http://t/session-items" in repr(items)
    assert "token-1" not in repr(items)
    gate = runtime._gate  # type: ignore[attr-defined]
    assert isinstance(gate, CheckpointGate)
    assert isinstance(gate._inner, RemoteGate)
    # The OpenAI runtime cannot give the notes of /steer to the model in a run, so it does not
    # take them. The notes go to the session after the run completes.
    assert not gate._inner._steer
    assert not runtime.capabilities().mid_run_steer
    assert isinstance(runtime._archiver, RemoteArchiver)  # type: ignore[attr-defined]


def test_the_openai_runtime_needs_the_model_url(tmp_path: object) -> None:
    request = RunRequest(
        scope=make_scope(), input="hi", config=make_config(RuntimeKind.OPENAI_AGENTS_SDK)
    )
    job = RunnerJob(op="run", run=request, session_url="http://t/session-items")
    with pytest.raises(ValueError, match="SAMA_MODEL_URL"):
        build_runtime(job, {**ENV, "SAMA_WORKDIR": str(tmp_path)})


async def test_a_runner_waits_for_the_runner_lock(tmp_path: Path) -> None:
    first = await runner_module._take_lock(str(tmp_path))
    assert first is not None
    second = asyncio.create_task(runner_module._take_lock(str(tmp_path)))
    await asyncio.sleep(0.2)
    assert not second.done()
    os.close(first)
    async with asyncio.timeout(2):
        fd = await second
    assert fd is not None
    os.close(fd)


async def test_a_runner_fails_when_the_last_runner_does_not_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "LOCK_WAIT_S", 0.1)
    first = await runner_module._take_lock(str(tmp_path))
    assert first is not None
    built: list[object] = []
    monkeypatch.setattr(runner_module, "build_runtime", lambda *args: built.append(args))
    env = {**ENV, WORKDIR_ENV: str(tmp_path)}
    out = io.StringIO()
    try:
        code = await serve(_lines([_job(_reply("hi")).model_dump_json()]), Output(out), env)
    finally:
        os.close(first)
    assert code == 3
    assert not built  # no second runtime starts while the lock is held
    [line] = out.getvalue().splitlines()
    failed = run_event_adapter.validate_json(line)
    assert isinstance(failed, RunFailed)
    assert failed.code is FailureCode.SANDBOX_ERROR
    assert failed.retryable


async def test_a_side_run_runs_next_to_the_runner_that_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M7.1: a side run has a runtime session of its own, so it does not wait for the lock."""
    monkeypatch.setattr(runner_module, "LOCK_WAIT_S", 0.1)
    first = await runner_module._take_lock(str(tmp_path))
    assert first is not None
    request = RunRequest(scope=make_scope("run-2"), input="hi", config=make_config(), parallel=True)
    job = RunnerJob(op="run", run=request, fake_scripts=(_reply("side"),))
    env = {**ENV, WORKDIR_ENV: str(tmp_path)}
    out = io.StringIO()
    try:
        code = await serve(_lines([job.model_dump_json()]), Output(out), env)
    finally:
        os.close(first)
    assert code == 0
    last = run_event_adapter.validate_json(out.getvalue().splitlines()[-1])
    assert isinstance(last, RunCompleted)
    assert last.output == "side"


async def test_no_lock_without_a_work_folder(tmp_path: Path) -> None:
    assert await runner_module._take_lock(str(tmp_path / "missing")) is None
    assert not (tmp_path / "missing").exists()


async def test_the_runner_writes_a_timing_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="sama.runner")
    env = {**ENV, WORKDIR_ENV: str(tmp_path)}
    out = io.StringIO()
    code = await serve(_lines([_job(_reply("hi")).model_dump_json()]), Output(out), env)
    assert code == 0
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith(TIMING_PREFIX)]
    assert len(lines) == 1
    for mark in ("job_s=", "lock_s=", "runtime_s=", "first_event_s=", "terminal_s=", "exit_s="):
        assert mark in lines[0]
    assert (tmp_path / ".sama" / "runner.lock").exists()


# -- the kept runtime (docs/architecture.md, Section 4.4) ---------------------------------------


class _Probe:
    """A runtime that records the state of the token relay while it runs."""

    kind = RuntimeKind.CLAUDE_AGENT_SDK

    def __init__(self, keep: runner_module.RunnerKeep) -> None:
        self.keep = keep
        self.active_during_run: list[bool] = []

    def capabilities(self) -> object:
        raise NotImplementedError

    async def run(self, request: RunRequest) -> AsyncIterator[RunEvent]:
        self.active_during_run.append(self.keep.relay.active)
        yield RunCompleted(output="ok", usage=Usage(output_tokens=1))

    async def cancel(self, run_id: str) -> None:
        return None


def _claude_job(*, parallel: bool = False) -> RunnerJob:
    request = RunRequest(
        scope=make_scope("run-1"),
        input="hi",
        config=make_config(RuntimeKind.CLAUDE_AGENT_SDK),
        parallel=parallel,
    )
    return RunnerJob(op="run", run=request)


KEEP_ENV = {**ENV, runner_module.MODEL_URL_ENV: "http://127.0.0.1:1"}


async def test_the_relay_has_the_token_only_while_the_job_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keep = runner_module.RunnerKeep(60, "http://127.0.0.1:1")
    probe = _Probe(keep)
    monkeypatch.setattr(runner_module, "build_runtime", lambda job, env, keep, mark: probe)
    env = {**KEEP_ENV, WORKDIR_ENV: str(tmp_path)}
    out = io.StringIO()
    try:
        code = await serve(_lines([_claude_job().model_dump_json()]), Output(out), env, keep=keep)
        assert code == 0
        assert probe.active_during_run == [True]
        assert not keep.relay.active  # the token left with the job
    finally:
        await keep.close()


async def test_the_relay_loses_the_token_when_the_runtime_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keep = runner_module.RunnerKeep(60, "http://127.0.0.1:1")

    def broken(job: RunnerJob, env: object, keep: object, mark: object) -> object:
        raise ValueError("no runtime")

    monkeypatch.setattr(runner_module, "build_runtime", broken)
    out = io.StringIO()
    try:
        env = {**KEEP_ENV, WORKDIR_ENV: str(tmp_path)}
        code = await serve(_lines([_claude_job().model_dump_json()]), Output(out), env, keep=keep)
        assert code == 2
        assert not keep.relay.active
    finally:
        await keep.close()


async def test_a_side_run_does_not_use_the_kept_runtime(tmp_path: Path) -> None:
    keep = runner_module.RunnerKeep(60, "http://127.0.0.1:1")
    try:
        job = _claude_job(parallel=True)
        assert runner_module._keeps(job, KEEP_ENV, keep) is None
        assert runner_module._keeps(_claude_job(), KEEP_ENV, keep) is keep
        assert runner_module._keeps(_claude_job(), KEEP_ENV, None) is None
        # The relay needs the URL of the model proxy.
        assert runner_module._keeps(_claude_job(), ENV, keep) is None
        assert runner_module._keeps(_job(), KEEP_ENV, keep) is None  # the fake runtime
    finally:
        await keep.close()


def test_a_kept_cli_gets_no_run_token_and_no_model_key() -> None:
    env = runner_module.kept_cli_env({RUN_TOKEN_ENV: "secret-token", "HOME": "/home/user"})
    assert env["HOME"] == "/home/user"
    assert RUN_TOKEN_ENV not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["DISABLE_TELEMETRY"] == "1"


async def test_the_kept_runtimes_are_made_when_they_are_needed() -> None:
    keep = runner_module.RunnerKeep(60, "http://127.0.0.1:1")
    assert not keep.holds
    assert keep.claude.idle_s == keep.openai.idle_s == 60
    assert not keep.holds
    await keep.reset()
    await keep.close()


def test_a_cold_runner_does_not_keep(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {warm_module.KEEP_ENV: "600"}
    monkeypatch.setattr(runner_module, "MODE", "cold")
    assert runner_module._keep_idle_s(env) == 0.0
    monkeypatch.setattr(runner_module, "MODE", "warm")
    monkeypatch.setattr(runner_module, "HOST", None)
    assert runner_module._keep_idle_s(env) == 0.0
    handle = warm_module.HostHandle("/x.host", "code", -1)
    monkeypatch.setattr(runner_module, "HOST", handle)
    assert runner_module._keep_idle_s(env) == 600.0
    assert runner_module._keep_idle_s({}) == 0.0
    assert runner_module._keep_idle_s({warm_module.KEEP_ENV: "soon"}) == 0.0


# -- the process fence (docs/architecture.md, Section 4.4) --------------------------------------


class _Seen:
    """A runtime that records what was alive when the run started, and holds the relay."""

    kind = RuntimeKind.CLAUDE_AGENT_SDK

    def __init__(self, keep: runner_module.RunnerKeep, survivor: int) -> None:
        self.keep = keep
        self.survivor = survivor
        self.survivor_gone_at_start: bool | None = None
        self.token_active = False

    def capabilities(self) -> object:
        raise NotImplementedError

    async def prepare(self, request: object) -> None:
        return None

    async def run(self, request: RunRequest) -> AsyncIterator[RunEvent]:
        self.survivor_gone_at_start = fence.exited(self.survivor)
        self.token_active = self.keep.relay.active
        yield RunCompleted(output="ok", usage=Usage(output_tokens=1))

    async def cancel(self, run_id: str) -> None:
        return None


SURVIVOR = """
import os, sys, time
folder, log = sys.argv[1], sys.argv[2]
open(log + '.pid', 'w').write(str(os.getpid()))
while True:
    try:
        name = open(os.path.join(folder, 'current')).read()
        token = open(os.path.join(folder, name)).read()
        open(log, 'a').write(token + '\\n')
    except OSError:
        pass
    time.sleep(0.02)
"""


def _start_survivor(folder: str, log_path: Path) -> int:
    """A process that a run left behind: it left its shell, and it reads the token file of the
    relay in a loop, as a hostile process of the last run could."""
    command = (
        f"nohup {sys.executable} -c {shlex.quote(SURVIVOR)} {folder} {log_path} >/dev/null 2>&1 &"
    )
    subprocess.run(["/bin/sh", "-c", command], check=True)
    pid_file = Path(f"{log_path}.pid")
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text():
            return int(pid_file.read_text())
        time.sleep(0.02)
    raise AssertionError("the survivor did not start")


async def test_a_process_of_the_last_run_is_killed_before_the_next_token_is_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The regression of the review: a process that outlived run N must never read the token of
    run N+1, and the clients that run N kept must not be reused."""
    fence.set_subreaper()
    keep = runner_module.RunnerKeep(60, "http://127.0.0.1:1")
    await keep.start()
    folder = os.path.dirname(keep.relay.shell_prefix)
    log_path = tmp_path / "seen.log"
    survivor = _start_survivor(folder, log_path)
    reset_calls: list[bool] = []
    original_reset = keep.reset

    async def reset() -> None:
        reset_calls.append(True)
        await original_reset()

    monkeypatch.setattr(keep, "reset", reset)
    try:
        # Run N: the survivor reads its token while the run is active.
        keep.relay.set_token("token-run-one")
        await asyncio.sleep(0.2)
        keep.relay.clear_token()
        assert "token-run-one" in log_path.read_text()
        assert not fence.exited(survivor)  # the run ended, and the survivor lives
        # Run N+1 starts through the runner. The fence runs before its token is active.
        probe = _Seen(keep, survivor)
        monkeypatch.setattr(runner_module, "build_runtime", lambda job, env, keep, mark: probe)
        env = {**KEEP_ENV, RUN_TOKEN_ENV: "token-run-two", WORKDIR_ENV: str(tmp_path)}
        out = io.StringIO()
        code = await serve(_lines([_claude_job().model_dump_json()]), Output(out), env, keep=keep)
        assert code == 0
        assert probe.token_active
        assert probe.survivor_gone_at_start is True  # killed before the token was active
        assert reset_calls, "a dirty fence must end the kept clients"
        await asyncio.sleep(0.2)
        assert "token-run-two" not in log_path.read_text()
        assert fence.exited(survivor)
    finally:
        await keep.close()


async def test_a_survivor_that_a_run_starts_is_found_at_the_end_of_that_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A run that leaves a process behind keeps nothing: the fence at the end finds it."""
    fence.set_subreaper()
    keep = runner_module.RunnerKeep(60, "http://127.0.0.1:1")
    await keep.start()
    folder = os.path.dirname(keep.relay.shell_prefix)
    log_path = tmp_path / "seen.log"
    resets: list[bool] = []
    original_reset = keep.reset

    async def reset() -> None:
        resets.append(True)
        await original_reset()

    monkeypatch.setattr(keep, "reset", reset)

    class Leaves(_Seen):
        async def run(self, request: RunRequest) -> AsyncIterator[RunEvent]:
            self.survivor = _start_survivor(folder, log_path)  # the run leaves it behind
            async for event in super().run(request):
                yield event

    probe = Leaves(keep, 0)
    monkeypatch.setattr(runner_module, "build_runtime", lambda job, env, keep, mark: probe)
    env = {**KEEP_ENV, RUN_TOKEN_ENV: "token-run-one", WORKDIR_ENV: str(tmp_path)}
    try:
        code = await serve(
            _lines([_claude_job().model_dump_json()]), Output(io.StringIO()), env, keep=keep
        )
        assert code == 0
        assert resets  # not kept
        assert fence.exited(probe.survivor)
        assert not keep.broken
    finally:
        await keep.close()


async def test_a_fence_that_cannot_kill_stops_the_runner_and_fails_the_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keep = runner_module.RunnerKeep(60, "http://127.0.0.1:1")
    probe = _Probe(keep)
    monkeypatch.setattr(runner_module, "build_runtime", lambda job, env, keep, mark: probe)

    def stuck(root: int, trusted: object = (), **kwargs: object) -> list[int]:
        raise fence.FenceError("a process does not stop")

    monkeypatch.setattr(fence, "kill_descendants", stuck)
    out = io.StringIO()
    env = {**KEEP_ENV, WORKDIR_ENV: str(tmp_path)}
    try:
        code = await serve(_lines([_claude_job().model_dump_json()]), Output(out), env, keep=keep)
        events = [run_event_adapter.validate_json(line) for line in out.getvalue().splitlines()]
        assert code == 3
        assert isinstance(events[-1], RunFailed)
        assert events[-1].retryable
        assert keep.broken
        assert probe.active_during_run == []  # the run never started, and no token was active
        assert not keep.relay.active
    finally:
        monkeypatch.undo()
        await keep.close()
