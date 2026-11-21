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
    CLAUDE_CODE = "claude_code"  # the Claude Code CLI, headless (M7.2, Section 15.4)
    CODEX = "codex"  # the OpenAI Codex CLI, headless (M7.2, Section 15.4)
    FAKE = "fake"  # scripted, for tests


ReasoningEffort = Literal["low", "medium", "high"]
REASONING_EFFORTS: Final[tuple[str, ...]] = ("low", "medium", "high")
"""The reasoning efforts that an agent profile can set (M7.10). Each adapter maps them."""


CODING_RUNTIMES: Final = frozenset({RuntimeKind.CLAUDE_CODE, RuntimeKind.CODEX})
"""The runtimes that run a coding agent CLI in the session sandbox (Section 15.4). The control
plane drives the CLI. The sandbox runner does not start them."""


class Capabilities(_Frozen):
    """What a runtime does natively. Sama fills each gap itself."""

    streaming: bool
    native_subagents: bool
    native_compaction: bool
    native_skills: bool
    native_file_checkpoints: bool
    resumable_sessions: bool
    mcp_client: bool
    manual_compaction: bool = False
    """The runtime compacts its context when a ``RunRequest`` has ``compact`` (``/compact``,
    Section 6.3). A runtime without it fails such a request with ``INVALID_REQUEST``."""
    compaction_archive: bool = False
    """The runtime gives the transcript to an archiver before it compacts (Section 6.3)."""
    mid_run_steer: bool = False
    """The runtime gives the notes of ``/steer`` to the model at the next tool call of the run
    (Section 4.3). For a runtime without it, the notes stay pending, and they go to the session
    as one new message after the run completes."""


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


CONTEXT_FILE_PATH_MAX_CHARS: Final = 200
CONTEXT_FILE_PATH_PATTERN: Final = rf'^[^"<>\r\n]{{1,{CONTEXT_FILE_PATH_MAX_CHARS}}}$'
"""A path is the attribute of a ``<context_file>`` tag, so it cannot close the tag."""


class ContextFile(_Frozen):
    path: str = Field(pattern=CONTEXT_FILE_PATH_PATTERN)
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


SUBAGENT_NAME_PATTERN: Final = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
SUBAGENT_NAME_MAX_CHARS: Final = 40
SUBAGENT_DESCRIPTION_MAX_CHARS: Final = 300
SUBAGENT_PROMPT_MAX_CHARS: Final = 8_000
SUBAGENT_MAX_TOOLS: Final = 64
SUBAGENT_MAX_TURNS: Final = 50
SUBAGENT_WIRE_MAX_CHARS: Final = 100_000
"""The longest text of a ``SubagentEvent`` on the wire. The runner has no redactor, so it never
cuts a text: a cut could split a secret. A longer text stays out whole, and a fixed marker takes
its place (``bound_subagent_text``). The control plane redacts the whole text and then cuts it
to ``SUBAGENT_STORED_MAX_CHARS``."""
SUBAGENT_STORED_MAX_CHARS: Final = 2_000
"""The longest text of a stored sub-agent event. The control plane cuts after the redaction."""
"""The limits of a sub-agent definition (docs/architecture.md, Section 13.1)."""


class SubagentDefinition(_Frozen):
    """A native sub-agent of a run: a short side task with its own prompt and tools.

    Only a runtime with the capability ``native_subagents`` uses it (Section 13.1). The
    control plane builds it from the agent profile, so a definition is part of the frozen
    config and the model can call only these sub-agents. ``tools`` are sama tool names. They
    are never more than the tools of the run (``sama.subagents.resolve``), and the tool gate
    still checks every call of a sub-agent, in the same way as a call of the main agent.
    """

    name: str = Field(max_length=SUBAGENT_NAME_MAX_CHARS, pattern=SUBAGENT_NAME_PATTERN)
    description: str = Field(min_length=1, max_length=SUBAGENT_DESCRIPTION_MAX_CHARS)
    """Tells the main agent when to use this sub-agent."""
    prompt: str = Field(min_length=1, max_length=SUBAGENT_PROMPT_MAX_CHARS)
    """The system prompt of the sub-agent."""
    tools: tuple[str, ...] = Field(default=(), max_length=SUBAGENT_MAX_TOOLS)
    """The tools that the sub-agent can call. An empty list means no tool."""
    model: str | None = Field(default=None, min_length=1, max_length=100)
    """``None``: the sub-agent uses the model of the run."""
    max_turns: int | None = Field(default=None, gt=0, le=SUBAGENT_MAX_TURNS)

    @model_validator(mode="after")
    def _tools_are_unique(self) -> Self:
        _require_unique("sub-agent tool", self.tools)
        return self


class RunConfig(_Frozen):
    """What the model sees. Frozen for the life of a session (decision D9)."""

    runtime: RuntimeKind
    model: str = Field(min_length=1)
    system: str = ""
    """Sama identity and tool-use guidance (stable tier)."""
    soul: str = ""
    context_files: tuple[ContextFile, ...] = ()
    skill_index: tuple[SkillIndexEntry, ...] = ()
    memory_snapshot: str = ""
    environment: str = ""
    """Facts of the session start, for example the date (volatile tier)."""
    tool_schemas: tuple[ToolSchema, ...] = ()
    mcp_servers: tuple[McpServerConfig, ...] = ()
    subagents: tuple[SubagentDefinition, ...] = ()
    """Native sub-agents. A runtime without ``native_subagents`` ignores them. The control
    plane does not put them in the config of such a runtime."""
    reasoning_effort: ReasoningEffort | None = None
    """How much the model thinks before it answers. ``None``: the default of the model. Each
    adapter passes it to its SDK."""

    @model_validator(mode="after")
    def _names_are_unique(self) -> Self:
        _require_unique("context file path", (f.path for f in self.context_files))
        _require_unique("skill name", (s.name for s in self.skill_index))
        _require_unique("tool name", (t.name for t in self.tool_schemas))
        _require_unique("MCP server name", (m.name for m in self.mcp_servers))
        _require_unique("sub-agent name", (a.name for a in self.subagents))
        return self

    def fingerprint(self) -> str:
        """A stable SHA-256 hex digest of the config.

        The same config gives the same digest in every process and on every machine. Secrets
        (run tokens) are not part of it. Sama uses the digest to check that a session's
        prefix did not change.
        """
        payload = self.model_dump(mode="json")
        if not self.subagents:
            # A config with no sub-agent keeps the digest that it had before the field existed.
            del payload["subagents"]
        if self.reasoning_effort is None:
            # The same for a config with no reasoning effort.
            del payload["reasoning_effort"]
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def render_prefix(self) -> str:
        """The system prompt prefix in the three tiers of Section 6.1: stable, context, volatile.

        Tool schemas and MCP servers are not in the text. The runtime gets them through its SDK.
        Empty parts are left out. The output is deterministic for the same config.
        """
        parts: list[str] = []
        if self.system.strip():
            parts.append(self.system.strip())
        if self.soul.strip():
            parts.append(self.soul.strip())
        for f in self.context_files:
            parts.append(f'<context_file path="{f.path}">\n{f.content}\n</context_file>')
        if self.skill_index:
            lines = "\n".join(f"- {s.name}: {s.description}" for s in self.skill_index)
            parts.append(f"<skills>\n{lines}\n</skills>")
        if self.memory_snapshot.strip():
            parts.append(f"<memory>\n{self.memory_snapshot.strip()}\n</memory>")
        if self.environment.strip():
            parts.append(f"<environment>\n{self.environment.strip()}\n</environment>")
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


BUDGET_KEYS: Final = tuple(Budget.model_fields)
"""The keys of a run budget: ``max_turns``, ``max_tokens``, ``max_cost_usd``, and
``max_wall_time_s``. The profile, the queue, and the API use this one list."""

MAX_COMPACT_FOCUS_CHARS: Final = 6_000
"""The most text that a ``/compact`` request can give to the summary (the focus and the pins)."""


class CompactRequest(_Frozen):
    """Asks the runtime to compact the context of the session now, instead of it answering a
    message (docs/architecture.md, Section 6.3)."""

    focus: str = Field(default="", max_length=MAX_COMPACT_FOCUS_CHARS)
    """What the summary must keep. Empty: the runtime decides."""


class RunRequest(_Frozen):
    scope: RunScope
    input: str
    """The user message. Empty for a ``compact`` request."""
    config: RunConfig
    budget: Budget = Budget()
    runtime_state: bytes | None = None
    """The opaque state that the same runtime returned at the end of the previous run of this
    session. ``None`` for the first run."""
    compact: CompactRequest | None = None
    """Set for a ``/compact`` run: the runtime compacts the session and sends no answer. It
    needs ``runtime_state``, because a session with no earlier run has nothing to compact."""


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
    """The runtime compacted its context. The control plane stores the event in
    ``session_compactions`` (Section 6.3)."""

    type: Literal["compacted"] = "compacted"
    tokens_before: int = Field(ge=0)
    tokens_after: int = Field(ge=0)
    trigger: Literal["auto", "manual"] = "auto"
    """``manual`` after a ``compact`` request, ``auto`` when the runtime decided itself."""


def bound_subagent_text(text: str) -> str:
    """The text for a ``SubagentEvent``: the text itself, or, when it is over the limit of the
    wire, a marker that has nothing of it. It never cuts, so a secret cannot lose a part."""
    if len(text) <= SUBAGENT_WIRE_MAX_CHARS:
        return text
    return f"[a text of {len(text)} characters is not in this event]"


def clip_subagent_text(text: str) -> str:
    """The text of a stored sub-agent event. Call it after the redaction."""
    if len(text) <= SUBAGENT_STORED_MAX_CHARS:
        return text
    return text[:SUBAGENT_STORED_MAX_CHARS]


class SubagentEvent(_Event):
    """Progress from a native sub-agent of the runtime, as one line of text."""

    type: Literal["subagent_event"] = "subagent_event"
    subagent: str = Field(min_length=1)
    text: str = Field(max_length=SUBAGENT_WIRE_MAX_CHARS)
    call_id: str | None = None
    """The id of the tool call that started the sub-agent. It groups the events of one
    invocation. ``None``: the runtime did not tell it."""
    done: bool = False
    """The last event of the invocation: the sub-agent ended, with any status."""
    tokens: int | None = Field(default=None, ge=0)
    """The tokens that the sub-agent used in this invocation. Only the last event has it, and
    only when the runtime reports it."""


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
    runtime_state: bytes | None = None
    """Optional. The state from which the next run of the session starts, if the runtime must
    keep something that the failure would lose (for example, the count of cost that the runtime
    reports for a whole session). It is not a state to resume the failed run: the conversation
    stays the one of the last completed run. A runtime that has nothing to keep sets nothing."""


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
