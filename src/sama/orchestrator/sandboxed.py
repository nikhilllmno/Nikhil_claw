"""Runs a runtime adapter inside the session sandbox (docs/architecture.md, Section 16.2).

``SandboxedRuntime`` implements ``AgentRuntime``. For each ``run`` or ``resume`` it:

1. gets the session sandbox: it resumes the sandbox of the session, or creates one if the
   session has none or the provider lost it,
2. issues a run token for exactly this run scope,
3. starts the runner (``python -m sama.runtime.runner``) with the token as its only
   credential, and sends the job on stdin,
4. streams the runner events from stdout.

It measures and logs the sandbox start and the time to the first event (roadmap M1.6).

The runner is a new process for each invocation, so the rules that span invocations are
enforced here: one stream per run id, each approval resumes once, and a cancelled run cannot
resume. The session-to-sandbox map and these rules are in memory for now. The run orchestrator
(M1.10) moves them to Postgres.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from pydantic import ValidationError

from sama.runtime.contract import (
    ApprovalRequired,
    Budget,
    Capabilities,
    FailureCode,
    ResumeRequest,
    RunEvent,
    RunFailed,
    RunRequest,
    RunScope,
    RuntimeKind,
    Usage,
    is_terminal,
    run_event_adapter,
)
from sama.runtime.runner import (
    MODEL_URL_ENV,
    RUN_TOKEN_ENV,
    WORKDIR_ENV,
    FakeScriptSpec,
    GatePolicy,
    RunnerJob,
)
from sama.sandbox.base import (
    Sandbox,
    SandboxError,
    SandboxNotFoundError,
    SandboxProcess,
    SandboxProvider,
    SandboxSpec,
    Stopwatch,
)
from sama.tenancy import RUN_TOKEN_MAX_TTL_S, RunTokenSigner

log: Final = logging.getLogger("sama.orchestrator")

RUNNER_ARGV: Final = ("/opt/sama/.venv/bin/python", "-m", "sama.runtime.runner")
"""The runner in the sama image (``sandbox/Dockerfile``)."""
DEFAULT_RUN_WALL_TIME_S: Final = 60 * 60
TOKEN_MARGIN_S: Final = 10 * 60
"""The run token lives this long after the wall-time limit, for the start and the stop."""
CANCEL_KILL_AFTER_S: Final = 7.0
"""After a cancel line, the orchestrator waits this long for the runner to end the stream,
then kills the runner. The runner gives the Claude CLI 4 seconds. Both fit in 10 seconds."""
EXIT_WAIT_S: Final = 10.0
_MEMORY: Final = 10_000

CAPABILITIES: Final[Mapping[RuntimeKind, Capabilities]] = {
    RuntimeKind.CLAUDE_AGENT_SDK: Capabilities(
        streaming=True,
        native_subagents=True,
        native_compaction=True,
        native_skills=True,
        native_file_checkpoints=True,
        resumable_sessions=True,
        mcp_client=True,
    ),
    RuntimeKind.FAKE: Capabilities(
        streaming=True,
        native_subagents=False,
        native_compaction=False,
        native_skills=False,
        native_file_checkpoints=False,
        resumable_sessions=True,
        mcp_client=False,
    ),
}
"""The capabilities of the adapters that the runner can start. A test checks that they are
the same as the adapters report."""


class _Recent:
    """A set that forgets its oldest items, so a long-lived process does not grow."""

    def __init__(self) -> None:
        self._items: dict[str, None] = {}

    def add(self, item: str) -> None:
        self._items[item] = None
        while len(self._items) > _MEMORY:
            del self._items[next(iter(self._items))]

    def discard(self, item: str) -> None:
        self._items.pop(item, None)

    def __contains__(self, item: object) -> bool:
        return item in self._items


@dataclass
class _Active:
    process: SandboxProcess | None = None
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True, slots=True)
class SandboxSettings:
    """``template`` is the provider template or image. ``allow_out`` is the egress
    allowlist of every session sandbox. ``model_url`` is the model proxy (M1.7)."""

    template: str
    model_url: str | None = None
    allow_out: tuple[str, ...] = ()
    runner_argv: tuple[str, ...] = RUNNER_ARGV
    extra_env: Mapping[str, str] = field(default_factory=dict)
    """More variables for the runner. Never a secret."""


class SandboxedRuntime:
    """An ``AgentRuntime`` that runs another runtime kind in the session sandbox."""

    def __init__(
        self,
        *,
        kind: RuntimeKind,
        provider: SandboxProvider,
        signer: RunTokenSigner,
        settings: SandboxSettings,
        gate: GatePolicy | None = None,
    ) -> None:
        if kind not in CAPABILITIES:
            raise ValueError(f"the runner does not support runtime {kind}")
        self._kind = kind
        self._provider = provider
        self._signer = signer
        self._settings = settings
        self._gate = gate or GatePolicy()
        self._sandbox_ids: dict[tuple[str, str], str] = {}
        self._session_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._active: dict[str, _Active] = {}
        self._paused = _Recent()
        self._used_approvals = _Recent()
        self._cancelled = _Recent()
        self.fake_scripts: list[FakeScriptSpec] = []
        """Scripts for the fake runtime in the sandbox. The next run takes all of them."""

    @property
    def kind(self) -> RuntimeKind:
        return self._kind

    def capabilities(self) -> Capabilities:
        return CAPABILITIES[self._kind]

    def sandbox_id(self, org_id: str, session_id: str) -> str | None:
        return self._sandbox_ids.get((org_id, session_id))

    # -- the protocol ----------------------------------------------------------------------

    async def run(self, request: RunRequest) -> AsyncIterator[RunEvent]:
        run_id = request.scope.run_id
        problem = self._check_config(request.config.runtime)
        if problem is None and (run_id in self._active or run_id in self._paused):
            problem = f"run {run_id} is already active"
        if problem is not None:
            yield _invalid(problem)
            return
        scripts, self.fake_scripts = tuple(self.fake_scripts), []
        job = RunnerJob(op="run", run=request, gate=self._gate, fake_scripts=scripts)
        stream = self._invoke(job, request.scope, request.budget, Usage())
        async with contextlib.aclosing(stream):
            async for event in stream:
                yield event

    async def resume(self, request: ResumeRequest) -> AsyncIterator[RunEvent]:
        run_id = request.scope.run_id
        problem = self._check_config(request.config.runtime)
        if problem is None:
            if request.approval_id in self._used_approvals:
                problem = f"approval {request.approval_id} was already used"
            elif run_id in self._cancelled:
                problem = f"run {run_id} was cancelled"
            elif run_id in self._active:
                problem = f"run {run_id} is already active"
        if problem is not None:
            yield _invalid(problem)
            return
        self._used_approvals.add(request.approval_id)
        self._paused.discard(run_id)
        job = RunnerJob(op="resume", resume=request, gate=self._gate)
        # The usage at the pause is in the opaque runtime state. Only the runner reads it, so a
        # failure before the runner starts reports zero usage.
        stream = self._invoke(job, request.scope, request.budget, Usage())
        async with contextlib.aclosing(stream):
            async for event in stream:
                yield event

    async def cancel(self, run_id: str) -> None:
        self._cancelled.add(run_id)
        self._paused.discard(run_id)
        active = self._active.get(run_id)
        if active is None:
            return
        active.cancelled.set()
        process = active.process
        if process is None:
            return  # The stream sees the flag when it has started the runner.
        await _send_cancel(process)
        # The runner writes its last event and exits. Its output is buffered, so this does not
        # wait for the caller to read the stream.
        try:
            await asyncio.wait_for(process.wait(), timeout=CANCEL_KILL_AFTER_S)
        except TimeoutError:
            log.warning("the runner did not stop after cancel; killing it run=%s", run_id)
            await process.kill()

    async def session_sandbox(self, org_id: str, session_id: str) -> Sandbox | None:
        """The sandbox of a session, resumed, or None if the session has none."""
        sandbox_id = self._sandbox_ids.get((org_id, session_id))
        if sandbox_id is None:
            return None
        try:
            return await self._provider.resume(sandbox_id, self._spec(org_id, session_id))
        except SandboxNotFoundError:
            return None

    async def close_sandboxes(self, *, kill: bool = False) -> None:
        """Pauses every session sandbox that this runtime knows, or kills it. For a shutdown
        and for tests."""
        for org_id, session_id in list(self._sandbox_ids):
            with contextlib.suppress(SandboxError):
                sandbox = await self.session_sandbox(org_id, session_id)
                if sandbox is not None:
                    await (sandbox.kill() if kill else sandbox.pause())
            if kill:
                del self._sandbox_ids[(org_id, session_id)]

    # -- internals -------------------------------------------------------------------------

    def _check_config(self, runtime: RuntimeKind) -> str | None:
        if runtime is not self._kind:
            return f"config is for runtime {runtime}, not {self._kind}"
        return None

    def _spec(self, org_id: str, session_id: str) -> SandboxSpec:
        return SandboxSpec(
            org_id=org_id,
            session_id=session_id,
            template=self._settings.template,
            allow_out=self._settings.allow_out,
        )

    async def acquire(self, scope: RunScope, budget: Budget) -> Sandbox:
        """Resumes the session sandbox, or creates one. Logs the start time."""
        key = (scope.org_id, scope.session_id)
        lock = self._session_locks.setdefault(key, asyncio.Lock())
        async with lock:
            spec = self._spec(*key)
            sandbox: Sandbox | None = None
            known = self._sandbox_ids.get(key)
            if known is not None:
                try:
                    sandbox = await self._provider.resume(known, spec)
                except SandboxNotFoundError:
                    log.warning("the session sandbox is gone; creating a new one sandbox=%s", known)
            if sandbox is None:
                sandbox = await self._provider.create(spec)
                self._sandbox_ids[key] = sandbox.id
            await sandbox.keep_alive(int(_wall_time_s(budget) + TOKEN_MARGIN_S))
            return sandbox

    async def _invoke(
        self, job: RunnerJob, scope: RunScope, budget: Budget, start: Usage
    ) -> AsyncGenerator[RunEvent]:
        run_id = scope.run_id
        active = _Active()
        self._active[run_id] = active
        watch = Stopwatch()
        usage = start
        process: SandboxProcess | None = None
        terminal = False
        try:
            try:
                sandbox = await self.acquire(scope, budget)
                if active.cancelled.is_set():
                    yield _cancelled(usage)
                    return
                ttl = min(_wall_time_s(budget) + TOKEN_MARGIN_S, RUN_TOKEN_MAX_TTL_S)
                env = {
                    WORKDIR_ENV: sandbox.workdir,
                    **self._settings.extra_env,
                    RUN_TOKEN_ENV: self._signer.issue(scope, ttl_s=ttl),
                }
                if self._settings.model_url:
                    env[MODEL_URL_ENV] = self._settings.model_url
                process = await sandbox.start(self._settings.runner_argv, env=env)
                active.process = process
                await process.write(job.model_dump_json() + "\n")
            except SandboxError as error:
                log.warning("the sandbox failed run=%s: %s", run_id, error)
                yield _sandbox_failed(f"the sandbox failed: {error}", usage)
                return
            if active.cancelled.is_set():
                await _send_cancel(process)
            first = True
            async for line in process.lines():
                event = _parse(line)
                if event is None:
                    continue
                if first:
                    first = False
                    log.info("first runner event run=%s after_s=%.3f", run_id, watch.elapsed())
                usage = event.usage
                if is_terminal(event):
                    terminal = True
                    # The run ends when the runner has stopped, so the next invocation of the
                    # session never meets a runner that is still shutting down.
                    await process.close_stdin()
                    if await _bounded_wait(process) is None:
                        await process.kill()
                    self._release(run_id, active)
                    if isinstance(event, ApprovalRequired) and run_id not in self._cancelled:
                        self._paused.add(run_id)
                yield event
                if terminal:
                    return
            code = await _bounded_wait(process)
            if active.cancelled.is_set():
                yield _cancelled(usage)
                return
            log.warning(
                "the runner ended without a terminal event run=%s code=%s stderr=%s",
                run_id,
                code,
                process.stderr_tail()[-2000:],
            )
            yield _sandbox_failed(f"the runner ended without a result (exit code {code})", usage)
        finally:
            self._release(run_id, active)
            if process is not None and not terminal:
                await process.kill()

    def _release(self, run_id: str, owner: _Active) -> None:
        if self._active.get(run_id) is owner:
            del self._active[run_id]


def _wall_time_s(budget: Budget) -> float:
    return budget.max_wall_time_s or DEFAULT_RUN_WALL_TIME_S


async def _bounded_wait(process: SandboxProcess) -> int | None:
    try:
        return await asyncio.wait_for(process.wait(), timeout=EXIT_WAIT_S)
    except TimeoutError:
        return None


def _parse(line: str) -> RunEvent | None:
    if not line.strip():
        return None
    try:
        return run_event_adapter.validate_json(line)
    except ValidationError:
        log.warning("the runner wrote a line that is not an event; ignored")
        return None


async def _send_cancel(process: SandboxProcess) -> None:
    with contextlib.suppress(SandboxError):
        await process.write('{"op": "cancel"}\n')


def _invalid(message: str) -> RunFailed:
    return RunFailed(code=FailureCode.INVALID_REQUEST, message=message)


def _cancelled(usage: Usage) -> RunFailed:
    return RunFailed(code=FailureCode.CANCELLED, message="cancelled", usage=usage)


def _sandbox_failed(message: str, usage: Usage) -> RunFailed:
    return RunFailed(code=FailureCode.SANDBOX_ERROR, message=message, usage=usage, retryable=True)


__all__: Sequence[str] = ["CAPABILITIES", "RUNNER_ARGV", "SandboxSettings", "SandboxedRuntime"]
