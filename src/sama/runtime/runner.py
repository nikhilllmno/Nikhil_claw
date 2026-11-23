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

A warm runner can stay alive after a run that ended well and serve the next runs of the session
(the host, ``sama.runtime.host``, Section 4.4). The host keeps the Claude CLI and the MCP
connections of the last run. It never keeps the run token: the token relay
(``sama.runtime.relay``) holds the token of the active run, and the kept clients hold only the
secret of the relay.
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
from collections.abc import AsyncIterator, Callable, Mapping
from typing import IO, TYPE_CHECKING, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from sama import warm
from sama.runtime import host
from sama.runtime.checkpoints import RUNNER_LOCK, CheckpointGate, Checkpoints
from sama.runtime.contract import (
    AgentRuntime,
    ContractViolation,
    FailureCode,
    ResumeRequest,
    RunConfig,
    RunEvent,
    RunFailed,
    RunPhase,
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

if TYPE_CHECKING:
    from sama.runtime.claude import ClaudeKeeper
    from sama.runtime.openai_agents import OpenAIKeeper
    from sama.runtime.relay import TokenRelay

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
SESSION_ROUTE: Final = "session-items"
"""The relay route of the session items endpoint (a name that no MCP server has)."""
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


def kept_cli_env(env: Mapping[str, str]) -> dict[str, str]:
    """The environment of a kept CLI: the environment of the job without the run token, and the
    CLI switches. The model key, the proxy URL, and the token of the shell commands come from the
    token relay."""
    kept = {name: value for name, value in env.items() if name != RUN_TOKEN_ENV}
    switches = claude_cli_env("", None)
    del switches["ANTHROPIC_API_KEY"]
    kept.update(switches)
    return kept


class RunnerKeep:
    """What a kept runner holds between jobs (Section 4.4): the token relay and the keepers of
    the two runtimes. One for each runner process."""

    def __init__(self, idle_s: float, model_url: str) -> None:
        from sama.runtime.relay import TokenRelay

        self.idle_s = idle_s
        self.relay: TokenRelay = TokenRelay(model_url)
        self._claude: ClaudeKeeper | None = None
        self._openai: OpenAIKeeper | None = None

    @property
    def claude(self) -> ClaudeKeeper:
        if self._claude is None:
            from sama.runtime.claude import ClaudeKeeper

            self._claude = ClaudeKeeper(idle_s=self.idle_s)
        return self._claude

    @property
    def openai(self) -> OpenAIKeeper:
        if self._openai is None:
            from sama.runtime.openai_agents import OpenAIKeeper

            self._openai = OpenAIKeeper(idle_s=self.idle_s)
        return self._openai

    @property
    def holds(self) -> bool:
        return (self._claude is not None and self._claude.holds) or (
            self._openai is not None and self._openai.holds
        )

    async def start(self) -> None:
        await self.relay.start()

    async def reset(self) -> None:
        """Ends the kept clients. The next job starts them again."""
        if self._claude is not None:
            await self._claude.close()
        if self._openai is not None:
            await self._openai.close()

    async def close(self) -> None:
        if self._claude is not None:
            await self._claude.close()
        if self._openai is not None:
            await self._openai.close()
        await self.relay.aclose()


def build_runtime(
    job: RunnerJob,
    env: Mapping[str, str],
    keep: RunnerKeep | None = None,
    mark: Callable[[str], None] | None = None,
) -> AgentRuntime:
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
            relay=keep.relay if keep is not None else None,
            keeper=keep.openai if keep is not None else None,
            mark=mark,
        )
        items = RemoteSessionItems(job.session_url, token)
        if keep is not None:
            # The call goes over the connection of the relay, which stays open between runs.
            keep.relay.set_mcp({SESSION_ROUTE: job.session_url})
            items = RemoteSessionItems(keep.relay.mcp_url(SESSION_ROUTE), keep.relay.secret)
        return OpenAIRuntime(
            items=items,
            gate=gate,
            settings=settings,
            archiver=archiver,
        )

    from sama.runtime.claude import ClaudeRuntime, ClaudeSettings
    from sama.runtime.claude_transcript import cli_session_exists

    claude_settings = ClaudeSettings(
        cwd=workdir,
        env=(
            kept_cli_env(env) if keep is not None else claude_cli_env(token, env.get(MODEL_URL_ENV))
        ),
        cancel_grace_s=CANCEL_GRACE_S,
        session_exists=cli_session_exists,
        relay=keep.relay if keep is not None else None,
        keeper=keep.claude if keep is not None else None,
        mark=mark,
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
"""``warm`` when ``sama.warm`` started the runner in a warm process. The timing line of a job
that a host ran has ``host``."""
HOST: warm.HostHandle | None = None
"""Set by ``sama.warm`` in a warm runner: the handle that lets this runner become a host after
a run that ended well (Section 4.4)."""
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


async def serve(
    lines: AsyncIterator[str],
    out: Output,
    env: Mapping[str, str],
    *,
    keep: RunnerKeep | None = None,
    started: float | None = None,
    mode: str | None = None,
) -> int:
    """Runs one job. Returns the exit code: 0 if the stream ended with a terminal event.

    With ``keep``, a job of the Claude or the OpenAI runtime uses the token relay and the kept
    clients of the runner (Section 4.4)."""
    if started is None:
        started = STARTED_AT if STARTED_AT is not None else _IMPORTED_AT
    timing = _Timing(started, mode or MODE)
    try:
        return await _serve(lines, out, env, timing, keep)
    finally:
        timing.mark("exit")
        log.info("%s", timing.line())


async def _serve(
    lines: AsyncIterator[str],
    out: Output,
    env: Mapping[str, str],
    timing: _Timing,
    keep: RunnerKeep | None,
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
        return await _run_job(job, lines, out, env, token, timing, keep)
    finally:
        if lock is not None:
            os.close(lock)


_KEPT_KINDS: Final = frozenset({RuntimeKind.CLAUDE_AGENT_SDK, RuntimeKind.OPENAI_AGENTS_SDK})


def _keeps(job: RunnerJob, env: Mapping[str, str], keep: RunnerKeep | None) -> RunnerKeep | None:
    """``keep`` if this job can use the kept clients, else ``None``. A side run has a runtime
    session of its own and runs next to the host, so it never uses them. The relay needs the
    URL of the model proxy."""
    if keep is None or job.request.parallel or not env.get(MODEL_URL_ENV):
        return None
    if job.request.config.runtime not in _KEPT_KINDS:
        return None
    return keep


async def _run_job(
    job: RunnerJob,
    lines: AsyncIterator[str],
    out: Output,
    env: Mapping[str, str],
    token: str,
    timing: _Timing,
    keep: RunnerKeep | None = None,
) -> int:
    use = _keeps(job, env, keep)
    if use is None:
        return await _run_job_with(job, lines, out, env, token, timing, None)
    await use.start()
    # The relay holds the token for this job only. The kept clients never see it.
    use.relay.set_token(token)
    try:
        return await _run_job_with(job, lines, out, env, token, timing, use)
    finally:
        use.relay.clear_token()


async def _run_job_with(
    job: RunnerJob,
    lines: AsyncIterator[str],
    out: Output,
    env: Mapping[str, str],
    token: str,
    timing: _Timing,
    keep: RunnerKeep | None,
) -> int:
    try:
        runtime = build_runtime(job, env, keep, timing.mark)
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
            if isinstance(event, RunPhase):
                timing.mark("thinking")  # progress only: no usage, not the first event
                out.event(event)
                continue
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


class _LiveStderr(logging.Handler):
    """Writes each log line to the stderr that the runner has now. A host serves one job after
    the other, and each job has the stderr of its own client."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stderr.write(self.format(record) + "\n")
            sys.stderr.flush()
        except Exception:
            self.handleError(record)


def _keep_idle_s(env: Mapping[str, str]) -> float:
    """The idle time of a kept runner, or 0 when this runner cannot become a host: it must be
    a warm runner, and ``SAMA_RUNNER_KEEP_S`` must be set."""
    if MODE != "warm" or HOST is None:
        return 0.0
    try:
        return max(0.0, float(env.get(warm.KEEP_ENV, "0")))
    except ValueError:
        return 0.0


async def _main(out_stream: IO[str]) -> int:
    env = dict(os.environ)
    idle_s = _keep_idle_s(env)
    keep = RunnerKeep(idle_s, env[MODEL_URL_ENV]) if idle_s > 0 and env.get(MODEL_URL_ENV) else None
    job_input = host.JobInput(0)
    await job_input.start()
    try:
        code = await serve(job_input.lines(), Output(out_stream), env, keep=keep)
    finally:
        await job_input.close()
    if keep is None:
        return code
    return await _become_host(keep, code)


async def _become_host(keep: RunnerKeep, code: int) -> int:
    """After a run that ended well, stays alive as the host of the session. Else exits."""
    handle = HOST
    listener = None
    if handle is not None and keep.holds and code == 0:
        try:
            listener = host.HostListener.bind(handle.path)
        except OSError as error:
            log.warning("the host socket did not open: %r", error)
    if handle is None or listener is None:
        await keep.close()
        return code
    # The socket exists before the client hears the exit code, so the next client finds the
    # host (or waits in its queue) and does not start a second CLI.
    handle.release(code)
    _detach_stdio()
    try:
        await host.serve_jobs(
            listener, lambda job: _run_hosted(keep, job), code=handle.code, idle_s=keep.idle_s
        )
    finally:
        await keep.close()
    return 0


_NULL: IO[str] | None = None


def _detach_stdio() -> None:
    """Points the standard files at ``/dev/null``. The first client has gone, and the host must
    not hold its pipes: the orchestrator waits for them to close."""
    global _NULL
    null = os.open(os.devnull, os.O_RDWR)
    for target in (0, 1, 2):
        os.dup2(null, target)
    os.close(null)
    _NULL = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 -- lives as long as the host
    sys.stdout = sys.stderr = _NULL


async def _run_hosted(keep: RunnerKeep, job: host.HostedJob) -> int:
    """Runs the job of one client in the host, with the files and the environment of the client."""
    request = job.request
    stdin_fd, stdout_fd, stderr_fd = job.fds
    env = {str(name): str(value) for name, value in request["env"].items()}
    with contextlib.suppress(OSError):
        os.chdir(str(request["cwd"]))
    # The shell tool of the OpenAI runtime runs in this process, so its commands see this.
    os.environ.clear()
    os.environ.update(env)
    out = os.fdopen(stdout_fd, "w", encoding="utf-8", closefd=False)
    err = os.fdopen(stderr_fd, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)
    previous = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = err
    job_input = host.JobInput(stdin_fd)
    job.input = job_input
    try:
        if env.get(warm.FRESH_ENV) == "1":
            await keep.reset()
        await job_input.start()
        return await serve(
            job_input.lines(),
            Output(out),
            env,
            keep=keep,
            started=float(request["started"]),
            mode="host",
        )
    finally:
        await job_input.close()
        with contextlib.suppress(OSError, ValueError):
            out.flush()
            err.flush()
        sys.stdout, sys.stderr = previous
        os.environ.pop(RUN_TOKEN_ENV, None)


def main() -> None:
    logging.basicConfig(level=logging.INFO, handlers=[_LiveStderr()])
    # The Claude CLI and the SDK must not write to stdout: it carries the event stream.
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        code = asyncio.run(_main(real_stdout))
    finally:
        sys.stdout = real_stdout
    raise SystemExit(code)


if __name__ == "__main__":
    main()
