"""The run worker (docs/architecture.md, Section 4.3).

A worker claims runs from the ``RunQueue`` and streams each run through its runtime adapter.
For each run it:

1. builds the ``RunRequest``, or the ``ResumeRequest`` for a run whose approval is decided,
2. stores the run events in ``run_events`` with increasing ``seq``,
3. extends the lease while the run streams, stops the run when a user cancels it or when it is
   longer than ``Budget.max_wall_time_s``, and stops it when the lease is lost,
4. ends the run: ``RunCompleted`` stores the reply, ``ApprovalRequired`` pauses the run, and
   ``RunFailed`` fails it,
5. calls the run-end hooks (for example ``ClarifyBroker.cancel_run``) when the run ends and is
   not paused.

Before it starts the runtime it marks the run as dispatched, so a reap does not start the run a
second time. When a write shows that the lease is lost, it stops the runtime at once.

From time to time it also times out approvals (``GateService.expire``), queues the paused runs
whose approval is decided, and reaps the runs whose lease expired.

An idle slot waits for a queue notification (``QueueWakeup``) or for ``poll_s``, whichever
comes first. So a new run starts at once, and the poll is the fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final

from sama.gate.service import GateService
from sama.observability.logs import log_context
from sama.observability.steps import Steps
from sama.orchestrator.queue import ClaimedRun, Lease, Resume, RunQueue
from sama.orchestrator.wakeup import QueueWakeup
from sama.prompt import PromptAssembler
from sama.review.apply import ReviewApplier
from sama.runtime.contract import (
    AgentRuntime,
    ApprovalDecision,
    ApprovalRequired,
    Audience,
    Budget,
    CompactRequest,
    FailureCode,
    ResumeRequest,
    RunCompleted,
    RunConfig,
    RunEvent,
    RunFailed,
    RunRequest,
    RunScope,
    RuntimeKind,
    RuntimeRegistry,
    TextDelta,
    Usage,
    is_terminal,
)
from sama.vault.redact import redact

if TYPE_CHECKING:
    from sama.orchestrator.references import ReferenceExpander

log: Final = logging.getLogger("sama.orchestrator")

ConfigBuilder = Callable[[ClaimedRun], Awaitable[RunConfig]]
"""Builds the ``RunConfig`` of a run. The default is ``sama.prompt.PromptAssembler``."""
RunEndHook = Callable[[str, str], None]
"""Called with ``(org_id, run_id)`` when a run ends and is not paused."""

MAX_FAILURE_CHARS: Final = 2_000
FLUSH_EVENTS: Final = 50
RECONFIRM_TIMEOUT_S: Final = 10.0
"""The longest wait for the check of an approved call before a resume. It is shorter than
the default lease, and a slow check rejects the call."""


def default_owner() -> str:
    """A worker id that is unique for each worker object."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@dataclass(slots=True)
class _Watch:
    cancel: bool = False
    timed_out: bool = False
    lost: bool = False


class _Writer:
    """Stores events in batches. A text delta waits for more events, up to ``flush_s``."""

    def __init__(
        self, queue: RunQueue, run: ClaimedRun, owner: str, watch: _Watch, flush_s: float
    ) -> None:
        self._queue = queue
        self._run = run
        self._owner = owner
        self._watch = watch
        self._flush_s = flush_s
        self._seq = run.next_seq
        self._pending: list[tuple[int, str, dict[str, Any]]] = []
        self._last = time.monotonic()
        self._flushing = asyncio.Lock()
        """A tool of the process can add events too (``Worker.emit``), so one flush writes at
        a time and the events keep their order."""

    async def add(self, event: RunEvent) -> None:
        payload = event.model_dump(mode="json", exclude={"runtime_state"})
        self._pending.append((self._seq, event.type, payload))
        self._seq += 1
        if (
            not isinstance(event, TextDelta)
            or len(self._pending) >= FLUSH_EVENTS
            or time.monotonic() - self._last >= self._flush_s
        ):
            await self.flush()

    async def flush(self) -> None:
        async with self._flushing:
            events, self._pending = self._pending, []
            self._last = time.monotonic()
            if (
                events
                and not self._watch.lost
                and not await self._queue.add_events(self._run, self._owner, events)
            ):
                self._watch.lost = True


class Worker:
    """Claims and executes runs. One object can run several runs at the same time."""

    def __init__(
        self,
        queue: RunQueue,
        runtimes: RuntimeRegistry,
        *,
        owner: str | None = None,
        config: ConfigBuilder | None = None,
        gate: GateService | None = None,
        on_run_end: Sequence[RunEndHook] = (),
        reviews: ReviewApplier | None = None,
        references: ReferenceExpander | None = None,
        heartbeat_s: float | None = None,
        poll_s: float = 1.0,
        maintenance_s: float = 30.0,
        flush_s: float = 0.25,
    ) -> None:
        self.owner = owner or default_owner()
        self._queue = queue
        self._runtimes = runtimes
        self._config = config or PromptAssembler(queue.engine)
        self._gate = gate or GateService(queue.engine)
        self._on_run_end = tuple(on_run_end)
        self._reviews = reviews
        self._references = references
        self._heartbeat_s = heartbeat_s or queue.lease.total_seconds() / 3
        self._poll_s = poll_s
        self._maintenance_s = maintenance_s
        self._flush_s = flush_s
        self._wakeup = QueueWakeup(queue.engine)
        self._writers: dict[tuple[str, str], _Writer] = {}
        """The event writers of the runs that stream now, by ``(org_id, run_id)``."""

    # -- loops ------------------------------------------------------------------------------

    async def serve(self, stop: asyncio.Event, *, concurrency: int = 1) -> None:
        """Runs until ``stop`` is set. Then it lets the current runs end and returns."""
        if concurrency < 1:
            raise ValueError("concurrency must be 1 or more")
        async with asyncio.TaskGroup() as group:
            group.create_task(self._wakeup.run(stop))
            group.create_task(self._maintenance_loop(stop))
            for _ in range(concurrency):
                group.create_task(self._slot(stop))

    async def _slot(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            # The ticket comes first, so a run that is queued during the claim wakes the slot.
            ticket = self._wakeup.ticket()
            try:
                worked = await self.run_once()
            except Exception:
                log.exception("the worker could not handle a run owner=%s", self.owner)
                worked = False
            if not worked:
                await ticket.wait(stop, self._poll_s)

    async def _maintenance_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.maintain()
            except Exception:
                log.exception("the queue maintenance failed owner=%s", self.owner)
            await _wait(stop, self._maintenance_s)

    async def maintain(self) -> None:
        """Times out approvals, queues decided runs, reaps runs with an expired lease, sends on
        the ``/steer`` notes of ended runs (Section 4.3), and applies the decided proposals of
        background reviews (Section 9.4)."""
        expired = await self._gate.expire()
        if expired:
            log.info("approvals timed out count=%s", expired)
        if self._reviews is not None:
            try:
                applied = await self._reviews.apply_decided()
            except Exception:
                log.exception("the review proposals could not be applied owner=%s", self.owner)
            else:
                if applied:
                    log.info("review proposals decided count=%s", applied)
        released = await self._queue.release_decided()
        requeued, ended = await self._queue.reap()
        try:
            steered = await self._queue.sweep_steers()
        except Exception:
            log.exception("the steer notes could not be swept owner=%s", self.owner)
        else:
            if steered:
                log.info("steer notes handled after their run ended runs=%s", steered)
        for run_id in released:
            log.info("run queued after its approval run=%s", run_id)
        for ref in requeued:
            log.warning("run queued again after a lost lease run=%s", ref.run_id)
        for ref in ended:
            log.warning("run ended after a lost lease run=%s", ref.run_id)
            self._ended(ref.org_id, ref.run_id)

    async def run_once(self) -> bool:
        """Claims one run and executes it. Returns False if no run was ready."""
        started = time.monotonic()
        run = await self._queue.claim(self.owner)
        if run is None:
            return False
        log.info(
            "run claimed org=%s session=%s run=%s attempt=%s resume=%s claim_s=%.3f",
            run.org_id,
            run.session_id,
            run.run_id,
            run.attempt,
            run.resume is not None,
            time.monotonic() - started,
        )
        await self.execute(run)
        return True

    # -- one run ----------------------------------------------------------------------------

    async def execute(self, run: ClaimedRun) -> None:
        """Streams a claimed run through its runtime and ends it. Each log line of the run
        carries the ids of the run (Section 18.1)."""
        with log_context(
            org_id=run.org_id,
            workspace_id=run.workspace_id,
            session_id=run.session_id,
            run_id=run.run_id,
            runtime=run.runtime,
        ):
            await self._execute(run)

    async def _execute(self, run: ClaimedRun) -> None:
        steps = Steps()
        try:
            runtime = self._runtimes.get(RuntimeKind(run.runtime))
            budget = Budget.model_validate(dict(run.budget))
            if run.compact and not runtime.capabilities().manual_compaction:
                raise ValueError(f"the runtime {run.runtime} cannot compact on request")
            config = await self._config(run)
            steps.mark("config")
            scope = _scope(run)
        except Exception as error:
            log.warning("the run cannot start run=%s: %r", run.run_id, error)
            message = redact(f"the run cannot start: {error}")[:MAX_FAILURE_CHARS]
            await self._fail(run, FailureCode.INVALID_REQUEST, message, Usage())
            return
        # The check comes before the dispatch: the dispatch then checks the lease just before
        # the start, so a slow check cannot start a run that another attempt owns now.
        run = await self._reconfirm(run)
        run = await self._expand(run)
        if not await self._queue.dispatch(run, self.owner, config_fingerprint=config.fingerprint()):
            log.warning("the worker lost the lease before the start run=%s", run.run_id)
            return
        steps.mark("dispatch")
        stream = _stream(runtime, run, scope, config, budget)
        watch = _Watch()
        writer = _Writer(self._queue, run, self.owner, watch, self._flush_s)
        key = (str(run.org_id), str(run.run_id))
        self._writers[key] = writer
        heart = asyncio.create_task(self._watch(run, runtime, budget, watch))
        terminal: RunEvent | None = None
        usage = Usage()
        try:
            async with contextlib.aclosing(stream):
                async for event in stream:
                    steps.mark("first_event", once=True)
                    usage = event.usage
                    await writer.add(event)
                    if watch.lost:
                        # Stop at once. The run belongs to another attempt now.
                        await _cancel(runtime, str(run.run_id))
                        break
                    if is_terminal(event):
                        terminal = event
                        break
            self._writers.pop(key, None)
            await writer.flush()
            steps.mark("stream")
        except Exception:
            log.exception("the run stream failed run=%s", run.run_id)
        finally:
            self._writers.pop(key, None)
            heart.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heart
        if watch.lost:
            log.warning("the worker lost the lease; it writes no result run=%s", run.run_id)
            return
        await self._finish(run, terminal, usage, watch)
        steps.mark("finish")
        # One line per run with the end time of each step (Section 18.1).
        log.info("worker steps run=%s %s", run.run_id, steps)

    async def _expand(self, run: ClaimedRun) -> ClaimedRun:
        """Attaches the content of the ``@`` references of a new chat message (Section 6.3,
        M4.9). Only the input of this run changes: the stored message stays as it was. The
        dispatch after this checks the lease, so a slow read cannot start a lost run."""
        if self._references is None or run.resume is not None or run.compact:
            return run
        if run.kind != "chat":
            return run
        try:
            text = await self._references.expand(run)
        except Exception as error:  # a reference must not fail the run
            log.warning("the references failed run=%s: %r", run.run_id, error)
            return run
        return replace(run, input=text)

    async def _reconfirm(self, run: ClaimedRun) -> ClaimedRun:
        """Checks an approved call again before the resume (Section 5.1). Authority is live,
        so a newer deny rule, a lost membership, or a spent budget turns the approve into a
        reject. A failed or slow check (``RECONFIRM_TIMEOUT_S``) also rejects."""
        resume = run.resume
        if resume is None or resume.decision is not ApprovalDecision.APPROVE:
            return run
        try:
            async with asyncio.timeout(RECONFIRM_TIMEOUT_S):
                verdict = await self._gate.reconfirm(
                    org_id=run.org_id, run_id=run.run_id, call_id=resume.approval_id
                )
            action, reason = verdict.action, verdict.reason
        except Exception as error:
            log.warning("the check before the resume failed run=%s: %r", run.run_id, error)
            action, reason = "deny", "the tool gate failed"
        if action == "allow":
            return run
        log.warning("the approved call is denied now run=%s: %s", run.run_id, reason)
        reject = Resume(
            resume.approval_id,
            ApprovalDecision.REJECT,
            f"the tool gate denies the approved call now: {reason}",
        )
        return replace(run, resume=reject)

    async def _finish(
        self, run: ClaimedRun, terminal: RunEvent | None, usage: Usage, watch: _Watch
    ) -> None:
        if isinstance(terminal, RunCompleted):
            if await self._queue.complete(
                run,
                self.owner,
                output=terminal.output,
                runtime_state=terminal.runtime_state,
                usage=terminal.usage,
            ):
                self._ended(run.org_id, run.run_id)
            return
        if isinstance(terminal, ApprovalRequired):
            status = await self._queue.pause(
                run,
                self.owner,
                approval_id=terminal.approval_id,
                tool_name=terminal.tool_name,
                arguments=terminal.arguments,
                reason=terminal.reason,
                runtime_state=terminal.runtime_state,
                usage=terminal.usage,
            )
            if status == "cancelled":
                self._ended(run.org_id, run.run_id)
            elif status == "paused":
                # The approval can be decided before the pause is stored.
                await self._queue.release_decided(org_id=run.org_id, run_id=run.run_id)
            return
        if isinstance(terminal, RunFailed):
            code, message = terminal.code, terminal.message
            if code is FailureCode.CANCELLED and watch.timed_out and not watch.cancel:
                code, message = FailureCode.TIMEOUT, "the run took longer than its wall-time limit"
            # Redact before the cut: a cut secret no longer matches.
            await self._fail(
                run,
                code,
                redact(message)[:MAX_FAILURE_CHARS],
                terminal.usage,
                runtime_state=terminal.runtime_state,
            )
            return
        await self._fail(run, FailureCode.INTERNAL, "the runtime ended without a result", usage)

    async def _fail(
        self,
        run: ClaimedRun,
        code: FailureCode,
        message: str,
        usage: Usage,
        runtime_state: bytes | None = None,
    ) -> None:
        if await self._queue.fail(
            run, self.owner, code=code, message=message, usage=usage, runtime_state=runtime_state
        ):
            self._ended(run.org_id, run.run_id)

    async def _watch(
        self, run: ClaimedRun, runtime: AgentRuntime, budget: Budget, watch: _Watch
    ) -> None:
        """Extends the lease, and stops the run on a cancel, a timeout, or a lost lease."""
        timers = [self._beat(run, runtime, watch)]
        if budget.max_wall_time_s is not None:
            timers.append(_deadline(runtime, str(run.run_id), budget.max_wall_time_s, watch))
        await asyncio.gather(*timers)

    async def _beat(self, run: ClaimedRun, runtime: AgentRuntime, watch: _Watch) -> None:
        run_id = str(run.run_id)
        while True:
            await asyncio.sleep(self._heartbeat_s)
            try:
                lease = await self._queue.heartbeat(run, self.owner)
            except Exception:
                log.exception("the heartbeat failed run=%s", run_id)
                continue
            if lease is Lease.LOST:
                watch.lost = True
                await _cancel(runtime, run_id)
                return
            if lease is Lease.CANCEL and not watch.cancel:
                watch.cancel = True
                log.info("cancel requested run=%s", run_id)
                await _cancel(runtime, run_id)

    async def emit(self, org_id: str, run_id: str, event: RunEvent) -> bool:
        """Adds ``event`` to the stream of a run that streams in this worker now, for example
        the progress of ``browser_task`` (Section 14.1, M5.6). The event gets the next ``seq``
        and is stored with the events of the runtime. Returns False when the run does not
        stream here: it ended, or another process runs it."""
        writer = self._writers.get((org_id, run_id))
        if writer is None:
            return False
        await writer.add(event)
        return True

    def _ended(self, org_id: uuid.UUID, run_id: uuid.UUID) -> None:
        for hook in self._on_run_end:
            try:
                hook(str(org_id), str(run_id))
            except Exception:
                log.exception("a run-end hook failed run=%s", run_id)


async def _deadline(runtime: AgentRuntime, run_id: str, seconds: float, watch: _Watch) -> None:
    await asyncio.sleep(seconds)
    watch.timed_out = True
    log.info("the run reached its wall-time limit run=%s", run_id)
    await _cancel(runtime, run_id)


def _scope(run: ClaimedRun) -> RunScope:
    audience = Audience(run.audience)
    owner = run.owner_user_id if audience is Audience.PRIVATE else None
    return RunScope(
        org_id=str(run.org_id),
        workspace_id=str(run.workspace_id),
        session_id=str(run.session_id),
        run_id=str(run.run_id),
        audience=audience,
        user_id=str(owner) if owner is not None else None,
    )


async def _stream(
    runtime: AgentRuntime, run: ClaimedRun, scope: RunScope, config: RunConfig, budget: Budget
) -> AsyncGenerator[RunEvent]:
    if run.resume is not None:
        events = runtime.resume(
            ResumeRequest(
                scope=scope,
                config=config,
                approval_id=run.resume.approval_id,
                decision=run.resume.decision,
                reason=run.resume.reason,
                budget=budget,
                runtime_state=run.runtime_state,
                parallel=run.side,
            )
        )
    else:
        events = runtime.run(
            RunRequest(
                scope=scope,
                # A compact run has no message: its input is the focus of the summary.
                input="" if run.compact else run.input,
                config=config,
                budget=budget,
                runtime_state=run.runtime_state,
                compact=CompactRequest(focus=run.input) if run.compact else None,
                parallel=run.side,
            )
        )
    try:
        async for event in events:
            yield event
    finally:
        # Closing the adapter stream early stops the run, as the contract says.
        close = getattr(events, "aclose", None)
        if close is not None:
            await close()


async def _cancel(runtime: AgentRuntime, run_id: str) -> None:
    try:
        await runtime.cancel(run_id)
    except Exception:
        log.exception("the runtime cancel failed run=%s", run_id)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
