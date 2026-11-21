"""The runner: the process in the sandbox that runs one invocation of a runtime adapter.

Start it with ``python -m sama.runtime.runner`` (docs/architecture.md, Section 16.2).

Protocol, one JSON object per line:

- stdin, line 1: a ``RunnerJob``. It holds a ``RunRequest`` or a ``ResumeRequest``.
- stdin, later lines: ``{"op": "cancel"}`` stops the run. End of stdin also stops the run, so
  a runner never outlives the orchestrator that started it.
- stdout: one ``RunEvent`` per line. The last line is the terminal event.
- stderr: logs only.

The runner gets exactly one credential, the run token, in ``SAMA_RUN_TOKEN``. It uses the
token as the bearer for every MCP server and as the model key for the model proxy at
``SAMA_MODEL_URL``. The runner refuses to start if a provider key or another long-lived
secret is in its environment (decision D6).

Approval state is in the control plane, not here: each invocation is a new process. The
orchestrator refuses a second resume of the same approval before it starts a runner.

One main runner at a time runs in a sandbox. A runner takes the runner lock before it builds
the runtime, so it waits until the runner of the last run has stopped. A side run (M7.1,
``RunRequest.parallel``) has a runtime session of its own, so it runs next to the main runner
and takes no runner lock. The orchestrator does not
wait for that stop before it reports the run (Section 16.2). ``sama.warm`` can start this
module in a process that has the imports already (the warm runner).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import sys
import time
from collections.abc import AsyncIterator, Mapping
from typing import IO, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from sama.runtime.checkpoints import RUNNER_LOCK, CheckpointGate, Checkpoints
from sama.runtime.contract import (
    AgentRuntime,
    ContractViolation,
    FailureCode,
    ResumeRequest,
    RunConfig,
    RunEvent,
    RunFailed,
    RunRequest,
    RunScope,
    RuntimeKind,
    Usage,
    is_terminal,
    run_event_adapter,
)
from sama.runtime.fake import FakeRuntime, FakeScript
from sama.runtime.gating import GateDecision, ToolGate
from sama.sandbox.base import WORKDIR

log: Final = logging.getLogger("sama.runner")

RUN_TOKEN_ENV: Final = "SAMA_RUN_TOKEN"  # noqa: S105 -- a variable name
MODEL_URL_ENV: Final = "SAMA_MODEL_URL"
WORKDIR_ENV: Final = "SAMA_WORKDIR"
LOCK_FILE: Final = RUNNER_LOCK
"""The runner lock, in the work folder."""
LOCK_WAIT_S: Final = 20.0
"""The longest wait for the runner of the last run. The orchestrator kills a runner that does
not stop before this."""
TIMING_PREFIX: Final = "runner timing"
"""The start of the timing line on stderr. The orchestrator logs the line (Section 18.1)."""
CANCEL_GRACE_S: Final = 4.0
"""The Claude adapter waits this long for the CLI after a cancel. The orchestrator waits
longer before it kills the runner, and both fit in the 10-second cancel limit."""

FORBIDDEN_ENV: Final = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "E2B_API_KEY",
        "DATABASE_URL",
        "SAMA_DATABASE_URL",
        "SAMA_RUN_TOKEN_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
    }
)
"""Variables that must not be in the sandbox. The runner sets the model key for the CLI
itself, from the run token."""


class GatePolicy(BaseModel):
    """A static tool gate for tests and local development. ``ask`` and ``deny`` hold exact
    tool names. Other tools are allowed. A real run sets ``RunnerJob.gate_url`` instead: then
    the runner asks the control plane for each call (``RemoteGate``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ask: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()


class StaticGate:
    """The tool gate of a ``GatePolicy``."""

    def __init__(self, policy: GatePolicy) -> None:
        self._policy = policy

    async def check(
        self,
        scope: RunScope,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        call_id: str,
        child: bool = False,
    ) -> GateDecision:
        if tool_name in self._policy.deny:
            return GateDecision("deny", "the tool policy denies this tool")
        if tool_name in self._policy.ask:
            return GateDecision("ask", "the tool policy asks for approval")
        return GateDecision("allow")


class RunnerJob(BaseModel):
    """The first line on stdin. Exactly one of ``run`` and ``resume`` is set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: Literal["run", "resume"]
    run: RunRequest | None = None
    resume: ResumeRequest | None = None
    gate: GatePolicy = GatePolicy()
    gate_url: str | None = Field(default=None, min_length=1)
    """The gate endpoint of the control plane (``POST /gate``). When set, ``gate`` must be
    empty."""
    archive_url: str | None = Field(default=None, min_length=1)
    """The archive endpoint of the control plane (``POST /archive``). With it, the runtime
    gives its transcript to the control plane before it compacts (Section 6.3)."""
    session_url: str | None = Field(default=None, min_length=1)
    """The session items endpoint of the control plane (``POST /session-items``). The OpenAI
    runtime keeps its conversation items there, so the runner needs no database credential
    (Section 7.3). The OpenAI runtime needs it."""
    fake_scripts: tuple[FakeScript, ...] = ()
    """Scripts for the fake runtime. Only for tests."""

    @model_validator(mode="after")
    def _one_request(self) -> Self:
        if (self.op == "run") != (self.run is not None) or (self.op == "resume") != (
            self.resume is not None
        ):
            raise ValueError("set run for op=run, or resume for op=resume, not both")
        if self.gate_url is not None and (self.gate.ask or self.gate.deny):
            raise ValueError("set gate_url or a static gate, not both")
        return self

    @property
    def request(self) -> RunRequest | ResumeRequest:
        request = self.run or self.resume
        assert request is not None  # noqa: S101 -- the validator checks it
        return request


def forbidden_env(env: Mapping[str, str]) -> list[str]:
    """The names of forbidden variables that are set and not empty."""
    return sorted(name for name in FORBIDDEN_ENV if env.get(name))


def with_run_token(config: RunConfig, token: str) -> RunConfig:
    """The config with the run token as the bearer of every MCP server.

    The bearer is not part of the JSON or the fingerprint, so the runner sets it here.
    """
    secret = SecretStr(token)
    servers = tuple(s.model_copy(update={"bearer_token": secret}) for s in config.mcp_servers)
    return config.model_copy(update={"mcp_servers": servers})


def claude_cli_env(token: str, model_url: str | None) -> dict[str, str]:
    env = {
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1",
        "ANTHROPIC_API_KEY": token,
    }
    if model_url:
        # The Anthropic route of the model proxy (docs/architecture.md, Section 16.6).
        env["ANTHROPIC_BASE_URL"] = model_url.rstrip("/") + "/anthropic"
    return env


def build_runtime(job: RunnerJob, env: Mapping[str, str]) -> AgentRuntime:
    kind = job.request.config.runtime
    if kind is RuntimeKind.FAKE:
        return FakeRuntime(scripts=job.fake_scripts)
    if kind not in {RuntimeKind.CLAUDE_AGENT_SDK, RuntimeKind.OPENAI_AGENTS_SDK}:
        raise ValueError(f"the runner does not support runtime {kind}")
    if kind is RuntimeKind.OPENAI_AGENTS_SDK and (
        job.session_url is None or not env.get(MODEL_URL_ENV)
    ):
        raise ValueError("the openai runtime needs session_url and SAMA_MODEL_URL")
    token = env[RUN_TOKEN_ENV]
    workdir = env.get(WORKDIR_ENV) or WORKDIR
    os.makedirs(workdir, exist_ok=True)
    archiver = None
    if job.archive_url is not None:
        from sama.runtime.archive import RemoteArchiver

        archiver = RemoteArchiver(job.archive_url, token)
    gate: ToolGate
    if job.gate_url is not None:
        from sama.runtime.remote_gate import RemoteGate

        # Only the Claude runtime takes the notes of ``/steer`` in a run
        # (``Capabilities.mid_run_steer``, Section 4.3).
        gate = RemoteGate(job.gate_url, token, steer=kind is RuntimeKind.CLAUDE_AGENT_SDK)
    else:
        gate = StaticGate(job.gate)
    # A snapshot of the work folder before each call that can change files (Section 15.3).
    gate = CheckpointGate(gate, Checkpoints(workdir))

    if kind is RuntimeKind.OPENAI_AGENTS_SDK:
        from sama.runtime.openai_agents import OpenAIRuntime, OpenAISettings
        from sama.runtime.session_items import RemoteSessionItems

        model_url = env[MODEL_URL_ENV]
        assert job.session_url is not None  # noqa: S101 -- checked above
        settings = OpenAISettings(
            cwd=workdir,
            # The OpenAI route of the model proxy (docs/architecture.md, Section 16.6).
            model_url=model_url.rstrip("/") + "/openai/v1",
            api_key=token,
            cancel_grace_s=CANCEL_GRACE_S,
        )
        return OpenAIRuntime(
            items=RemoteSessionItems(job.session_url, token),
            gate=gate,
            settings=settings,
            archiver=archiver,
        )

    from sama.runtime.claude import ClaudeRuntime, ClaudeSettings
    from sama.runtime.claude_transcript import cli_session_exists

    claude_settings = ClaudeSettings(
        cwd=workdir,
        env=claude_cli_env(token, env.get(MODEL_URL_ENV)),
        cancel_grace_s=CANCEL_GRACE_S,
        session_exists=cli_session_exists,
    )
    return ClaudeRuntime(gate=gate, settings=claude_settings, archiver=archiver)


def _failed(code: FailureCode, message: str, usage: Usage | None = None) -> RunFailed:
    return RunFailed(code=code, message=message, usage=usage or Usage())


class Output:
    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream

    def event(self, event: RunEvent) -> None:
        self._stream.write(run_event_adapter.dump_json(event).decode() + "\n")
        self._stream.flush()


async def _stdin_lines() -> AsyncIterator[str]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while raw := await reader.readline():
        yield raw.decode()


async def _watch_control(
    lines: AsyncIterator[str],
    runtime: AgentRuntime,
    run_id: str,
    done: asyncio.Event,
    requested: asyncio.Event,
) -> None:
    """Reads control lines. A cancel line or the end of stdin cancels the run.

    It sets ``requested`` too: a cancel that comes before the adapter registers the run has
    no effect in the adapter, so ``serve`` sends it again when the first event comes.
    """
    async for line in lines:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log.warning("the runner ignored a control line that is not JSON")
            continue
        if isinstance(message, dict) and message.get("op") == "cancel":
            break
        log.warning("the runner ignored an unknown control line")
    if not done.is_set():
        requested.set()
        await runtime.cancel(run_id)


class _Timing:
    """The times of one runner, for the timing line on stderr."""

    def __init__(self, started: float, mode: str) -> None:
        self.mode = mode
        self.marks: dict[str, float] = {"start": started}

    def mark(self, name: str) -> None:
        self.marks.setdefault(name, time.time())

    def line(self) -> str:
        start = self.marks["start"]
        parts = " ".join(
            f"{name}_s={at - start:.3f}" for name, at in self.marks.items() if name != "start"
        )
        return f"{TIMING_PREFIX} mode={self.mode} {parts}"


STARTED_AT: float | None = None
"""The time that the runner process started. ``sama.warm`` sets it to the start of the client
process. ``None`` means the import time of this module."""
MODE: str = "cold"
"""``warm`` when ``sama.warm`` started the runner in a warm process."""
_IMPORTED_AT: Final = time.time()


def _open_lock(workdir: str) -> int | None:
    """Opens the runner lock file. A work folder that does not exist had no runner, so there
    is no lock."""
    if not os.path.isdir(workdir):
        return None
    path = os.path.join(workdir, LOCK_FILE)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        log.warning("the runner lock did not open: %r", error)
        return None


class LockBusyError(Exception):
    """The runner of the last run of the session did not stop in ``LOCK_WAIT_S``."""


async def _take_lock(workdir: str) -> int | None:
    """Takes the runner lock and returns its file descriptor, or None if the work folder does
    not exist. It waits up to ``LOCK_WAIT_S`` for the runner of the last run, then raises
    ``LockBusyError``: two Claude CLIs must never use the files of one session."""
    fd = _open_lock(workdir)
    if fd is None:
        return None
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise LockBusyError from None
            await asyncio.sleep(0.02)


async def serve(lines: AsyncIterator[str], out: Output, env: Mapping[str, str]) -> int:
    """Runs one job. Returns the exit code: 0 if the stream ended with a terminal event."""
    timing = _Timing(STARTED_AT if STARTED_AT is not None else _IMPORTED_AT, MODE)
    try:
        return await _serve(lines, out, env, timing)
    finally:
        timing.mark("exit")
        log.info("%s", timing.line())


async def _serve(
    lines: AsyncIterator[str], out: Output, env: Mapping[str, str], timing: _Timing
) -> int:
    first = await anext(aiter(lines), None)
    timing.mark("job")
    if first is None:
        out.event(_failed(FailureCode.INVALID_REQUEST, "no job on stdin"))
        return 2
    try:
        job = RunnerJob.model_validate_json(first)
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors())
        out.event(_failed(FailureCode.INVALID_REQUEST, f"the job is not valid: {problems}"))
        return 2
    except ContractViolation as error:  # a fake script that breaks the stream rules
        out.event(_failed(FailureCode.INVALID_REQUEST, f"the job is not valid: {error}"))
        return 2
    leaked = forbidden_env(env)
    if leaked:
        message = f"the sandbox has forbidden variables: {', '.join(leaked)}"
        out.event(_failed(FailureCode.SANDBOX_ERROR, message))
        return 3
    token = env.get(RUN_TOKEN_ENV)
    if not token:
        out.event(_failed(FailureCode.SANDBOX_ERROR, f"{RUN_TOKEN_ENV} is not set"))
        return 3
    try:
        # A side run (M7.1) runs next to the runner of the session: it has a runtime session of
        # its own, so it takes no runner lock.
        parallel = job.request.parallel
        lock = None if parallel else await _take_lock(env.get(WORKDIR_ENV) or WORKDIR)
    except LockBusyError:
        message = "the runner of the last run of this session did not stop"
        out.event(RunFailed(code=FailureCode.SANDBOX_ERROR, message=message, retryable=True))
        return 3
    timing.mark("lock")
    try:
        return await _run_job(job, lines, out, env, token, timing)
    finally:
        if lock is not None:
            os.close(lock)


async def _run_job(
    job: RunnerJob,
    lines: AsyncIterator[str],
    out: Output,
    env: Mapping[str, str],
    token: str,
    timing: _Timing,
) -> int:
    try:
        runtime = build_runtime(job, env)
    except (ValueError, ImportError, ContractViolation) as error:
        out.event(_failed(FailureCode.INVALID_REQUEST, str(error)))
        return 2
    timing.mark("runtime")

    config = with_run_token(job.request.config, token)
    stream: AsyncIterator[RunEvent]
    if job.run is not None:
        stream = runtime.run(job.run.model_copy(update={"config": config}))
    else:
        assert job.resume is not None  # noqa: S101 -- the validator checks it
        stream = runtime.resume(job.resume.model_copy(update={"config": config}))

    run_id = job.request.scope.run_id
    done = asyncio.Event()
    requested = asyncio.Event()
    control = asyncio.create_task(_watch_control(lines, runtime, run_id, done, requested))
    usage = Usage()
    ended = False
    try:
        async for event in stream:
            if requested.is_set() and not is_terminal(event):
                requested.clear()  # The control task sets it once, so this repeats it once.
                await runtime.cancel(run_id)
            usage = event.usage
            ended = is_terminal(event)
            timing.mark("first_event")
            out.event(event)
            if ended:
                timing.mark("terminal")
                break
    except Exception as error:  # the adapter broke the contract; report it, do not crash
        log.exception("the runtime raised")
        if not ended:
            out.event(
                _failed(FailureCode.INTERNAL, f"the runtime raised {type(error).__name__}", usage)
            )
            ended = True
    finally:
        done.set()
        control.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await control
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()
        close_runtime = getattr(runtime, "aclose", None)
        if close_runtime is not None:
            await close_runtime()
    if not ended:
        out.event(
            _failed(FailureCode.INTERNAL, "the runtime ended without a terminal event", usage)
        )
        return 1
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    # The Claude CLI and the SDK must not write to stdout: it carries the event stream.
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        code = asyncio.run(serve(_stdin_lines(), Output(real_stdout), os.environ))
    finally:
        sys.stdout = real_stdout
    raise SystemExit(code)


if __name__ == "__main__":
    main()
