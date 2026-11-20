"""The Claude Agent SDK runtime adapter (docs/architecture.md, Section 5.3).

``ClaudeRuntime`` maps a ``RunRequest`` to ``ClaudeAgentOptions`` and the SDK messages to
``RunEvent``s. The SDK drives the bundled Claude Code CLI as a subprocess. Install the SDK
with ``uv sync --group runtime-claude``.

Approvals use a ``PreToolUse`` hook. The hook asks the sama ``ToolGate``. ``ask`` becomes the
hook decision ``defer``: the CLI ends the turn with ``stop_reason == "tool_deferred"``, and the
adapter yields ``ApprovalRequired``. The live behaviour of the CLI, checked with
``claude-agent-sdk==0.2.184``:

- Approve: resume the SDK session with no prompt. The CLI checks the deferred call again,
  through ``can_use_tool`` or (for some read-only calls) the hook. Both allow only the
  approved tool use id. Then the CLI runs the call and the model continues.
- Reject: a deny on resume is not reliable, because some calls skip ``can_use_tool`` and the
  model can try the call again. So the adapter first resumes with the tool in
  ``disallowed_tools`` and ``CLAUDE_CODE_DROP_UNAVAILABLE_DEFERRED_TOOL=1``. The CLI then
  drops the call (``stop_reason == "tool_deferred_unavailable"``). Then the adapter resumes
  again with all tools and tells the model that the user rejected the call.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, Literal, Protocol, cast

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    HookContext,
    HookInput,
    HookMatcher,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import (
    CanUseTool,
    HookCallback,
    McpHttpServerConfig,
    PermissionResultAllow,
    PermissionResultDeny,
    PreToolUseHookSpecificOutput,
    SyncHookJSONOutput,
    SystemPromptPreset,
    TaskNotificationMessage,
    TaskStartedMessage,
    ToolPermissionContext,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sama.runtime.archive import Archiver, TranscriptArchive, bound_for_wire
from sama.runtime.claude_transcript import read_transcript
from sama.runtime.contract import (
    ApprovalDecision,
    ApprovalRequired,
    Budget,
    Capabilities,
    Compacted,
    FailureCode,
    ResumeRequest,
    RunCompleted,
    RunConfig,
    RunEvent,
    RunFailed,
    RunRequest,
    RunScope,
    RuntimeKind,
    SubagentDefinition,
    SubagentEvent,
    TerminalEvent,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    Usage,
    bound_subagent_text,
    is_terminal,
)
from sama.runtime.gating import AllowAllGate as AllowAllGate
from sama.runtime.gating import BoundedSet
from sama.runtime.gating import GateDecision as GateDecision
from sama.runtime.gating import ToolGate as ToolGate

log: Final = logging.getLogger("sama.runtime.claude")

DROP_DEFERRED_ENV: Final = "CLAUDE_CODE_DROP_UNAVAILABLE_DEFERRED_TOOL"
"""CLI switch: drop a deferred tool call when the tool is not available on resume."""

BACKGROUND_TASKS_ENV: Final = "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"
"""CLI switch: no background tasks. Without it, the CLI starts a sub-agent in the background,
and the run can end before the sub-agent ends. With a sub-agent in the config, a sub-agent
runs in the foreground, and its calls stay inside the run (the budget, the gate, the events)."""

TOOL_SEARCH_ENV: Final = "ENABLE_TOOL_SEARCH"
"""CLI switch for tool search. With tool search on, the CLI defers MCP tools: the model must
call the native ``ToolSearch`` tool to load a tool schema first. ``ToolSearch`` is in no sama
toolset, so the gate denies it and the model cannot call a sama MCP tool. The adapter sets the
switch to ``false``, so the CLI loads all MCP tools at the start. ``ClaudeSettings.env`` can
override it."""

_ABORTED: Final = frozenset({"aborted_streaming", "aborted_tools"})
_BUDGET_SUBTYPES: Final = frozenset({"error_max_turns", "error_max_budget_usd"})
_PROXY_BUDGET_STATUS: Final = 402
"""The model proxy refuses a call over a budget with HTTP 402 (Section 16.6)."""
_RETRYABLE_STATUS: Final = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
_KNOWN_SUBTYPES: Final = frozenset(
    {"error_during_execution", "error_max_structured_output_retries"}
)
_SUBAGENT_TOOLS: Final = frozenset({"Task", "Agent"})
_NATIVE_TOOLS: Final = frozenset(
    {"Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep", "WebFetch"}
    | {"TodoWrite"}
)
"""The sama tools that the CLI has built in. Every other sama tool is a tool of the sama MCP
server, and the CLI names it ``mcp__sama__<tool>``."""
_SAMA_MCP_PREFIX: Final = "mcp__sama__"
_PREVIEW_CHARS: Final = 2_000


# ---------------------------------------------------------------------------------------------
# Seams: the tool gate and the SDK client
# ---------------------------------------------------------------------------------------------


class ClaudeClient(Protocol):
    """The part of ``ClaudeSDKClient`` that the adapter uses. Tests give a fake."""

    async def connect(self, prompt: str | None = None) -> None: ...

    async def query(self, prompt: str) -> None: ...

    def receive_messages(self) -> AsyncIterator[Any]: ...

    async def interrupt(self) -> None: ...

    async def disconnect(self) -> None: ...


ClientFactory = Callable[[ClaudeAgentOptions], ClaudeClient]


def _sdk_client(options: ClaudeAgentOptions) -> ClaudeClient:
    return cast(ClaudeClient, ClaudeSDKClient(options))


@dataclass(frozen=True, slots=True)
class ClaudeSettings:
    """Process settings of the adapter. They are not part of the frozen ``RunConfig``.

    ``env`` goes to the CLI process. The model proxy sets ``ANTHROPIC_BASE_URL`` and the run
    token as ``ANTHROPIC_API_KEY`` here (M1.7). ``cancel_grace_s`` is how long ``cancel``
    waits for the CLI to stop by itself before the adapter stops the stream. Keep it well
    under 10 seconds.
    """

    cwd: str | None = None
    cli_path: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    claude_code_preset: bool = True
    cancel_grace_s: float = 5.0
    session_exists: Callable[[str], bool] | None = None
    """Checks that the CLI can resume an SDK session (its transcript is there). The runner sets
    it. ``None`` skips the check. A run whose session is gone starts a new SDK session, and an
    approval resume of it fails (Section 16.2)."""

    def __post_init__(self) -> None:
        if not 0 < self.cancel_grace_s <= 8:
            raise ValueError("cancel_grace_s must be more than 0 and at most 8 seconds")


# ---------------------------------------------------------------------------------------------
# Runtime state: the opaque blob that the orchestrator stores with the run
# ---------------------------------------------------------------------------------------------


class _PendingCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_id: str
    tool_name: str


class _State(BaseModel):
    """The JSON in ``runtime_state``. ``sdk_cost_usd`` is the last cost that the CLI reported
    for the SDK session. The CLI reports cost for the whole SDK session, also after a resume,
    so the adapter adds only the difference to the cost of the run. ``turns`` and
    ``elapsed_s`` are what the run used before it paused. The CLI counts its limits per
    process, so a resume gets only the rest of the budget."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    session_id: str
    run_id: str
    usage: Usage = Usage()
    sdk_cost_usd: Decimal = Decimal(0)
    sdk_tokens: Usage | None = None
    """The tokens that ``model_usage`` of the last result counted for the SDK session, over all
    models. A local command, such as ``/compact``, reports no tokens in ``usage``, but it adds
    to ``model_usage``. The adapter counts the difference. ``None`` means that an older state
    did not save it, so the difference is not known."""
    base_session_id: str | None = None
    """In the state of a paused run: the SDK session of the last completed run, which the run
    continued. If the run fails later, the next run goes on with that session, and only the
    count of the cost and the tokens moves on (``_Translator.failure_state``)."""
    turns: int = Field(default=0, ge=0)
    elapsed_s: float = Field(default=0.0, ge=0)
    pending: _PendingCall | None = None

    def dump(self) -> bytes:
        return self.model_dump_json().encode()

    @classmethod
    def load(cls, raw: bytes | None) -> _State | None:
        if raw is None:
            return None
        return cls.model_validate_json(raw)


# ---------------------------------------------------------------------------------------------
# Phases: one CLI process each
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Phase:
    """One connection to the CLI. ``prompt`` None continues the session as it is.

    ``drop_tool`` makes the phase drop the pending deferred call of that tool and stop at the
    first result. ``approved_call`` is the tool use id that a human approved.
    """

    prompt: str | None
    drop_tool: str | None = None
    approved_call: str | None = None


def _compact_prompt(focus: str) -> str:
    """The prompt that makes the CLI compact the session (``/compact``). The focus is the
    custom instruction of the summary."""
    focus = focus.strip()
    return f"/compact {focus}" if focus else "/compact"


def _reject_prompt(tool_name: str, reason: str | None) -> str:
    text = f"The user rejected the {tool_name} tool call. Do not run it again."
    if reason:
        text += f" Reason from the user: {reason}"
    return text + " Continue without it."


# ---------------------------------------------------------------------------------------------
# Message translation
# ---------------------------------------------------------------------------------------------


class _UnexpectedResult(Exception):
    """The CLI ended a phase in a way that the adapter does not expect."""


@dataclass(slots=True)
class _Translator:
    """Turns SDK messages into ``RunEvent``s and keeps the running usage of the run."""

    run_id: str
    usage: Usage
    sdk_cost_usd: Decimal
    sdk_tokens: Usage | None = None
    """The cumulative tokens of ``model_usage`` at the last result. ``None``: not known."""
    base_session_id: str | None = None
    """The SDK session of the last completed run, which this run continues. ``None``: there is
    none (the first run, or a run that started in a state that was paused)."""
    compact: bool = False
    """The run is a ``/compact`` run. It needs a compaction boundary to be complete."""
    compacted: bool = False
    session_id: str | None = None
    turns: int = 0
    elapsed_before_s: float = 0.0
    """Wall time that the run used in earlier streams (before a pause)."""
    started_at: float | None = None
    ask_reasons: dict[str, str] = field(default_factory=dict)
    allowed_calls: set[str] = field(default_factory=set)
    """Tool use ids that the gate or a human allowed. ``can_use_tool`` allows only these."""
    _subagents: dict[str, str] = field(default_factory=dict)
    _streamed_text: bool = False
    _text: list[str] = field(default_factory=list)

    def feed(self, message: Any) -> list[RunEvent]:
        if isinstance(message, StreamEvent):
            return self._stream_event(message)
        if isinstance(message, AssistantMessage):
            return self._assistant(message)
        if isinstance(message, UserMessage):
            return self._user(message)
        if isinstance(message, SystemMessage):
            return self._system(message)
        return []

    def _stream_event(self, message: StreamEvent) -> list[RunEvent]:
        if message.parent_tool_use_id is not None:
            return []
        event = message.event
        delta = event.get("delta") if event.get("type") == "content_block_delta" else None
        if isinstance(delta, dict) and delta.get("type") == "text_delta":
            text = str(delta.get("text", ""))
            if text:
                self._streamed_text = True
                self._text.append(text)
                return [TextDelta(text=text, usage=self.usage)]
        return []

    def _assistant(self, message: AssistantMessage) -> list[RunEvent]:
        parent = message.parent_tool_use_id
        events: list[RunEvent] = []
        if parent is not None:
            name = self._subagents.get(parent, "subagent")
            for block in message.content:
                if isinstance(block, TextBlock) and block.text:
                    text = bound_subagent_text(block.text)
                elif isinstance(block, ToolUseBlock):
                    text = bound_subagent_text(f"calls {block.name}")
                else:
                    continue
                events.append(
                    SubagentEvent(subagent=name, text=text, call_id=parent, usage=self.usage)
                )
            return events
        streamed = self._streamed_text
        self._streamed_text = False
        for block in message.content:
            if isinstance(block, TextBlock) and block.text and not streamed:
                self._text.append(block.text)
                events.append(TextDelta(text=block.text, usage=self.usage))
            elif isinstance(block, ToolUseBlock):
                if block.name in _SUBAGENT_TOOLS:
                    kind = block.input.get("subagent_type") or block.input.get("description")
                    self._subagents[block.id] = str(kind or block.name)
                events.append(
                    ToolCallStarted(
                        call_id=block.id, name=block.name, arguments=block.input, usage=self.usage
                    )
                )
        return events

    def _user(self, message: UserMessage) -> list[RunEvent]:
        if message.parent_tool_use_id is not None or isinstance(message.content, str):
            return []
        return [
            ToolCallFinished(
                call_id=block.tool_use_id,
                is_error=bool(block.is_error),
                output_preview=_preview(block.content),
                usage=self.usage,
            )
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ]

    def _system(self, message: SystemMessage) -> list[RunEvent]:
        if isinstance(message, TaskStartedMessage | TaskNotificationMessage):
            return self._subagent_task(message)
        if message.subtype != "compact_boundary":
            return []
        metadata = message.data.get("compact_metadata") or {}
        before = _non_negative_int(metadata.get("pre_tokens"))
        after = _non_negative_int(metadata.get("post_tokens"))
        trigger: Literal["auto", "manual"] = (
            "manual" if metadata.get("trigger") == "manual" else "auto"
        )
        self.compacted = True
        return [
            Compacted(tokens_before=before, tokens_after=after, trigger=trigger, usage=self.usage)
        ]

    def _subagent_task(
        self, message: TaskStartedMessage | TaskNotificationMessage
    ) -> list[RunEvent]:
        """The start and the end of a sub-agent. The CLI reports the tokens of the sub-agent
        only here, because the usage of the stream is the usage of the run."""
        call_id = message.tool_use_id
        if call_id is None or call_id not in self._subagents:
            return []  # Not a sub-agent of the Agent tool, for example a background command.
        name = self._subagents[call_id]
        if isinstance(message, TaskStartedMessage):
            text = bound_subagent_text(f"started: {message.description}")
            return [SubagentEvent(subagent=name, text=text, call_id=call_id, usage=self.usage)]
        tokens = message.usage["total_tokens"] if message.usage else None
        text = bound_subagent_text(f"{message.status}: {message.summary}")
        return [
            SubagentEvent(
                subagent=name,
                text=text,
                call_id=call_id,
                done=True,
                tokens=None if tokens is None else _non_negative_int(tokens),
                usage=self.usage,
            )
        ]

    def add_result_usage(self, result: ResultMessage) -> None:
        raw = result.usage or {}
        tokens = Usage(
            input_tokens=_non_negative_int(raw.get("input_tokens")),
            output_tokens=_non_negative_int(raw.get("output_tokens")),
            cache_read_tokens=_non_negative_int(raw.get("cache_read_input_tokens")),
            cache_write_tokens=_non_negative_int(raw.get("cache_creation_input_tokens")),
        )
        cumulative = _model_tokens(result.model_usage)
        if cumulative is not None:
            if tokens.total_tokens == 0 and self.sdk_tokens is not None:
                # The CLI reports no tokens in ``usage`` for ``/compact`` (a local command that
                # calls the model), but ``model_usage`` and the cost count them. A normal turn
                # reports its tokens in ``usage``, so this never counts a turn two times.
                tokens = _token_difference(cumulative, self.sdk_tokens)
            self.sdk_tokens = cumulative
        cost = Decimal(0)
        if result.total_cost_usd is not None and result.total_cost_usd >= 0:
            reported = Decimal(str(result.total_cost_usd))
            # The CLI reports the cost of the whole SDK session. A lower value means that the
            # count started again, so the full value is new cost.
            cost = reported - self.sdk_cost_usd if reported >= self.sdk_cost_usd else reported
            self.sdk_cost_usd = reported
        self.usage = self.usage + tokens.model_copy(update={"cost_usd": cost})
        self.turns += _non_negative_int(result.num_turns)
        self.session_id = result.session_id or self.session_id

    def elapsed_s(self) -> float:
        if self.started_at is None:
            return self.elapsed_before_s
        return self.elapsed_before_s + max(0.0, asyncio.get_running_loop().time() - self.started_at)

    def state(self, pending: _PendingCall | None = None) -> bytes | None:
        if self.session_id is None:
            return None
        return _State(
            session_id=self.session_id,
            run_id=self.run_id,
            usage=self.usage,
            sdk_cost_usd=self.sdk_cost_usd,
            sdk_tokens=self.sdk_tokens,
            # Only a paused run needs it: a completed run is the base of the next run.
            base_session_id=self.base_session_id if pending is not None else None,
            turns=self.turns,
            elapsed_s=self.elapsed_s(),
            pending=pending,
        ).dump()

    def failure_state(self) -> bytes | None:
        """The state that a failed run leaves for the next run: the state of the last completed
        run, with the count of the cost and the tokens that this run moved on.

        The paid work of a failed run is in its own usage already. The CLI counts it in the
        cumulative values of the SDK session too. If the next run started again from the
        count of the last completed run, it would count that work a second time. The
        conversation does not change: ``session_id`` is the session of the last completed run,
        so the next run never resumes the conversation of the failed run. The state has no
        state if the run did not continue that session: its count is not for that session.
        """
        if self.base_session_id is None or self.session_id != self.base_session_id:
            return None
        return _State(
            session_id=self.base_session_id,
            run_id=self.run_id,
            sdk_cost_usd=self.sdk_cost_usd,
            sdk_tokens=self.sdk_tokens,
        ).dump()

    def terminal(self, result: ResultMessage, cancelled: bool) -> TerminalEvent:
        """Maps the result of a prompt phase to the terminal event of the run."""
        usage = self.usage
        if cancelled or result.terminal_reason in _ABORTED:
            return RunFailed(code=FailureCode.CANCELLED, message="cancelled", usage=usage)
        if result.stop_reason == "tool_deferred":
            deferred = result.deferred_tool_use
            if deferred is None:
                raise _UnexpectedResult("the CLI deferred a tool call but did not name it")
            pending = _PendingCall(approval_id=deferred.id, tool_name=deferred.name)
            return ApprovalRequired(
                approval_id=deferred.id,
                call_id=deferred.id,
                tool_name=deferred.name,
                arguments=deferred.input,
                reason=self.ask_reasons.get(deferred.id, ""),
                runtime_state=self.state(pending),
                usage=usage,
            )
        if result.subtype in _BUDGET_SUBTYPES:
            limit = "max_turns" if result.subtype == "error_max_turns" else "max_cost_usd"
            return RunFailed(
                code=FailureCode.BUDGET_EXCEEDED, message=f"the run reached {limit}", usage=usage
            )
        if result.is_error and result.api_error_status == _PROXY_BUDGET_STATUS:
            return RunFailed(
                code=FailureCode.BUDGET_EXCEEDED,
                message="the model proxy refused the call: a budget is used up",
                usage=usage,
            )
        if result.is_error or result.subtype != "success":
            status = result.api_error_status
            return RunFailed(
                code=FailureCode.MODEL_ERROR,
                message=_error_text(result),
                retryable=status in _RETRYABLE_STATUS,
                usage=usage,
            )
        if self.compact and not self.compacted:
            # The CLI can answer ``/compact`` with a normal result and an error text, for
            # example when the summary call fails with HTTP 400. No boundary means that the
            # context is not smaller, so the run did not do what it was asked.
            return RunFailed(
                code=FailureCode.MODEL_ERROR,
                message="the compaction did not finish: the runtime made no compaction boundary",
                usage=usage,
            )
        output = result.result if result.result is not None else "".join(self._text)
        return RunCompleted(output=output, runtime_state=self.state(), usage=usage)


def _non_negative_int(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _model_tokens(model_usage: Mapping[str, Any] | None) -> Usage | None:
    """The tokens of ``ResultMessage.model_usage``, summed over the models. ``None`` if the
    result has none."""
    if not isinstance(model_usage, Mapping) or not model_usage:
        return None
    total = Usage()
    for entry in model_usage.values():
        if not isinstance(entry, Mapping):
            continue
        total = total + Usage(
            input_tokens=_non_negative_int(entry.get("inputTokens")),
            output_tokens=_non_negative_int(entry.get("outputTokens")),
            cache_read_tokens=_non_negative_int(entry.get("cacheReadInputTokens")),
            cache_write_tokens=_non_negative_int(entry.get("cacheCreationInputTokens")),
        )
    return total


def _token_difference(now: Usage, before: Usage) -> Usage:
    """The tokens that ``now`` has more than ``before``. A part that is lower means that the
    count started again, so the whole part is new."""

    def part(new: int, old: int) -> int:
        return new - old if new >= old else new

    return Usage(
        input_tokens=part(now.input_tokens, before.input_tokens),
        output_tokens=part(now.output_tokens, before.output_tokens),
        cache_read_tokens=part(now.cache_read_tokens, before.cache_read_tokens),
        cache_write_tokens=part(now.cache_write_tokens, before.cache_write_tokens),
    )


def _preview(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        text = content
    else:
        parts = [
            str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in content
        ]
        text = "\n".join(p for p in parts if p)
    return text[:_PREVIEW_CHARS]


def _error_text(result: ResultMessage) -> str:
    """A fixed public message. The CLI error strings can hold tool data, so they stay out."""
    status = result.api_error_status
    detail = f", HTTP status {status}" if isinstance(status, int) else ""
    subtype = result.subtype if result.subtype in _KNOWN_SUBTYPES else "error"
    return f"the model call failed ({subtype}{detail})"


# ---------------------------------------------------------------------------------------------
# The runtime
# ---------------------------------------------------------------------------------------------


class _Done:
    """Queue marker: the pump stopped."""


@dataclass(slots=True)
class _ActiveRun:
    """One active stream. ``send`` queues an event and drops events after the terminal one."""

    queue: asyncio.Queue[RunEvent | _Done] = field(default_factory=asyncio.Queue)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    client: ClaudeClient | None = None
    pump: asyncio.Task[None] | None = None
    translator: _Translator | None = None
    sent_terminal: bool = False

    def usage(self) -> Usage:
        return self.translator.usage if self.translator is not None else Usage()

    def send(self, event: RunEvent) -> None:
        if self.sent_terminal:
            return
        self.sent_terminal = is_terminal(event)
        self.queue.put_nowait(event)


class ClaudeRuntime:
    """The default runtime. See the module docstring for the approval flow.

    Rules of the contract (architecture Section 5.1) in this process:

    - One stream at a time per run id. The run id is freed before the terminal event.
    - An approval resumes once, and only for its run. The approval id and the run id must
      match the ``runtime_state`` of ``ApprovalRequired``.
    - ``cancel`` interrupts the CLI. If the CLI does not stop in ``cancel_grace_s``, the
      adapter stops the stream. The stream ends with ``RunFailed(cancelled)``. ``cancel`` also
      makes the approvals of a paused run invalid.

    The orchestrator enforces the same rules across processes (M1.10).
    """

    def __init__(
        self,
        *,
        gate: ToolGate | None = None,
        settings: ClaudeSettings | None = None,
        client_factory: ClientFactory = _sdk_client,
        archiver: Archiver | None = None,
    ) -> None:
        self._gate = gate or AllowAllGate()
        self._archiver = archiver
        self._settings = settings or ClaudeSettings()
        self._client_factory = client_factory
        self._active: dict[str, _ActiveRun] = {}
        self._paused = BoundedSet()
        self._used_approvals = BoundedSet()
        self._cancelled_runs = BoundedSet()
        self._pumps: set[asyncio.Task[None]] = set()
        self._cleanups: set[asyncio.Task[None]] = set()
        self._stopping: dict[str, asyncio.Task[None]] = {}
        """The CLI stop of each SDK session, while it runs. The next run of the session waits
        for it, because ``RunCompleted`` goes out before the stop ends."""

    @property
    def kind(self) -> RuntimeKind:
        return RuntimeKind.CLAUDE_AGENT_SDK

    def capabilities(self) -> Capabilities:
        return Capabilities(
            streaming=True,
            native_subagents=True,
            native_compaction=True,
            native_skills=True,
            native_file_checkpoints=True,
            resumable_sessions=True,
            mcp_client=True,
            manual_compaction=True,
            compaction_archive=True,
            mid_run_steer=True,
        )

    # -- the protocol ----------------------------------------------------------------------

    def _session_exists(self, session_id: str) -> bool:
        check = self._settings.session_exists
        return check is None or check(session_id)

    async def _wait_stopped(self, session_id: str, run_id: str) -> bool:
        """Waits until the CLI of the last run of the session has stopped. Returns ``False``
        if ``cancel(run_id)`` came during the wait.

        The CLI writes the transcript while it stops. So a run that comes after an early
        ``RunCompleted`` must not look for the transcript or start a CLI before that. The
        callers do all their checks after this wait, with no ``await`` between the checks and
        the registration of the run, so two calls cannot both pass the same check.
        """
        task = self._stopping.get(session_id)
        if task is None:
            return True
        cancelled_before = run_id in self._cancelled_runs
        # asyncio.wait does not cancel the stop when this run is cancelled.
        await asyncio.wait({task})
        return cancelled_before or run_id not in self._cancelled_runs

    async def run(self, request: RunRequest) -> AsyncIterator[RunEvent]:
        run_id = request.scope.run_id
        problem = self._check_config(request.config)
        try:
            previous = _State.load(request.runtime_state)
        except ValidationError:
            problem = problem or "runtime_state is not valid for the claude runtime"
            previous = None
        stopped = (
            previous is None
            or problem is not None
            or await self._wait_stopped(previous.session_id, run_id)
        )
        if not stopped:
            yield _cancelled(Usage())
            return
        # No await from here to the registration of the run in ``_stream``.
        if problem is None and (run_id in self._active or run_id in self._paused):
            problem = f"run {run_id} is already active"
        if problem is not None:
            yield _invalid(problem)
            return
        if previous is not None and not self._session_exists(previous.session_id):
            # The sandbox is new (lost, or from another template), so the CLI has no
            # transcript of this session. Start a new SDK session; its counts start at zero.
            log.warning("the sdk session is gone; starting a new one run=%s", run_id)
            previous = None
        if request.compact is not None and previous is None:
            yield _invalid("there is nothing to compact")
            return
        phases: list[_Phase] = []
        if previous is not None and previous.pending is not None:
            # A new message after an unanswered approval. Drop the deferred call first, or
            # the CLI runs it on resume.
            phases.append(_Phase(prompt=None, drop_tool=previous.pending.tool_name))
        if request.compact is not None:
            phases.append(_Phase(prompt=_compact_prompt(request.compact.focus)))
        else:
            phases.append(_Phase(prompt=request.input))
        translator = _Translator(
            run_id=run_id,
            usage=Usage(),
            sdk_cost_usd=previous.sdk_cost_usd if previous else Decimal(0),
            # A new SDK session starts at zero. An older state has no count: not known.
            sdk_tokens=previous.sdk_tokens if previous else Usage(),
            base_session_id=previous.session_id if previous else None,
            compact=request.compact is not None,
            session_id=previous.session_id if previous else None,
        )
        stream = self._stream(request.scope, request.config, request.budget, phases, translator)
        async with contextlib.aclosing(stream):
            async for event in stream:
                yield event

    async def resume(self, request: ResumeRequest) -> AsyncIterator[RunEvent]:
        run_id = request.scope.run_id
        problem = self._check_config(request.config)
        state: _State | None = None
        if problem is None:
            try:
                state = _State.load(request.runtime_state)
            except ValidationError:
                problem = "runtime_state is not valid for the claude runtime"
        if (
            problem is None
            and state is not None
            and not await self._wait_stopped(state.session_id, run_id)
        ):
            yield _cancelled(state.usage)
            return
        # No await from here to the registration of the run in ``_stream``: the checks and the
        # use of the approval are one step.
        if problem is None:
            problem = self._check_resume(request, state)
        if problem is None and state is not None and not self._session_exists(state.session_id):
            problem = "the sdk session of this run is gone, so the approval cannot resume it"
        if problem is not None or state is None or state.pending is None:
            yield _invalid(problem or "runtime_state has no pending approval")
            return
        self._used_approvals.add(request.approval_id)
        self._paused.discard(run_id)
        tool_name = state.pending.tool_name
        if request.decision is ApprovalDecision.APPROVE:
            phases = [_Phase(prompt=None, approved_call=request.approval_id)]
        else:
            phases = [
                _Phase(prompt=None, drop_tool=tool_name),
                _Phase(prompt=_reject_prompt(tool_name, request.reason)),
            ]
        translator = _Translator(
            run_id=run_id,
            usage=state.usage,
            sdk_cost_usd=state.sdk_cost_usd,
            sdk_tokens=state.sdk_tokens,
            base_session_id=state.base_session_id,
            session_id=state.session_id,
            turns=state.turns,
            elapsed_before_s=state.elapsed_s,
        )
        stream = self._stream(request.scope, request.config, request.budget, phases, translator)
        async with contextlib.aclosing(stream):
            async for event in stream:
                yield event

    async def cancel(self, run_id: str) -> None:
        self._cancelled_runs.add(run_id)
        self._paused.discard(run_id)
        active = self._active.get(run_id)
        if active is None:
            return
        active.cancelled.set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.cancel_grace_s
        client = active.client
        if client is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.interrupt(), timeout=self._settings.cancel_grace_s)
        pump = active.pump
        if pump is None or pump.done():
            return
        remaining = max(0.0, deadline - loop.time())
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(pump), timeout=remaining)
        if not pump.done():
            # The CLI did not stop in time. End the stream now; the pump cleans up after.
            active.send(_cancelled(active.usage()))
            pump.cancel()

    # -- checks ----------------------------------------------------------------------------

    def _check_config(self, config: RunConfig) -> str | None:
        if config.runtime is not RuntimeKind.CLAUDE_AGENT_SDK:
            return f"config is for runtime {config.runtime}, not claude_agent_sdk"
        return None

    def _check_resume(self, request: ResumeRequest, state: _State | None) -> str | None:
        run_id = request.scope.run_id
        if state is None or state.pending is None:
            return "runtime_state has no pending approval"
        if state.pending.approval_id != request.approval_id:
            return f"unknown approval: {request.approval_id}"
        if state.run_id != run_id:
            return f"approval {request.approval_id} belongs to another run"
        if request.approval_id in self._used_approvals:
            return f"approval {request.approval_id} was already used"
        if run_id in self._cancelled_runs:
            return f"run {run_id} was cancelled"
        if run_id in self._active:
            return f"run {run_id} is already active"
        return None

    # -- streaming -------------------------------------------------------------------------

    async def _stream(
        self,
        scope: RunScope,
        config: RunConfig,
        budget: Budget,
        phases: Sequence[_Phase],
        translator: _Translator,
    ) -> AsyncGenerator[RunEvent]:
        run_id = scope.run_id
        active = _ActiveRun(translator=translator)
        self._active[run_id] = active
        pump = asyncio.create_task(self._pump(scope, config, budget, phases, translator, active))
        active.pump = pump
        self._pumps.add(pump)
        pump.add_done_callback(self._pumps.discard)
        try:
            while True:
                item = await active.queue.get()
                if isinstance(item, _Done):
                    # The pump always sends a terminal event before it stops.
                    self._release(run_id, active)
                    yield RunFailed(
                        code=FailureCode.INTERNAL,
                        message="the stream ended without a terminal event",
                        usage=translator.usage,
                    )
                    return
                if is_terminal(item):
                    self._release(run_id, active)
                    if isinstance(item, ApprovalRequired):
                        if active.cancelled.is_set():
                            # cancel() came after the pump queued the approval. A cancelled
                            # run cannot pause.
                            item = _cancelled(item.usage)
                        else:
                            self._paused.add(run_id)
                    yield item
                    return
                yield item
        finally:
            self._release(run_id, active)
            if not pump.done():
                pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump

    def _release(self, run_id: str, owner: _ActiveRun) -> None:
        if self._active.get(run_id) is owner:
            del self._active[run_id]

    async def _pump(
        self,
        scope: RunScope,
        config: RunConfig,
        budget: Budget,
        phases: Sequence[_Phase],
        translator: _Translator,
        active: _ActiveRun,
    ) -> None:
        """Owns the SDK clients. Every SDK call happens in this task."""

        def send(event: RunEvent) -> None:
            if isinstance(event, RunFailed) and event.runtime_state is None:
                event = event.model_copy(update={"runtime_state": translator.failure_state()})
            active.send(event)

        translator.started_at = asyncio.get_running_loop().time()
        wall_time: float | None = None
        if budget.max_wall_time_s is not None:
            wall_time = max(0.0, budget.max_wall_time_s - translator.elapsed_before_s)
        try:
            async with asyncio.timeout(wall_time):
                for phase in phases:
                    if active.cancelled.is_set():
                        break
                    if wall_time == 0.0:
                        raise TimeoutError
                    exhausted = _exhausted(budget, translator)
                    if exhausted is not None:
                        send(
                            RunFailed(
                                code=FailureCode.BUDGET_EXCEEDED,
                                message=f"the run reached {exhausted}",
                                usage=translator.usage,
                            )
                        )
                        break
                    terminal = await self._run_phase(
                        scope, config, budget, phase, translator, active
                    )
                    if terminal is not None:
                        send(terminal)
                        break
            if active.cancelled.is_set():
                send(_cancelled(translator.usage))
        except TimeoutError:
            send(
                RunFailed(
                    code=FailureCode.TIMEOUT,
                    message="the run reached max_wall_time_s",
                    usage=translator.usage,
                )
            )
        except asyncio.CancelledError:
            send(_cancelled(translator.usage))
            raise
        except _UnexpectedResult as exc:
            send(RunFailed(code=FailureCode.INTERNAL, message=str(exc), usage=translator.usage))
        except ClaudeSDKError as exc:
            send(
                RunFailed(
                    code=FailureCode.INTERNAL,
                    # The text of an SDK error can hold raw CLI output. Only the type goes out.
                    message=f"claude agent sdk error: {type(exc).__name__}",
                    usage=translator.usage,
                )
            )
        except Exception as exc:
            send(
                RunFailed(
                    code=FailureCode.INTERNAL,
                    message=f"unexpected error: {type(exc).__name__}",
                    usage=translator.usage,
                )
            )
        finally:
            active.queue.put_nowait(_Done())

    async def _run_phase(
        self,
        scope: RunScope,
        config: RunConfig,
        budget: Budget,
        phase: _Phase,
        translator: _Translator,
        active: _ActiveRun,
    ) -> TerminalEvent | None:
        """Runs one CLI process. Returns the terminal event, or None to go to the next phase."""
        options = self._options(scope, config, budget, phase, translator)
        client = self._client_factory(options)
        active.client = client
        final: ResultMessage | None = None
        try:
            await client.connect()
            if phase.prompt is not None:
                await client.query(phase.prompt)
            async for message in client.receive_messages():
                if isinstance(message, ResultMessage):
                    translator.add_result_usage(message)
                    final = message
                    break
                for event in translator.feed(message):
                    active.send(event)
            if final is not None and phase.drop_tool is None:
                early = translator.terminal(final, active.cancelled.is_set())
                if isinstance(early, RunCompleted):
                    # A completed run has nothing more to count, so the answer goes out now
                    # and does not wait for the CLI to stop (Section 16.2). ``aclose`` still
                    # waits for the CLI, so the runner exits only after the CLI.
                    active.send(early)
        finally:
            active.client = None
            await self._disconnect(client, translator.session_id)
        # Map other results only after the CLI stopped, so the wall time in runtime_state
        # also counts the time to stop the CLI.
        if final is None:
            raise _UnexpectedResult("the CLI stopped before it sent a result")
        if phase.drop_tool is None:
            return translator.terminal(final, active.cancelled.is_set())
        if active.cancelled.is_set() or final.terminal_reason in _ABORTED:
            return _cancelled(translator.usage)
        if final.stop_reason != "tool_deferred_unavailable":
            raise _UnexpectedResult(
                f"the CLI did not drop the rejected tool call (stop_reason: {final.stop_reason})"
            )
        return None

    async def _disconnect(self, client: ClaudeClient, session_id: str | None = None) -> None:
        """Stops the CLI process in a task of its own.

        The SDK stops the CLI in steps (close stdin, terminate, kill), and this can take about
        20 seconds. A cancel of that work can leave the CLI running, so the adapter never
        cancels it. It waits up to ``cancel_grace_s`` and lets the task finish after. The
        runtime keeps a reference to the task until it ends. ``aclose`` waits for all of them.
        """

        async def stop() -> None:
            with contextlib.suppress(Exception):
                await client.disconnect()

        task = asyncio.create_task(stop())
        self._cleanups.add(task)
        task.add_done_callback(self._cleanups.discard)
        if session_id is not None:
            self._stopping[session_id] = task

            def forget(done: asyncio.Task[None]) -> None:
                if self._stopping.get(session_id) is done:
                    del self._stopping[session_id]

            task.add_done_callback(forget)
        # asyncio.wait does not cancel the task on a timeout or when this task is cancelled.
        await asyncio.wait({task}, timeout=self._settings.cancel_grace_s)

    async def aclose(self) -> None:
        """Waits until every CLI process that the adapter started has stopped.

        It waits for the pumps first, because a pump that ``cancel`` stopped starts its
        ``disconnect`` task only when it unwinds.
        """
        while self._pumps or self._cleanups:
            await asyncio.wait(self._pumps | self._cleanups)

    # -- options ---------------------------------------------------------------------------

    def _options(
        self,
        scope: RunScope,
        config: RunConfig,
        budget: Budget,
        phase: _Phase,
        translator: _Translator,
    ) -> ClaudeAgentOptions:
        settings = self._settings
        prefix = config.render_prefix()
        system_prompt: str | SystemPromptPreset
        if settings.claude_code_preset:
            system_prompt = {
                "type": "preset",
                "preset": "claude_code",
                "append": prefix,
                "exclude_dynamic_sections": True,
            }
        else:
            system_prompt = prefix
        env = {TOOL_SEARCH_ENV: "false", **settings.env}
        if config.subagents:
            env[BACKGROUND_TASKS_ENV] = "1"
        disallowed: list[str] = []
        if phase.drop_tool is not None:
            env[DROP_DEFERRED_ENV] = "1"
            disallowed.append(phase.drop_tool)
        return ClaudeAgentOptions(
            model=config.model,
            system_prompt=system_prompt,
            mcp_servers=_mcp_servers(config),
            agents=_agents(config),
            strict_mcp_config=True,
            setting_sources=[],
            disallowed_tools=disallowed,
            resume=translator.session_id,
            max_turns=_remaining_turns(budget, translator),
            max_budget_usd=_remaining_cost(budget, translator),
            include_partial_messages=True,
            hooks=self._hooks(scope, config, phase, translator),
            can_use_tool=_can_use_tool(translator),
            env=env,
            cwd=settings.cwd,
            cli_path=settings.cli_path,
        )

    def _hooks(
        self, scope: RunScope, config: RunConfig, phase: _Phase, translator: _Translator
    ) -> dict[Any, list[HookMatcher]]:
        hooks: dict[Any, list[HookMatcher]] = {
            "PreToolUse": [HookMatcher(hooks=[self._hook(scope, config, phase, translator)])]
        }
        if self._archiver is not None:
            hooks["PreCompact"] = [HookMatcher(hooks=[self._archive_hook(scope, self._archiver)])]
        return hooks

    def _archive_hook(self, scope: RunScope, archiver: Archiver) -> HookCallback:
        """The ``PreCompact`` hook: gives the transcript to the archiver before the CLI
        compacts it (Section 6.3). A failed archive does not stop the compaction, because a
        session that is full must still be able to make room."""

        async def pre_compact(
            data: HookInput, tool_use_id: str | None, context: HookContext
        ) -> SyncHookJSONOutput:
            raw = cast(Mapping[str, Any], data)
            if raw.get("hook_event_name") != "PreCompact":
                return {}
            trigger: Literal["auto", "manual"] = (
                "manual" if raw.get("trigger") == "manual" else "auto"
            )
            path = raw.get("transcript_path")
            try:
                entries, left_out = (
                    await asyncio.to_thread(read_transcript, path)
                    if isinstance(path, str) and path
                    else ([], 0)
                )
                if entries:
                    # Whole entries stay out when the transcript is over the limits of the
                    # wire. No text is cut here: the control plane redacts it first.
                    bounded, omitted = bound_for_wire(entries, left_out)
                    await archiver.archive(
                        scope, TranscriptArchive(trigger=trigger, entries=bounded, omitted=omitted)
                    )
            except Exception as exc:
                log.warning(
                    "the transcript archive failed run=%s: %s", scope.run_id, type(exc).__name__
                )
            return {}

        return pre_compact

    def _hook(
        self, scope: RunScope, config: RunConfig, phase: _Phase, translator: _Translator
    ) -> HookCallback:
        gate = self._gate
        subagents = frozenset(agent.name for agent in config.subagents)
        if phase.approved_call is not None:
            translator.allowed_calls.add(phase.approved_call)

        async def pre_tool_use(
            data: HookInput, tool_use_id: str | None, context: HookContext
        ) -> SyncHookJSONOutput:
            raw = cast(Mapping[str, Any], data)
            if raw.get("hook_event_name") != "PreToolUse":
                return {}
            tool_name = str(raw.get("tool_name", ""))
            arguments = raw.get("tool_input") or {}
            call_id = str(raw.get("tool_use_id") or tool_use_id or "")
            problem = _subagent_problem(tool_name, arguments, subagents)
            if problem is not None:
                # Sama never starts a sub-agent that the profile does not define. The call
                # does not reach the gate, so the gate has no record of it.
                return _deny(problem)
            if call_id and call_id == phase.approved_call:
                # A human approved this call. The worker checked it again before the resume
                # (GateService.reconfirm), and that check made the ticket of the call.
                decision = GateDecision("allow", "approved by the user")
            else:
                try:
                    decision = await gate.check(
                        scope, tool_name, arguments, call_id=call_id, child="agent_id" in raw
                    )
                except Exception as exc:
                    decision = GateDecision("deny", f"the tool gate failed: {type(exc).__name__}")
            action: Literal["allow", "deny", "defer"]
            if decision.action == "ask" and "agent_id" in raw:
                # A sub-agent cannot pause the run. The gate maps ``ask`` to ``deny`` for a
                # child call itself (``child``). This stays for a gate that does not.
                action = "deny"
            elif decision.action == "ask":
                action = "defer"
                translator.ask_reasons[call_id] = decision.reason
            else:
                action = decision.action
            if action == "allow" and call_id:
                translator.allowed_calls.add(call_id)
            return _pre_tool_use_output(
                action, decision.reason or f"sama tool gate: {action}", steer=decision.steer
            )

        return pre_tool_use


def _pre_tool_use_output(
    action: Literal["allow", "deny", "defer"], reason: str, *, steer: str = ""
) -> SyncHookJSONOutput:
    output: PreToolUseHookSpecificOutput = {
        "hookEventName": "PreToolUse",
        "permissionDecision": action,
        "permissionDecisionReason": reason,
    }
    if steer:
        # The notes of ``/steer`` reach the model with this call (Section 4.3).
        output["additionalContext"] = steer
    return {"hookSpecificOutput": output}


def _deny(reason: str) -> SyncHookJSONOutput:
    return _pre_tool_use_output("deny", reason)


def _subagent_problem(
    tool_name: str, arguments: Mapping[str, Any], defined: frozenset[str]
) -> str | None:
    """Why a call of the sub-agent tool cannot start, or ``None``. The CLI has built-in
    sub-agents that sama does not define, and a sub-agent can run in the background after
    the run ended. Sama allows neither. Another tool gives ``None``."""
    if tool_name not in _SUBAGENT_TOOLS:
        return None
    kind = arguments.get("subagent_type")
    if not isinstance(kind, str) or kind not in defined:
        return "this profile does not define that sub-agent"
    if arguments.get("run_in_background"):
        return "a sub-agent cannot run in the background"
    return None


def _agents(config: RunConfig) -> dict[str, AgentDefinition] | None:
    """The ``agents`` option: the sub-agents of the config, or ``None`` when it has none."""
    if not config.subagents:
        return None
    return {agent.name: _agent(agent) for agent in config.subagents}


def _agent(agent: SubagentDefinition) -> AgentDefinition:
    tools = [tool if tool in _NATIVE_TOOLS else f"{_SAMA_MCP_PREFIX}{tool}" for tool in agent.tools]
    return AgentDefinition(
        description=agent.description,
        prompt=agent.prompt,
        tools=tools,
        # A sub-agent cannot start a sub-agent.
        disallowedTools=sorted(_SUBAGENT_TOOLS),
        model=agent.model or "inherit",
        maxTurns=agent.max_turns,
        background=False,
    )


def _can_use_tool(translator: _Translator) -> CanUseTool:
    """The permission callback of the CLI. It allows only calls that the hook or a human allowed.

    The hook decides each new call, so the CLI asks this callback only in other cases. One
    case is the deferred call on an approve resume: there the CLI can skip the hook.
    """

    async def can_use_tool(
        tool_name: str, arguments: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        if context.tool_use_id and context.tool_use_id in translator.allowed_calls:
            return PermissionResultAllow()
        return PermissionResultDeny(message="The sama tool gate did not allow this call.")

    return can_use_tool


def _mcp_servers(config: RunConfig) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    for server in config.mcp_servers:
        entry: McpHttpServerConfig = {"type": "http", "url": server.url}
        if server.bearer_token is not None:
            entry["headers"] = {"Authorization": f"Bearer {server.bearer_token.get_secret_value()}"}
        servers[server.name] = entry
    return servers


def _remaining_turns(budget: Budget, translator: _Translator) -> int | None:
    if budget.max_turns is None:
        return None
    return budget.max_turns - translator.turns


def _remaining_cost(budget: Budget, translator: _Translator) -> float | None:
    """The CLI counts ``max_budget_usd`` per process, so each process gets only the rest."""
    if budget.max_cost_usd is None:
        return None
    return float(budget.max_cost_usd - translator.usage.cost_usd)


def _exhausted(budget: Budget, translator: _Translator) -> str | None:
    """The name of a budget limit that the run already used up, or None."""
    remaining_turns = _remaining_turns(budget, translator)
    if remaining_turns is not None and remaining_turns <= 0:
        return "max_turns"
    remaining_cost = _remaining_cost(budget, translator)
    if remaining_cost is not None and remaining_cost <= 0:
        return "max_cost_usd"
    return None


def _invalid(message: str) -> RunFailed:
    return RunFailed(code=FailureCode.INVALID_REQUEST, message=message)


def _cancelled(usage: Usage) -> RunFailed:
    return RunFailed(code=FailureCode.CANCELLED, message="cancelled", usage=usage)
