"""The runtime contract that every agent runtime adapter implements.

Each adapter (Claude Agent SDK, OpenAI Agents SDK, fake) maps this contract to the API of its
SDK. Sama features (tenancy, approvals, memory, skills, audit) sit above this seam, so they
work the same for every runtime. See docs/architecture.md, Section 5.

Two rules from the design shape this module:

- ``RunConfig`` is frozen per session. It holds only what the model sees and what the prompt
  cache needs. Authority (membership, policy, budgets, revocations) is not in it: the control
  plane checks authority again at every call.
- Every event carries the running ``Usage``. A stream ends with exactly one terminal event:
  ``ApprovalRequired``, ``RunCompleted``, or ``RunFailed``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal, Protocol, Self, runtime_checkable

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    SecretStr,
    TypeAdapter,
    model_validator,
)

# ---------------------------------------------------------------------------------------------
# Base model
# ---------------------------------------------------------------------------------------------


class _Frozen(BaseModel):
    """Immutable value object. Unknown fields are an error, so typos fail early.

    Bytes (the opaque ``runtime_state``) are base64 in JSON, so any byte value can be stored
    and sent.
    """

    model_config = ConfigDict(
        frozen=True, extra="forbid", ser_json_bytes="base64", val_json_bytes="base64"
    )


def _freeze_json(value: Any) -> Any:
    """Makes a JSON value read-only: objects become read-only mappings, arrays become tuples.

    The result is a new value, so a later change to the input does not change the model.
    """
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze_json(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(v) for v in value)
    return value


def _thaw_json(value: Any) -> Any:
    """The reverse of ``_freeze_json``, for serialisation. Returns plain dicts and lists."""
    if isinstance(value, Mapping):
        return {k: _thaw_json(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(v) for v in value]
    return value


FrozenJsonObject = Annotated[
    Mapping[str, Any],
    AfterValidator(_freeze_json),
    PlainSerializer(_thaw_json, return_type=dict[str, Any]),
]
"""A JSON object that cannot change after validation, also in nested values.

``frozen=True`` stops field assignment only. This type also stops changes inside the object,
so a frozen ``RunConfig`` keeps the same fingerprint for the life of a session.
"""


# ---------------------------------------------------------------------------------------------
# Runtime identity and capabilities
# ---------------------------------------------------------------------------------------------


class RuntimeKind(StrEnum):
    CLAUDE_AGENT_SDK = "claude_agent_sdk"  # default
    OPENAI_AGENTS_SDK = "openai_agents_sdk"
    FAKE = "fake"  # scripted, for tests


class Capabilities(_Frozen):
    """What a runtime does natively. Sama fills each gap itself."""

    streaming: bool
    native_subagents: bool
    native_compaction: bool
    native_skills: bool
    native_file_checkpoints: bool
    resumable_sessions: bool
    mcp_client: bool


# ---------------------------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------------------------


class Audience(StrEnum):
    """Who a session is for (docs/architecture.md, Section 11.3)."""

    PRIVATE = "private"  # one user, for example a DM. Runs as that user.
    SHARED = "shared"  # a channel, thread, or group. Runs as the workspace.


class RunScope(_Frozen):
    """The tenant scope of one run. Every query and tool call is scoped by it."""

    org_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    audience: Audience
    user_id: str | None = None
    """The user for a private session. ``None`` for a shared session."""

    @model_validator(mode="after")
    def _principal_matches_audience(self) -> Self:
        if self.audience is Audience.PRIVATE and not self.user_id:
            raise ValueError("a private session needs a user_id")
        if self.audience is Audience.SHARED and self.user_id is not None:
            raise ValueError("a shared session runs as the workspace and has no user_id")
        return self


# ---------------------------------------------------------------------------------------------
# Frozen run configuration
# ---------------------------------------------------------------------------------------------


CONTEXT_FILE_MAX_CHARS: Final = 20_000
"""Cap for one context file, as in Hermes (docs/architecture.md, Section 6.1)."""


class ContextFile(_Frozen):
    path: str = Field(min_length=1)
    content: str = Field(max_length=CONTEXT_FILE_MAX_CHARS)


class SkillIndexEntry(_Frozen):
    """One line of the skill index. The full skill loads only when the agent needs it."""

    name: str = Field(min_length=1)
    description: str


class ToolSchema(_Frozen):
    name: str = Field(min_length=1, max_length=64)
    description: str
    input_schema: FrozenJsonObject = Field(default_factory=dict, validate_default=True)


class McpServerConfig(_Frozen):
    """A remote MCP server that the runtime connects to, normally the sama MCP tool server.

    The bearer token is a run token. It changes per run, so it is not part of the
    fingerprint, and ``repr`` hides it.
    """

    name: str = Field(min_length=1)
    url: str = Field(min_length=1)
    bearer_token: SecretStr | None = Field(default=None, exclude=True)


class RunConfig(_Frozen):
    """What the model sees. Frozen for the life of a session (decision D9)."""

    runtime: RuntimeKind
    model: str = Field(min_length=1)
    soul: str = ""
    context_files: tuple[ContextFile, ...] = ()
    skill_index: tuple[SkillIndexEntry, ...] = ()
    memory_snapshot: str = ""
    tool_schemas: tuple[ToolSchema, ...] = ()
    mcp_servers: tuple[McpServerConfig, ...] = ()

    @model_validator(mode="after")
    def _names_are_unique(self) -> Self:
        _require_unique("context file path", (f.path for f in self.context_files))
        _require_unique("skill name", (s.name for s in self.skill_index))
        _require_unique("tool name", (t.name for t in self.tool_schemas))
        _require_unique("MCP server name", (m.name for m in self.mcp_servers))
        return self

    def fingerprint(self) -> str:
        """A stable SHA-256 hex digest of the config.

        The same config gives the same digest in every process and on every machine. Secrets
        (run tokens) are not part of it. Sama uses the digest to check that a session's
        prefix did not change.
        """
        payload = self.model_dump(mode="json")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def render_prefix(self) -> str:
        """The system prompt prefix in the three tiers of Section 6.1: stable, context, volatile.

        Tool schemas and MCP servers are not in the text. The runtime gets them through its SDK.
        Empty parts are left out. The output is deterministic for the same config.
        """
        parts: list[str] = []
        if self.soul.strip():
            parts.append(self.soul.strip())
        for f in self.context_files:
            parts.append(f'<context_file path="{f.path}">\n{f.content}\n</context_file>')
        if self.skill_index:
            lines = "\n".join(f"- {s.name}: {s.description}" for s in self.skill_index)
            parts.append(f"<skills>\n{lines}\n</skills>")
        if self.memory_snapshot.strip():
            parts.append(f"<memory>\n{self.memory_snapshot.strip()}\n</memory>")
        return "\n\n".join(parts)


def _require_unique(label: str, values: Iterable[str]) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {label}: {value}")
        seen.add(value)


# ---------------------------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------------------------


class Budget(_Frozen):
    """Limits for one run (docs/architecture.md, Section 6.2). ``None`` means no limit.

    The model proxy and the orchestrator enforce budgets. The adapter passes them to the SDK
    where the SDK supports them.
    """

    max_turns: int | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    max_cost_usd: Decimal | None = Field(default=None, gt=0)
    max_wall_time_s: float | None = Field(default=None, gt=0)


class RunRequest(_Frozen):
    scope: RunScope
    input: str
    config: RunConfig
    budget: Budget = Budget()
    runtime_state: bytes | None = None
    """The opaque state that the same runtime returned at the end of the previous run of this
    session. ``None`` for the first run."""


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ResumeRequest(_Frozen):
    """Continues a run that ended with ``ApprovalRequired``."""

    scope: RunScope
    config: RunConfig
    approval_id: str = Field(min_length=1)
    decision: ApprovalDecision
    reason: str | None = None
    budget: Budget = Budget()
    runtime_state: bytes | None = None
    """The state from the ``ApprovalRequired`` event."""


# ---------------------------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------------------------


class Usage(_Frozen):
    """Running totals for the run so far. The model proxy record is the source of truth."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(default=Decimal(0), ge=0)

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


# ---------------------------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------------------------


class FailureCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    MODEL_ERROR = "model_error"
    TOOL_ERROR = "tool_error"
    SANDBOX_ERROR = "sandbox_error"
    BUDGET_EXCEEDED = "budget_exceeded"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


class _Event(_Frozen):
    usage: Usage = Usage()


class TextDelta(_Event):
    type: Literal["text_delta"] = "text_delta"
    text: str


class ToolCallStarted(_Event):
    type: Literal["tool_call_started"] = "tool_call_started"
    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: FrozenJsonObject = Field(default_factory=dict, validate_default=True)


class ToolCallFinished(_Event):
    type: Literal["tool_call_finished"] = "tool_call_finished"
    call_id: str = Field(min_length=1)
    is_error: bool = False
    output_preview: str = ""


class Compacted(_Event):
    type: Literal["compacted"] = "compacted"
    tokens_before: int = Field(ge=0)
    tokens_after: int = Field(ge=0)


class SubagentEvent(_Event):
    """Progress from a native sub-agent of the runtime, as one line of text."""

    type: Literal["subagent_event"] = "subagent_event"
    subagent: str = Field(min_length=1)
    text: str


class ApprovalRequired(_Event):
    """Terminal. The run pauses until a human decides. Resume with ``ResumeRequest``."""

    type: Literal["approval_required"] = "approval_required"
    approval_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: FrozenJsonObject = Field(default_factory=dict, validate_default=True)
    reason: str = ""
    runtime_state: bytes | None = None


class RunCompleted(_Event):
    """Terminal. The run ended normally."""

    type: Literal["run_completed"] = "run_completed"
    output: str
    runtime_state: bytes | None = None


class RunFailed(_Event):
    """Terminal. The run ended with an error."""

    type: Literal["run_failed"] = "run_failed"
    code: FailureCode
    message: str
    retryable: bool = False


RunEvent = Annotated[
    TextDelta
    | ToolCallStarted
    | ToolCallFinished
    | Compacted
    | SubagentEvent
    | ApprovalRequired
    | RunCompleted
    | RunFailed,
    Field(discriminator="type"),
]
"""One event in a run stream. The ``type`` field tags the event when it is stored."""

TerminalEvent = ApprovalRequired | RunCompleted | RunFailed
TERMINAL_EVENT_TYPES: Final = (ApprovalRequired, RunCompleted, RunFailed)

run_event_adapter: Final[TypeAdapter[RunEvent]] = TypeAdapter(RunEvent)
"""Serialises and parses events, for example ``run_event_adapter.validate_json(raw)``."""


def is_terminal(event: RunEvent) -> bool:
    return isinstance(event, TERMINAL_EVENT_TYPES)


class ContractViolation(Exception):
    """A runtime broke the event stream rules of the contract."""


def validate_event_stream(events: Sequence[RunEvent], start: Usage | None = None) -> TerminalEvent:
    """Checks the stream rules and returns the terminal event.

    Rules: the stream is not empty, it ends with exactly one terminal event, and token counts
    and cost in ``usage`` never go down. For a resumed run, ``start`` is the usage of the
    ``ApprovalRequired`` event that paused it: usage is for the whole run, so the resumed
    stream must not report less.
    """
    if not events:
        raise ContractViolation("the stream has no events")
    terminals = [i for i, e in enumerate(events) if is_terminal(e)]
    if terminals != [len(events) - 1]:
        raise ContractViolation(
            f"the stream must end with exactly one terminal event; terminal positions: "
            f"{terminals}, length: {len(events)}"
        )
    previous = start if start is not None else Usage()
    for index, event in enumerate(events):
        if not _usage_not_lower(event.usage, previous):
            raise ContractViolation(f"usage went down at event {index}")
        previous = event.usage
    last = events[-1]
    if not isinstance(last, TERMINAL_EVENT_TYPES):  # pragma: no cover -- checked above
        raise ContractViolation("the last event is not terminal")
    return last


def _usage_not_lower(current: Usage, previous: Usage) -> bool:
    return (
        current.input_tokens >= previous.input_tokens
        and current.output_tokens >= previous.output_tokens
        and current.cache_read_tokens >= previous.cache_read_tokens
        and current.cache_write_tokens >= previous.cache_write_tokens
        and current.cost_usd >= previous.cost_usd
    )


# ---------------------------------------------------------------------------------------------
# The runtime protocol and registry
# ---------------------------------------------------------------------------------------------


@runtime_checkable
class AgentRuntime(Protocol):
    @property
    def kind(self) -> RuntimeKind: ...

    def capabilities(self) -> Capabilities: ...

    def run(self, request: RunRequest) -> AsyncIterator[RunEvent]:
        """Streams the events of one run. The last event is a terminal event."""
        ...

    def resume(self, request: ResumeRequest) -> AsyncIterator[RunEvent]:
        """Continues a run after an approval decision. The last event is a terminal event."""
        ...

    async def cancel(self, run_id: str) -> None:
        """Stops a run. Its stream ends with ``RunFailed(code=CANCELLED)``.

        A run that is paused on ``ApprovalRequired`` stops too: its approvals become invalid,
        and a later ``resume`` fails with ``INVALID_REQUEST``.
        """
        ...


class UnknownRuntimeError(LookupError):
    pass


class RuntimeRegistry:
    """Maps a ``RuntimeKind`` to its adapter. Adapters register at startup."""

    def __init__(self) -> None:
        self._adapters: dict[RuntimeKind, AgentRuntime] = {}

    def register(self, runtime: AgentRuntime) -> None:
        if runtime.kind in self._adapters:
            raise ValueError(f"runtime already registered: {runtime.kind}")
        self._adapters[runtime.kind] = runtime

    def get(self, kind: RuntimeKind) -> AgentRuntime:
        try:
            return self._adapters[kind]
        except KeyError:
            raise UnknownRuntimeError(kind) from None

    def kinds(self) -> list[RuntimeKind]:
        return sorted(self._adapters)
