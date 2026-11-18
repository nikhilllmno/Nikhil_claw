"""A scripted runtime for tests and local development. It calls no model.

``FakeRuntime`` replays ``FakeScript`` objects in order, one per ``run`` call. With no script
left, it echoes the input. It follows the same contract as the real adapters, so the
orchestrator and the conformance suite can use it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field

from claw.runtime.contract import (
    ApprovalDecision,
    ApprovalRequired,
    Capabilities,
    FailureCode,
    ResumeRequest,
    RunCompleted,
    RunEvent,
    RunFailed,
    RunRequest,
    RuntimeKind,
    TextDelta,
    Usage,
    validate_event_stream,
)


@dataclass(frozen=True, slots=True)
class FakeScript:
    """The events of one scripted run.

    If ``events`` ends with ``ApprovalRequired``, ``on_approve`` and ``on_reject`` are the
    events that ``resume`` streams for each decision. They must end with ``RunCompleted`` or
    ``RunFailed``. ``delay_s`` is the pause before each event, to test streaming and cancel.
    """

    events: Sequence[RunEvent]
    on_approve: Sequence[RunEvent] = ()
    on_reject: Sequence[RunEvent] = ()
    delay_s: float = 0.0

    def __post_init__(self) -> None:
        if self.delay_s < 0:
            raise ValueError("delay_s must not be negative")
        terminal = validate_event_stream(self.events)
        if isinstance(terminal, ApprovalRequired):
            for name, branch in (("on_approve", self.on_approve), ("on_reject", self.on_reject)):
                if isinstance(validate_event_stream(branch), ApprovalRequired):
                    raise ValueError(f"{name} must end with RunCompleted or RunFailed")
        elif self.on_approve or self.on_reject:
            raise ValueError("on_approve and on_reject need events that end with ApprovalRequired")


@dataclass(slots=True)
class _Pending:
    run_id: str
    script: FakeScript


@dataclass(slots=True)
class FakeRuntime:
    """Replays scripts. Records every request in ``requests`` for assertions in tests."""

    scripts: Iterable[FakeScript] = ()
    requests: list[RunRequest | ResumeRequest] = field(default_factory=list)
    _queue: deque[FakeScript] = field(init=False)
    _active: dict[str, asyncio.Event] = field(init=False, default_factory=dict)
    _pending: dict[str, _Pending] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self._queue = deque(self.scripts)

    @property
    def kind(self) -> RuntimeKind:
        return RuntimeKind.FAKE

    def capabilities(self) -> Capabilities:
        return Capabilities(
            streaming=True,
            native_subagents=False,
            native_compaction=False,
            native_skills=False,
            native_file_checkpoints=False,
            resumable_sessions=True,
            mcp_client=False,
        )

    def add_script(self, script: FakeScript) -> None:
        self._queue.append(script)

    async def run(self, request: RunRequest) -> AsyncIterator[RunEvent]:
        self.requests.append(request)
        if request.config.runtime is not RuntimeKind.FAKE:
            yield _invalid(f"config is for runtime {request.config.runtime}, not fake")
            return
        script = self._queue.popleft() if self._queue else _echo_script(request)
        async for event in self._stream(request.scope.run_id, script.events, script.delay_s):
            if isinstance(event, ApprovalRequired):
                self._pending[event.approval_id] = _Pending(request.scope.run_id, script)
            yield event

    async def resume(self, request: ResumeRequest) -> AsyncIterator[RunEvent]:
        self.requests.append(request)
        pending = self._pending.get(request.approval_id)
        if pending is None:
            yield _invalid(f"unknown approval: {request.approval_id}")
            return
        if pending.run_id != request.scope.run_id:
            yield _invalid(f"approval {request.approval_id} belongs to another run")
            return
        del self._pending[request.approval_id]
        script = pending.script
        branch = (
            script.on_approve if request.decision is ApprovalDecision.APPROVE else script.on_reject
        )
        async for event in self._stream(request.scope.run_id, branch, script.delay_s):
            yield event

    async def cancel(self, run_id: str) -> None:
        cancelled = self._active.get(run_id)
        if cancelled is not None:
            cancelled.set()

    async def _stream(
        self, run_id: str, events: Sequence[RunEvent], delay_s: float
    ) -> AsyncIterator[RunEvent]:
        if run_id in self._active:
            yield _invalid(f"run {run_id} is already active")
            return
        cancelled = asyncio.Event()
        self._active[run_id] = cancelled
        usage = Usage()
        try:
            for event in events:
                if delay_s:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(cancelled.wait(), timeout=delay_s)
                if cancelled.is_set():
                    yield RunFailed(code=FailureCode.CANCELLED, message="cancelled", usage=usage)
                    return
                usage = event.usage
                yield event
        finally:
            del self._active[run_id]


def _invalid(message: str) -> RunFailed:
    return RunFailed(code=FailureCode.INVALID_REQUEST, message=message)


def _echo_script(request: RunRequest) -> FakeScript:
    """Echoes the input word by word. The runtime state counts the turns of the session."""
    turns = 0
    if request.runtime_state:
        turns = int(json.loads(request.runtime_state)["turns"])
    reply = f"echo: {request.input}"
    input_tokens = len(request.input.split())
    events: list[RunEvent] = []
    words = reply.split(" ")
    for index, word in enumerate(words, start=1):
        text = word if index == len(words) else word + " "
        events.append(
            TextDelta(text=text, usage=Usage(input_tokens=input_tokens, output_tokens=index))
        )
    state = json.dumps({"turns": turns + 1}).encode()
    final_usage = Usage(input_tokens=input_tokens, output_tokens=len(words))
    events.append(RunCompleted(output=reply, runtime_state=state, usage=final_usage))
    return FakeScript(events=events)
