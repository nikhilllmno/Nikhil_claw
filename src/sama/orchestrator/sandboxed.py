"""Runs a runtime adapter inside the session sandbox (docs/architecture.md, Section 16.2).

``SandboxedRuntime`` implements ``AgentRuntime``. For each ``run`` or ``resume`` it:

1. gets the session sandbox: it resumes the sandbox of the session, or creates one if the
   session has none or the provider lost it,
2. issues a run token for exactly this run scope,
3. starts the runner (``python -m sama.runtime.runner``) with the token as its only
   credential, and sends the job on stdin,
4. streams the runner events from stdout.

It measures and logs the sandbox start and the time to the first event (roadmap M1.6).

Before a run it makes the idle timeout of the sandbox long enough for the run. After the last
run of the session it sets the timeout back, so an idle sandbox pauses on time.

A worker task does the sandbox work of each invocation. The stream reads its events, so a
cancel ends the stream within ``CANCEL_DEADLINE_S`` even if a provider call hangs.

The runner is a new process for each invocation, so the rules that span invocations are
enforced here: one stream per run id, each approval resumes once, and a cancelled run cannot
resume. These rules are in memory. The run queue (``sama.orchestrator.queue``) enforces the
same rules in Postgres across processes. With a ``SandboxDirectory``, the session-to-sandbox map
is in Postgres too, so a new process finds the sandbox of a session.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from pydantic import ValidationError

from sama.observability.steps import Steps
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
from sama.runtime.fake import FakeScript
from sama.runtime.runner import (
    MODEL_URL_ENV,
    RUN_TOKEN_ENV,
    TIMING_PREFIX,
    WORKDIR_ENV,
    GatePolicy,
    RunnerJob,
)
from sama.sandbox.base import (
    DEFAULT_IDLE_TIMEOUT_S,
    ExecResult,
    Sandbox,
    SandboxError,
    SandboxNotFoundError,
    SandboxOutdatedError,
    SandboxProcess,
    SandboxProvider,
    SandboxSpec,
)
from sama.skills.sync import (
    CHECK_CHANGED,
    CHECK_SAME,
    SYNC_TIMEOUT_S,
    SkillSource,
    checked_start,
    publish,
    wanted_files,
)
from sama.tenancy import RUN_TOKEN_MAX_TTL_S, RunTokenSigner

log: Final = logging.getLogger("sama.orchestrator")

RUNNER_ARGV: Final = ("/opt/sama/.venv/bin/python", "-m", "sama.runtime.runner")
"""The runner in the sama image (``sandbox/Dockerfile``)."""
WARM_RUNNER_ARGV: Final = ("/opt/sama/.venv/bin/python", "-m", "sama.warm")
"""The same runner, started in a warm process when one is ready (``sama.warm``)."""
DEFAULT_RUN_WALL_TIME_S: Final = 60 * 60
TOKEN_MARGIN_S: Final = 10 * 60
"""The run token lives this long after the wall-time limit, for the start and the stop."""
CANCEL_KILL_AFTER_S: Final = 7.0
"""After a cancel, the orchestrator waits this long for the runner to stop, then kills it.
The runner gives the Claude CLI 4 seconds."""
KILL_WAIT_S: Final = 2.0
CANCEL_DEADLINE_S: Final = 9.0
"""The stream ends this long after a cancel at the latest. The contract limit is 10 s."""
EXIT_WAIT_S: Final = 5.0
"""After the terminal event, the orchestrator waits this long for the runner to exit."""
PROVIDER_CALL_S: Final = 10.0
"""The longest wait for a provider call that has no other limit, for example a stdin write."""
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
        manual_compaction=True,
        compaction_archive=True,
    ),
    RuntimeKind.OPENAI_AGENTS_SDK: Capabilities(
        streaming=True,
        native_subagents=False,
        native_compaction=True,
        native_skills=False,
        native_file_checkpoints=False,
        resumable_sessions=True,
        mcp_client=True,
        manual_compaction=True,
        compaction_archive=True,
    ),
    RuntimeKind.FAKE: Capabilities(
        streaming=True,
        native_subagents=False,
        native_compaction=False,
        native_skills=False,
        native_file_checkpoints=False,
        resumable_sessions=True,
        mcp_client=False,
        manual_compaction=True,
        compaction_archive=False,
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
    cancelled_at: float = 0.0

    def cancel(self) -> None:
        if not self.cancelled.is_set():
            self.cancelled_at = time.monotonic()
            self.cancelled.set()

    def time_left(self) -> float:
        """The time that the stream waits for the runner after a cancel. Call it only after a
        cancel. If the runner has no process yet, the stream does not wait: no runner event
        can come, and the worker stops the late runner in the background."""
        if self.process is None:
            return 0.0
        return max(0.0, self.cancelled_at + CANCEL_DEADLINE_S - time.monotonic())


CARRY_DIRS: Final = (".claude",)
CARRY_MAX_BYTES: Final = 64 * 1024 * 1024
"""A larger archive of ``carry_dirs`` does not go to the new sandbox."""
CARRY_FILE: Final = ".sama-carry.tgz"
"""The archive, in the work folder of each sandbox, only for the time of the move."""
_CARRY_PACK: Final = (
    'cd "$HOME" || exit 1; '
    'set -- $(for d in "$@"; do [ -e "$d" ] && echo "$d"; done); '
    '[ "$#" -gt 0 ] || exit 3; '
    'tar -czf "$CARRY" "$@" && stat -c %s "$CARRY"'
)
_CARRY_UNPACK: Final = (
    'mkdir -p "$HOME" && tar -xzf "$CARRY" -C "$HOME"; code=$?; rm -f "$CARRY"; exit $code'
)


@dataclass(frozen=True, slots=True)
class SandboxSettings:
    """``template`` is the provider template or image. ``allow_out`` is the egress
    allowlist of every session sandbox: E2B blocks all other hosts, and an empty list blocks
    all of them. ``open_egress`` turns the block off, for development only. ``model_url`` is
    the model proxy (M1.7). ``gate_url`` is the gate endpoint of the control plane (M1.9):
    with it, the runner asks the control plane for each tool call. ``archive_url`` is the
    archive endpoint (M2.6): with it, the runner gives the transcript to the control plane
    before a compaction. ``session_url`` is the session items endpoint (M3.6): the OpenAI
    runtime keeps its conversation items there."""

    template: str
    model_url: str | None = None
    gate_url: str | None = None
    archive_url: str | None = None
    session_url: str | None = None
    allow_out: tuple[str, ...] = ()
    open_egress: bool = False
    runner_argv: tuple[str, ...] = RUNNER_ARGV
    extra_env: Mapping[str, str] = field(default_factory=dict)
    """More variables for the runner. Never a secret."""
    carry_dirs: tuple[str, ...] = CARRY_DIRS
    """Folders in the home folder of the sandbox user that go to the new sandbox when the
    session sandbox is from another template (Section 16.2). The default is the folder of
    the Claude CLI, which holds the transcripts that a run resumes."""


class SandboxDirectory(Protocol):
    """Stores the sandbox id of each session, so that every process uses the same sandbox."""

    async def get(self, org_id: str, session_id: str) -> str | None: ...

    async def put(self, org_id: str, session_id: str, sandbox_id: str) -> None: ...


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
        directory: SandboxDirectory | None = None,
        skills: SkillSource | None = None,
    ) -> None:
        if kind not in CAPABILITIES:
            raise ValueError(f"the runner does not support runtime {kind}")
        if gate is not None and settings.gate_url is not None:
            raise ValueError("set settings.gate_url or a static gate, not both")
        self._kind = kind
        self._provider = provider
        self._signer = signer
        self._settings = settings
        self._gate = gate or GatePolicy()
        self._directory = directory
        self._skills = skills
        self._sandbox_ids: dict[tuple[str, str], str] = {}
        self._session_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._session_runs: dict[tuple[str, str], int] = {}
        self._active: dict[str, _Active] = {}
        self._paused = _Recent()
        self._used_approvals = _Recent()
        self._cancelled = _Recent()
        self._tasks: set[asyncio.Task[None]] = set()
        self.fake_scripts: list[FakeScript] = []
        """Scripts for the fake runtime in the sandbox. The next run takes all of them."""

    @property
    def kind(self) -> RuntimeKind:
        return self._kind

    def capabilities(self) -> Capabilities:
        return CAPABILITIES[self._kind]

    def for_kind(self, kind: RuntimeKind) -> SandboxedRuntime:
        """The same sandboxes, sessions, and runs for another runtime kind. Both objects share
        their state, so a session that changes its runtime keeps its sandbox, and a cancel or a
        resume finds the run in either object."""
        if kind not in CAPABILITIES:
            raise ValueError(f"the runner does not support runtime {kind}")
        other = copy.copy(self)
        other._kind = kind
        return other

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
        job = RunnerJob(
            op="run",
            run=request,
            gate=self._gate,
            gate_url=self._settings.gate_url,
            archive_url=self._settings.archive_url,
            session_url=self._settings.session_url,
            fake_scripts=scripts,
        )
        async with contextlib.aclosing(self._invoke(job, request.scope, request.budget)) as stream:
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
        # The approval is reserved now, so a second resume is refused. If the job never
        # reaches the runner, ``_invoke`` gives the approval back for a retry.
        self._used_approvals.add(request.approval_id)
        self._paused.discard(run_id)
        job = RunnerJob(
            op="resume",
            resume=request,
            gate=self._gate,
            gate_url=self._settings.gate_url,
            archive_url=self._settings.archive_url,
            session_url=self._settings.session_url,
        )
        # The usage at the pause is in the opaque runtime state. Only the runner reads it, so a
        # failure before the runner starts reports zero usage.
        stream = self._invoke(job, request.scope, request.budget, approval_id=request.approval_id)
        async with contextlib.aclosing(stream):
            async for event in stream:
                yield event

    async def cancel(self, run_id: str) -> None:
        self._cancelled.add(run_id)
        self._paused.discard(run_id)
        active = self._active.get(run_id)
        if active is None or active.cancelled.is_set():
            return
        active.cancel()
        # If the runner has no job yet, the worker sees the flag after the job write.
        if active.process is not None:
            await _halt(active.process, run_id)

    async def session_sandbox(self, org_id: str, session_id: str) -> Sandbox | None:
        """The sandbox of a session, resumed, or None if the session has none."""
        sandbox_id = await self._known_sandbox((org_id, session_id))
        if sandbox_id is None:
            return None
        try:
            return await self._provider.resume(sandbox_id, self._spec(org_id, session_id))
        except SandboxNotFoundError:
            # Also an outdated sandbox. It is not paused here: a run can move its state at
            # this time (``acquire``). The next run kills it; E2B pauses it when it is idle.
            return None

    async def exec_script(self, scope: RunScope, script: str, *, timeout_s: float) -> ExecResult:
        """Runs a job script with ``bash -c`` in the session sandbox (Section 12.1). The script
        gets no run token and no secret: the sandbox environment has none."""
        sandbox = await self.acquire(scope, Budget(max_wall_time_s=timeout_s))
        try:
            return await sandbox.exec(
                ["bash", "-c", script], cwd=sandbox.workdir, timeout_s=timeout_s
            )
        finally:
            await self._end_session_run((scope.org_id, scope.session_id), sandbox)

    async def wait_for_cleanup(self) -> None:
        """Waits for the background work: the invocations that still run after their stream
        ended (for example, after a cancel), and the runner stops."""
        # A done task leaves the set in a later loop step, so wait only for the pending ones.
        while pending := [task for task in self._tasks if not task.done()]:
            await asyncio.wait(pending)

    async def close_sandboxes(self, *, kill: bool = False) -> None:
        """Pauses every session sandbox that this runtime knows, or kills it. For a shutdown
        and for tests."""
        await self.wait_for_cleanup()
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

    def _spec(
        self, org_id: str, session_id: str, idle_timeout_s: int = DEFAULT_IDLE_TIMEOUT_S
    ) -> SandboxSpec:
        return SandboxSpec(
            org_id=org_id,
            session_id=session_id,
            template=self._settings.template,
            idle_timeout_s=idle_timeout_s,
            allow_out=self._settings.allow_out,
            open_egress=self._settings.open_egress,
        )

    async def acquire(self, scope: RunScope, budget: Budget) -> Sandbox:
        """Resumes the session sandbox, or creates one, and makes its idle timeout long enough
        for the run. Each call needs one ``_end_session_run`` later.

        The create or the resume sets the long timeout (``SandboxSpec.idle_timeout_s``), so the
        run needs no separate ``keep_alive`` call: each provider call is a round trip."""
        key = (scope.org_id, scope.session_id)
        async with self._session_locks.setdefault(key, asyncio.Lock()):
            spec = self._spec(*key, idle_timeout_s=int(_wall_time_s(budget) + TOKEN_MARGIN_S))
            sandbox: Sandbox | None = None
            outdated: Sandbox | None = None
            known = await self._known_sandbox(key)
            if known is not None:
                try:
                    sandbox = await self._provider.resume(known, spec)
                except SandboxOutdatedError as error:
                    log.warning(
                        "the session sandbox is from another template; creating a new one"
                        " sandbox=%s",
                        known,
                    )
                    outdated = error.sandbox
                except SandboxNotFoundError:
                    log.warning("the session sandbox is gone; creating a new one sandbox=%s", known)
            if sandbox is None:
                try:
                    sandbox = await self._provider.create(spec)
                    if outdated is not None:
                        await self._carry(outdated, sandbox)
                finally:
                    if outdated is not None:
                        with contextlib.suppress(Exception):
                            await outdated.kill()
                self._sandbox_ids[key] = sandbox.id
                if self._directory is not None:
                    try:
                        await self._directory.put(*key, sandbox.id)
                    except Exception:
                        # This process still knows the sandbox. Another process creates a
                        # new one, so the run goes on.
                        log.exception("could not store the session sandbox=%s", sandbox.id)
            self._session_runs[key] = self._session_runs.get(key, 0) + 1
            return sandbox

    async def _carry(self, old: Sandbox, new: Sandbox) -> None:
        """Moves ``carry_dirs`` from the outdated sandbox to the new one. A failure only logs:
        the run goes on, and a runtime without its saved session starts a new one."""
        dirs = self._settings.carry_dirs
        if not dirs:
            return
        try:
            source = f"{old.workdir}/{CARRY_FILE}"
            packed = await old.exec(
                ["sh", "-c", _CARRY_PACK, "sh", *dirs], env={"CARRY": source}, timeout_s=120
            )
            if packed.exit_code == 3:
                return  # nothing to move
            size = int(packed.stdout.strip() or -1) if packed.exit_code == 0 else -1
            if not 0 < size <= CARRY_MAX_BYTES:
                log.warning(
                    "the state of the old sandbox does not move code=%s size=%s",
                    packed.exit_code,
                    size,
                )
                return
            target = f"{new.workdir}/{CARRY_FILE}"
            data = await old.read_file(source, max_bytes=CARRY_MAX_BYTES)
            await new.write_file(target, data)
            unpacked = await new.exec(
                ["sh", "-c", _CARRY_UNPACK], env={"CARRY": target}, timeout_s=120
            )
            if unpacked.exit_code != 0:
                log.warning(
                    "the state of the old sandbox did not unpack code=%s", unpacked.exit_code
                )
        except Exception:
            log.exception("the state of the old sandbox did not move sandbox=%s", old.id)

    async def _known_sandbox(self, key: tuple[str, str]) -> str | None:
        known = self._sandbox_ids.get(key)
        if known is None and self._directory is not None:
            known = await self._directory.get(*key)
            if known is not None:
                self._sandbox_ids[key] = known
        return known

    async def _end_session_run(self, key: tuple[str, str], sandbox: Sandbox) -> None:
        """After the last run of the session, sets the idle timeout back. It holds the session
        lock, so it cannot shorten the timeout of a run that starts at the same time."""
        async with self._session_locks.setdefault(key, asyncio.Lock()):
            left = self._session_runs.get(key, 0) - 1
            if left > 0:
                self._session_runs[key] = left
                return
            self._session_runs.pop(key, None)
            idle = self._spec(*key).idle_timeout_s
            try:
                await asyncio.wait_for(sandbox.keep_alive(idle), timeout=PROVIDER_CALL_S)
            except (SandboxError, TimeoutError) as error:
                log.warning("could not set the idle timeout sandbox=%s: %r", sandbox.id, error)

    def _spawn(self, work: Awaitable[None]) -> None:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _invoke(
        self,
        job: RunnerJob,
        scope: RunScope,
        budget: Budget,
        *,
        approval_id: str | None = None,
    ) -> AsyncGenerator[RunEvent]:
        """Streams the events of one invocation.

        A worker task does the sandbox work and puts the events in a queue. This generator only
        reads the queue, so it can end the stream at the cancel deadline while the worker still
        waits for a slow provider. The worker then cleans up in the background.
        """
        run_id = scope.run_id
        active = _Active()
        self._active[run_id] = active
        events: asyncio.Queue[RunEvent] = asyncio.Queue()
        self._spawn(self._work(job, scope, budget, active, events, approval_id))
        started = time.monotonic()
        usage = Usage()
        first = True
        ended = False
        try:
            while (event := await _next_event(events, active)) is not None:
                if first:
                    first = False
                    elapsed = time.monotonic() - started
                    log.info("first runner event run=%s after_s=%.3f", run_id, elapsed)
                usage = event.usage
                if not is_terminal(event):
                    yield event
                    continue
                ended = True
                self._release(run_id, active)
                if isinstance(event, ApprovalRequired):
                    if active.cancelled.is_set() or run_id in self._cancelled:
                        event = _cancelled(event.usage)
                    else:
                        self._paused.add(run_id)
                yield event
                return
            if active.process is not None:
                log.warning("the runner did not stop before the cancel deadline run=%s", run_id)
            ended = True
            yield _cancelled(usage)
        finally:
            self._release(run_id, active)
            if not ended and not active.cancelled.is_set():
                # The caller closed the stream early. Stop the runner as for a cancel.
                active.cancel()
                if active.process is not None:
                    self._spawn(_halt(active.process, run_id))

    async def _work(
        self,
        job: RunnerJob,
        scope: RunScope,
        budget: Budget,
        active: _Active,
        events: asyncio.Queue[RunEvent],
        approval_id: str | None,
    ) -> None:
        """Gets the sandbox, starts the runner, sends the job, and puts the runner events in
        ``events``. It always puts one terminal event, and it always cleans up."""
        run_id = scope.run_id
        key = (scope.org_id, scope.session_id)
        usage = Usage()
        sandbox: Sandbox | None = None
        process: SandboxProcess | None = None
        terminal = False
        sent = False  # True when the job write started: the runner can have the job.
        steps = Steps()
        skills = "none"
        # The skill files come from the database while the sandbox starts.
        files = asyncio.create_task(self._skill_files(scope))
        try:
            try:
                sandbox = await self.acquire(scope, budget)
                steps.mark("acquire")
                if active.cancelled.is_set():
                    events.put_nowait(_cancelled(usage))
                    return
                wanted = await files
                steps.mark("files")
                ttl = min(_wall_time_s(budget) + TOKEN_MARGIN_S, RUN_TOKEN_MAX_TTL_S)
                env = {
                    WORKDIR_ENV: sandbox.workdir,
                    **self._settings.extra_env,
                    RUN_TOKEN_ENV: self._signer.issue(scope, ttl_s=ttl, budget=budget),
                }
                if self._settings.model_url:
                    env[MODEL_URL_ENV] = self._settings.model_url
                argv = list(self._settings.runner_argv)
                # One command checks the skills and starts the runner (Section 9.3).
                start = argv if wanted is None else checked_start(sandbox, wanted, argv)
                process = await sandbox.start(start, env=env)
                steps.mark("start")
                if active.cancelled.is_set():
                    events.put_nowait(_cancelled(usage))
                    return
                sent = True
                await _write_job(process, job)
                steps.mark("write")
                if wanted is not None:
                    answer = await _first_line(process)
                    steps.mark("check")
                    skills = "same"
                    if answer == CHECK_CHANGED:
                        skills = "changed"
                        # The command read the job line and did not run it.
                        sent = False
                        if await _bounded_wait(process) is None:
                            await _bounded_kill(process)
                        process = None
                        if active.cancelled.is_set():
                            events.put_nowait(_cancelled(usage))
                            return
                        await _publish(sandbox, wanted, scope)
                        steps.mark("sync")
                        process = await sandbox.start(argv, env=env)
                        if active.cancelled.is_set():
                            events.put_nowait(_cancelled(usage))
                            return
                        sent = True
                        await _write_job(process, job)
                        steps.mark("restart")
                    elif answer != CHECK_SAME:
                        tail = process.stderr_tail()[-200:]
                        raise SandboxError(f"the runner did not start: {tail}")
            except SandboxError as error:
                if active.cancelled.is_set():
                    events.put_nowait(_cancelled(usage))
                    return
                log.warning("the sandbox failed run=%s: %s", run_id, error)
                # Before the job write, the runner surely has no job, so the approved call did
                # not run and a retry is safe. After a failed write it is not sure.
                retryable = not sent or approval_id is None
                if approval_id is not None and not sent:
                    self._used_approvals.discard(approval_id)
                    self._paused.add(run_id)
                message = f"the sandbox failed: {error}"
                events.put_nowait(_sandbox_failed(message, usage, retryable=retryable))
                return
            # Set the process only now, so a cancel line never comes before the job line.
            active.process = process
            if active.cancelled.is_set():
                self._spawn(_halt(process, run_id))
            try:
                async for line in process.lines():
                    event = _parse(line)
                    if event is None:
                        continue
                    usage = event.usage
                    events.put_nowait(event)
                    steps.mark("first_event", once=True)
                    if is_terminal(event):
                        terminal = True
                        steps.mark("terminal")
                        log.info("sandbox steps run=%s skills=%s %s", run_id, skills, steps)
                        # The caller gets the terminal event now, and this task stops the
                        # runner after it. The next runner of the session waits for this one
                        # to exit: it takes the runner lock in the sandbox (Section 16.2).
                        await _stop(process)
                        _log_timing(run_id, process)
                        return
            except SandboxError as error:
                if active.cancelled.is_set():
                    events.put_nowait(_cancelled(usage))
                else:
                    log.warning("the runner stream failed run=%s: %s", run_id, error)
                    events.put_nowait(_sandbox_failed(f"the runner stream failed: {error}", usage))
                return
            if active.cancelled.is_set():
                events.put_nowait(_cancelled(usage))
                return
            code = await _bounded_wait(process)
            log.warning(
                "the runner ended without a terminal event run=%s code=%s stderr=%s",
                run_id,
                code,
                process.stderr_tail()[-2000:],
            )
            message = f"the runner ended without a result (exit code {code})"
            events.put_nowait(_sandbox_failed(message, usage))
        except Exception as error:
            log.exception("the sandbox work failed run=%s", run_id)
            events.put_nowait(_sandbox_failed(f"the sandbox work failed: {error!r}", usage))
        finally:
            files.cancel()
            if process is not None and not terminal:
                await _bounded_kill(process)
            if sandbox is not None:
                await self._end_session_run(key, sandbox)

    async def _skill_files(self, scope: RunScope) -> dict[str, str] | None:
        """The skill files to sync (``wanted_files``), or None: no skill source, or the read
        failed. A failed read is only a warning, as a failed sync (Section 9.3)."""
        if self._skills is None:
            return None
        try:
            async with asyncio.timeout(SYNC_TIMEOUT_S):
                return wanted_files(await self._skills.files(scope))
        except Exception as error:
            log.warning("skill sync failed run=%s: %s", scope.run_id, error)
            return None

    def _release(self, run_id: str, owner: _Active) -> None:
        if self._active.get(run_id) is owner:
            del self._active[run_id]


def _wall_time_s(budget: Budget) -> float:
    return budget.max_wall_time_s or DEFAULT_RUN_WALL_TIME_S


async def _next_event(events: asyncio.Queue[RunEvent], active: _Active) -> RunEvent | None:
    """The next event, or None at the cancel deadline."""
    if not events.empty():
        return events.get_nowait()
    get = asyncio.ensure_future(events.get())
    try:
        if not active.cancelled.is_set():
            flag = asyncio.ensure_future(active.cancelled.wait())
            try:
                await asyncio.wait({get, flag}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                flag.cancel()
        if not get.done() and (left := active.time_left()) > 0:
            await asyncio.wait({get}, timeout=left)
        return get.result() if get.done() else None
    finally:
        get.cancel()


async def _halt(process: SandboxProcess, run_id: str) -> None:
    """Sends a cancel line and waits for the runner to stop, then kills it."""
    try:
        # The runner writes its last event and exits. Its output is buffered, so this does
        # not wait for the caller to read the stream.
        async with asyncio.timeout(CANCEL_KILL_AFTER_S):
            await _send_cancel(process)
            await process.wait()
    except TimeoutError:
        log.warning("the runner did not stop after cancel; killing it run=%s", run_id)
        await _bounded_kill(process)


async def _write_job(process: SandboxProcess, job: RunnerJob) -> None:
    try:
        await asyncio.wait_for(process.write(job.model_dump_json() + "\n"), timeout=PROVIDER_CALL_S)
    except TimeoutError:
        raise SandboxError("the job write timed out") from None


async def _first_line(process: SandboxProcess) -> str | None:
    """The first stdout line of the command, or None if it ended without one."""
    lines = process.lines()
    try:
        async with asyncio.timeout(SYNC_TIMEOUT_S + PROVIDER_CALL_S):
            async for line in lines:
                return line
    except TimeoutError:
        raise SandboxError("the skill check timed out") from None
    finally:
        close = getattr(lines, "aclose", None)
        if close is not None:
            await close()
    return None


async def _publish(sandbox: Sandbox, wanted: Mapping[str, str], scope: RunScope) -> None:
    """Writes the skill files after a changed check. It never raises, except for a cancel: a
    skill that cannot sync must not stop the run."""
    started = time.monotonic()
    try:
        async with asyncio.timeout(SYNC_TIMEOUT_S):
            written = await publish(sandbox, wanted)
    except Exception as error:
        log.warning("skill sync failed run=%s: %s", scope.run_id, error)
        return
    log.info(
        "skill sync run=%s files=%d duration_s=%.3f",
        scope.run_id,
        written,
        time.monotonic() - started,
    )


async def _bounded_wait(process: SandboxProcess) -> int | None:
    try:
        return await asyncio.wait_for(process.wait(), timeout=EXIT_WAIT_S)
    except TimeoutError:
        return None


async def _bounded_kill(process: SandboxProcess) -> None:
    try:
        await asyncio.wait_for(process.kill(), timeout=KILL_WAIT_S)
    except (SandboxError, TimeoutError) as error:
        log.warning("the runner kill did not finish: %r", error)


async def _stop(process: SandboxProcess) -> None:
    """Closes stdin, waits for the exit, and kills the runner if it does not exit."""
    with contextlib.suppress(SandboxError, TimeoutError):
        await asyncio.wait_for(process.close_stdin(), timeout=PROVIDER_CALL_S)
    if await _bounded_wait(process) is None:
        await _bounded_kill(process)


def _log_timing(run_id: str, process: SandboxProcess) -> None:
    """Logs the timing line of the runner (Section 18.1), if its stderr has one."""
    for line in reversed(process.stderr_tail().splitlines()):
        start = line.find(TIMING_PREFIX)
        if start >= 0:
            log.info("%s run=%s", line[start:][:300], run_id)
            return


def _parse(line: str) -> RunEvent | None:
    if not line.strip():
        return None
    try:
        return run_event_adapter.validate_json(line)
    except ValidationError:
        log.warning("the runner wrote a line that is not an event; ignored")
        return None


async def _send_cancel(process: SandboxProcess) -> None:
    with contextlib.suppress(SandboxError, TimeoutError):
        await asyncio.wait_for(process.write('{"op": "cancel"}\n'), timeout=PROVIDER_CALL_S)


def _invalid(message: str) -> RunFailed:
    return RunFailed(code=FailureCode.INVALID_REQUEST, message=message)


def _cancelled(usage: Usage) -> RunFailed:
    return RunFailed(code=FailureCode.CANCELLED, message="cancelled", usage=usage)


def _sandbox_failed(message: str, usage: Usage, *, retryable: bool = True) -> RunFailed:
    return RunFailed(
        code=FailureCode.SANDBOX_ERROR, message=message, usage=usage, retryable=retryable
    )


__all__: Sequence[str] = ["CAPABILITIES", "RUNNER_ARGV", "SandboxSettings", "SandboxedRuntime"]
