"""The OpenAI Agents SDK runtime adapter (docs/architecture.md, Section 5.3).

``OpenAIRuntime`` maps a ``RunRequest`` to a ``SandboxAgent`` run and the SDK stream events to
``RunEvent``s. Install the SDK with ``uv sync --group runtime-openai``.

- The model is the Responses API through the sama model proxy. The run token is the API key,
  so the sandbox has no provider key.
- The shell tool runs through the SDK local sandbox client (``UnixLocalSandboxClient``) in the
  work folder. In production the runner is already in the E2B sandbox, so the runner needs no
  E2B key. The sama tools stay MCP tools of the sama tool server (decision D3).
- The conversation is a ``SessionItems`` store. In the sandbox it is ``RemoteSessionItems``,
  which sends each operation to the tool server with the run token. The runner has no database
  credentials.
- Approvals: each tool gets ``needs_approval``, which asks the sama ``ToolGate``. ``ask``
  makes the SDK stop with an interruption. The adapter saves the SDK ``RunState`` in
  ``runtime_state`` and yields ``ApprovalRequired``. A resume loads the ``RunState`` and calls
  ``approve`` or ``reject``. The SDK asks ``needs_approval`` only for a call with no decision,
  so the approved call does not go to the gate again. A ``deny`` of the gate becomes a tool
  input guardrail that gives the model the reason in place of the tool result.
- Sub-agents (Sections 13.1 and 13.3): each ``SubagentDefinition`` of the config is an agent
  as a tool (``Agent.as_tool``) named ``subagent_<name>``. The gate sees the call as an
  ``Agent`` call with ``subagent_type``, as in the Claude adapter. The sub-agent has only the
  tools of its definition, it has no agent tool (no nested sub-agents), and each of its calls
  asks the gate with ``child``: an ``ask`` is a ``deny``, because a sub-agent cannot pause the
  run.
- Compaction: the ``Compaction`` capability turns on the server-side compaction of the
  Responses API. A manual compaction (``RunRequest.compact`` or the input ``/compact``) calls
  ``POST /responses/compact`` and replaces the items. The adapter gives the items that a
  compaction drops to the ``Archiver`` first.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import logging
import time
import uuid
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal

import httpx
from agents import (
    Agent,
    MaxTurnsExceeded,
    ModelSettings,
    Runner,
    RunState,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrail,
    ToolInputGuardrailData,
    set_tracing_disabled,
)
from agents import (
    RunConfig as SdkRunConfig,
)
from agents.items import ToolApprovalItem, TResponseInputItem
from agents.mcp import MCPServer, MCPServerStreamableHttp
from agents.memory.session import SessionABC
from agents.models.openai_responses import OpenAIResponsesModel
from agents.result import RunResultStreaming
from agents.run_config import SandboxRunConfig
from agents.run_context import RunContextWrapper
from agents.sandbox import Manifest, SandboxAgent
from agents.sandbox.capabilities import Compaction, Shell
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient
from agents.sandbox.snapshot import NoopSnapshotSpec
from agents.tool import FunctionTool, Tool, ToolOriginType, get_function_tool_origin
from agents.usage import Usage as SdkUsage
from openai import AsyncOpenAI
from openai.types.shared import Reasoning
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sama.runtime.archive import ArchiveEntry, Archiver, TranscriptArchive, bound_for_wire
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
    RunPhase,
    RunPhaseName,
    RunRequest,
    RunScope,
    RuntimeKind,
    SubagentDefinition,
    SubagentEvent,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    Usage,
    bound_subagent_text,
    is_terminal,
    output_preview,
)
from sama.runtime.gating import AllowAllGate, BoundedSet, GateDecision, ToolGate
from sama.runtime.session_items import (
    MAX_WIRE_ITEMS,
    SessionItems,
    SessionItemsError,
    SessionItemsRequest,
)

if TYPE_CHECKING:
    from sama.runtime.relay import TokenRelay

log: Final = logging.getLogger("sama.runtime.openai_agents")

DEFAULT_MAX_TURNS: Final = 200
"""The turn limit of a run with no ``max_turns``. The SDK default (10) is too low for agent
work."""
SHELL_TOOL: Final = "exec_command"
"""The name of the shell tool of the SDK. Sama calls it ``Bash`` (the gate, the events)."""
SAMA_SHELL_TOOL: Final = "Bash"
COMPACT_COMMAND: Final = "/compact"
_PROXY_BUDGET_STATUS: Final = 402
"""The model proxy refuses a call over a budget with HTTP 402 (Section 16.6)."""
_RETRYABLE_STATUS: Final = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
_PREVIEW_CHARS: Final = 2_000
_CLEANUP_S: Final = 10.0
MCP_CONNECT_S: Final = 30.0
"""The longest wait for the MCP servers of a run to connect. The servers connect at the same
time, so this is also the limit for one server."""
MCP_CALL_TIMEOUT_S: Final = 3_600.0
"""How long the runtime waits for one call of a sama MCP tool. The SDK default is 5 seconds,
and many sama tools take longer: a browser session starts in more than 5 seconds, ``clarify``
waits up to 600 seconds, ``browser_task`` and ``repo_clone`` up to 600 and 300 seconds, and
``coding_agent_run`` up to 50 minutes (M7.2), so the limit is 1 hour, the longest run. The tool
server has its own limit for each tool, so this is only the outer limit."""
_NOT_RUN_OUTPUT: Final = "The call did not run: the run ended before it."
SUBAGENT_TOOL_PREFIX: Final = "subagent_"
"""The SDK name of the tool of a sub-agent is ``subagent_<name>``."""
SAMA_SUBAGENT_TOOL: Final = "Agent"
"""The gate and the events see the call of a sub-agent as a call of ``Agent``, as on Claude."""
SAMA_MCP_PREFIX: Final = "mcp__sama__"
DEFAULT_SUBAGENT_TURNS: Final = 20
"""The turn limit of a sub-agent with no ``max_turns``."""
_CHILD_ASK: Final = "a sub-agent cannot ask for an approval"

# The SDK sends traces to OpenAI by default. Sama builds its traces from the stored rows.
set_tracing_disabled(True)


@dataclass(frozen=True, slots=True)
class OpenAISettings:
    """``model_url`` is the base URL of the Responses API (``<proxy>/openai/v1``). ``api_key``
    is the run token in the sandbox. ``cwd`` is the work folder of the shell tool."""

    cwd: str | None = None
    model_url: str | None = None
    api_key: str | None = None
    cancel_grace_s: float = 4.0
    max_retries: int = 2
    relay: TokenRelay | None = None
    """The token relay of a kept runner (Section 4.4). With it, the model client and the MCP
    clients hold only the local secret of the relay, which adds the token of the active run."""
    keeper: OpenAIKeeper | None = None
    """Keeps the MCP connections and the model client between runs of the session. It needs
    ``relay``."""
    mark: Callable[[str], None] | None = None
    """Called with the name of a step the first time that it happens in a run (the timing line
    of the runner)."""

    def __post_init__(self) -> None:
        if self.keeper is not None and self.relay is None:
            raise ValueError("a keeper needs a relay: kept clients must not hold a run token")

    def __repr__(self) -> str:
        return f"OpenAISettings(cwd={self.cwd!r}, model_url={self.model_url!r}, api_key=<hidden>)"


# ---------------------------------------------------------------------------------------------
# MCP connections: parallel connect and reuse between runs
# ---------------------------------------------------------------------------------------------


class _Owned:
    """One MCP server. One task connects it, waits, and closes it, because the SDK enters
    cancel scopes when it connects, and a scope must exit in the task that entered it. So a
    connection can outlive the task of the run that made it."""

    def __init__(self, server: MCPServer) -> None:
        self.server = server
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._ready.add_done_callback(_consume)
        self._stop = asyncio.Event()
        self.clean: bool | None = None
        """After ``close``: whether the cleanup of the server completed in its own task."""
        self._task = asyncio.create_task(self._own())

    async def _own(self) -> None:
        try:
            await self.server.connect()  # type: ignore[no-untyped-call]
        except BaseException as error:
            if not self._ready.done():
                if isinstance(error, asyncio.CancelledError):
                    self._ready.cancel()
                else:
                    self._ready.set_exception(error)
            self.clean = await _cleanup(self.server.cleanup())  # type: ignore[no-untyped-call]
            if isinstance(error, asyncio.CancelledError):
                raise
            return
        self._ready.set_result(None)
        try:
            await self._stop.wait()
        finally:
            self.clean = await _cleanup(self.server.cleanup())  # type: ignore[no-untyped-call]

    async def ready(self) -> None:
        await asyncio.shield(self._ready)

    async def close(self) -> None:
        self._stop.set()
        if not self._ready.done():
            self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task


def _consume(future: asyncio.Future[None]) -> None:
    if not future.cancelled():
        future.exception()  # a failure that nobody waits for is not an error of the loop


class _ServerSet:
    """The MCP servers of one run. They connect at the same time, and the run waits for all."""

    def __init__(self, key: str, owners: list[_Owned]) -> None:
        self.key = key
        self._owners = owners
        self.idle_since = time.monotonic()

    @property
    def servers(self) -> list[MCPServer]:
        return [owned.server for owned in self._owners]

    @property
    def clean(self) -> bool:
        """After ``close``: every server finished its cleanup in its own task."""
        return all(owned.clean is True for owned in self._owners)

    async def ready(self, timeout_s: float = MCP_CONNECT_S) -> None:
        """Waits until every server is connected. Raises the first connect error."""
        async with asyncio.timeout(timeout_s):
            await asyncio.gather(*(owned.ready() for owned in self._owners))

    async def close(self) -> None:
        await asyncio.gather(*(owned.close() for owned in self._owners), return_exceptions=True)


class OpenAIKeeper:
    """Keeps the MCP connections and the model client between runs of the session
    (docs/architecture.md, Section 4.4).

    A set of connections fits a run when the key is the same (the config fingerprint and the
    relay) and it did not wait longer than ``idle_s``. A run takes the set out (``take``), so
    one run uses it at a time, and puts it back after a clean end. An error, a cancel, a pause
    for an approval, and a timeout close it. The connections hold no token: the relay adds the
    token of the active run.
    """

    def __init__(self, *, idle_s: float = 600.0) -> None:
        self.idle_s = idle_s
        self._kept: _ServerSet | None = None
        self._client: AsyncOpenAI | None = None
        self._closing: set[asyncio.Task[None]] = set()

    @property
    def holds(self) -> bool:
        return self._kept is not None

    async def take(self, key: str) -> _ServerSet | None:
        """Gives the set to the run if it fits. Else it closes the set (and waits for it) and
        returns ``None``."""
        kept, self._kept = self._kept, None
        if kept is None:
            return None
        if kept.key == key and time.monotonic() - kept.idle_since <= self.idle_s:
            return kept
        await kept.close()
        return None

    async def retire_unless(self, key: str) -> None:
        """Closes the kept set if the next run cannot use it. The runner calls it before the
        token of the next run is active."""
        kept = self._kept
        if kept is not None and not (
            kept.key == key and time.monotonic() - kept.idle_since <= self.idle_s
        ):
            self._kept = None
            await kept.close()

    def put(self, kept: _ServerSet) -> None:
        old, self._kept = self._kept, kept
        kept.idle_since = time.monotonic()
        if old is not None:
            self._stop(old.close())

    def client(self, settings: OpenAISettings) -> AsyncOpenAI:
        """The model client. It lives as long as the keeper."""
        if self._client is None:
            self._client = _model_client(settings)
        return self._client

    def _stop(self, work: Any) -> None:
        task = asyncio.create_task(work)
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def close(self) -> None:
        kept, self._kept = self._kept, None
        if kept is not None:
            self._stop(kept.close())
        while self._closing:
            await asyncio.wait(set(self._closing))
        client, self._client = self._client, None
        if client is not None:
            await client.close()


def _model_client(settings: OpenAISettings) -> AsyncOpenAI:
    relay = settings.relay
    return AsyncOpenAI(
        base_url=relay.openai_url if relay is not None else settings.model_url,
        api_key=relay.secret if relay is not None else settings.api_key,
        max_retries=settings.max_retries,
    )


# ---------------------------------------------------------------------------------------------
# The runtime state
# ---------------------------------------------------------------------------------------------


class _PendingCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str
    """The id of the paused call. It is also the approval id."""
    tool_name: str


class _State(BaseModel):
    """The JSON in ``runtime_state``.

    ``conversation`` names the item list of the session. ``item_count`` is the number of items
    that the next run keeps: a failed run keeps the count of the run before it, so the next run
    drops the items of the failed run. ``paused_count`` is the number of items when the run
    paused. ``run_state`` is the SDK ``RunState`` of a paused run. ``window`` is the number of
    items at the start of the list that a manual compaction gave: the answer of
    ``/responses/compact`` must stay whole, so a ``compaction`` item in it does not cut the
    items before it. ``repair`` is true when a run failed before it gave an output to the
    calls of an unanswered approval, so the next run must give them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime: Literal["openai_agents_sdk"] = "openai_agents_sdk"
    version: Literal[1] = 1
    conversation: str
    run_id: str
    item_count: int = Field(default=0, ge=0)
    paused_count: int | None = Field(default=None, ge=0)
    usage: Usage = Usage()
    elapsed_s: float = Field(default=0.0, ge=0)
    pending: _PendingCall | None = None
    run_state: dict[str, Any] | None = None
    window: int = Field(default=0, ge=0)
    repair: bool = False

    def dump(self) -> bytes:
        return self.model_dump_json().encode()

    @classmethod
    def load(cls, raw: bytes | None) -> _State | None:
        if raw is None:
            return None
        return cls.model_validate_json(raw)


# ---------------------------------------------------------------------------------------------
# The session store of the SDK
# ---------------------------------------------------------------------------------------------


def _plain(item: Any) -> dict[str, Any]:
    """An SDK input item as a JSON object."""
    value = json.loads(json.dumps(item, default=_json_default))
    if not isinstance(value, dict):
        raise TypeError("a session item must be a JSON object")
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_unset=True, warnings=False)
    raise TypeError(f"not JSON: {type(value).__name__}")


class _Store(SessionABC):
    """The SDK ``Session`` over a sama ``SessionItems`` store."""

    def __init__(self, items: SessionItems, scope: RunScope, conversation: str) -> None:
        self.session_id = conversation
        self._items = items
        self._scope = scope

    async def _call(self, op: str, **fields: Any) -> Any:
        request = SessionItemsRequest(op=op, conversation=self.session_id, **fields)  # type: ignore[arg-type]
        try:
            return await self._items.call(self._scope, request)
        except SessionItemsError as error:
            raise StoreFailure(str(error) or "the store refused the operation") from error
        except Exception as error:
            raise StoreFailure(_store_text(error), retryable=True) from error

    async def get_items(self, limit: int | None = None) -> list[Any]:
        return list((await self._call("get", limit=limit)).items)

    async def add_items(self, items: list[Any]) -> None:
        plain = [_plain(item) for item in items]
        for start in range(0, len(plain), MAX_WIRE_ITEMS):
            await self._call("add", items=plain[start : start + MAX_WIRE_ITEMS])

    async def pop_item(self) -> Any:
        answer = await self._call("pop")
        return answer.items[0] if answer.items else None

    async def clear_session(self) -> None:
        await self._call("clear")

    async def count(self) -> int:
        return int((await self._call("get", limit=0)).count)

    async def trim(self, count: int) -> int:
        return int((await self._call("trim", count=count)).count)

    async def replace(self, items: Sequence[dict[str, Any]]) -> int:
        if len(items) > MAX_WIRE_ITEMS:
            raise ValueError("too many items for one replace")
        return int((await self._call("replace", items=list(items))).count)


# ---------------------------------------------------------------------------------------------
# The tool gate of one run
# ---------------------------------------------------------------------------------------------


def sama_arguments(sdk_name: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """The arguments that sama sees for a call. The shell call becomes a ``Bash`` call with a
    ``command``, so one gate policy fits both runtimes."""
    if sdk_name == SHELL_TOOL:
        arguments: dict[str, Any] = {"command": str(params.get("cmd", ""))}
        if params.get("workdir"):
            arguments["workdir"] = str(params["workdir"])
        return arguments
    return dict(params)


def _tool_name(tool: FunctionTool) -> str:
    """The sama name of a tool: ``mcp__<server>__<tool>`` for an MCP tool, as in the Claude
    adapter, and ``Bash`` for the shell tool."""
    origin = get_function_tool_origin(tool)
    if origin is not None and origin.type is ToolOriginType.MCP and origin.mcp_server_name:
        return f"mcp__{origin.mcp_server_name}__{tool.name}"
    if tool.name == SHELL_TOOL:
        return SAMA_SHELL_TOOL
    return tool.name


@dataclass
class _RunGate:
    """Asks the sama gate for each call of one run, and keeps what the events need."""

    gate: ToolGate
    scope: RunScope
    names: dict[str, str] = field(default_factory=dict)
    """SDK tool name to sama tool name."""
    denied: dict[str, str] = field(default_factory=dict)
    """Call id to the reason of the deny."""
    ask_reasons: dict[str, str] = field(default_factory=dict)
    rejected: set[str] = field(default_factory=set)
    subagents: dict[str, str] = field(default_factory=dict)
    """SDK tool name of a sub-agent to the name of the sub-agent."""
    subagent_calls: dict[str, str] = field(default_factory=dict)
    """Call id of a sub-agent call to the name of the sub-agent."""
    shell: FunctionTool | None = None
    """The shell tool of the main agent. A sub-agent with ``Bash`` gets a copy of it."""

    def sama_name(self, sdk_name: str) -> str:
        if sdk_name in self.subagents:
            return SAMA_SUBAGENT_TOOL
        return self.names.get(sdk_name, SAMA_SHELL_TOOL if sdk_name == SHELL_TOOL else sdk_name)

    def sama_arguments(self, sdk_name: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """The arguments that sama sees. A sub-agent call has the ``subagent_type`` and the
        ``prompt``, as the ``Agent`` call of the Claude CLI."""
        subagent = self.subagents.get(sdk_name)
        if subagent is not None:
            return {"subagent_type": subagent, "prompt": str(params.get("input", ""))}
        return sama_arguments(sdk_name, params)

    def wrap(self, tool: FunctionTool, *, child: bool = False) -> None:
        sdk_name = tool.name
        if sdk_name not in self.subagents:
            self.names[sdk_name] = _tool_name(tool)

        async def needs_approval(
            _context: RunContextWrapper[Any], params: dict[str, Any], call_id: str
        ) -> bool:
            return await self.check(sdk_name, params, call_id, child=child)

        tool.needs_approval = needs_approval
        tool.tool_input_guardrails = [
            *(g for g in tool.tool_input_guardrails or [] if g.name != "sama_gate"),
            ToolInputGuardrail(guardrail_function=self.guardrail, name="sama_gate"),
        ]
        tool._sama_gated = True  # type: ignore[attr-defined]

    async def check(
        self, sdk_name: str, params: Mapping[str, Any], call_id: str, *, child: bool = False
    ) -> bool:
        name = self.sama_name(sdk_name)
        arguments = self.sama_arguments(sdk_name, params)
        try:
            decision = await self.gate.check(
                self.scope, name, arguments, call_id=call_id, child=child
            )
        except Exception as exc:
            decision = GateDecision("deny", f"the tool gate failed: {type(exc).__name__}")
        if decision.action == "ask" and child:
            # A sub-agent cannot pause the run. The gate maps ``ask`` to ``deny`` for a child
            # call itself. This stays for a gate that does not.
            decision = GateDecision("deny", decision.reason or _CHILD_ASK)
        if decision.action == "ask":
            self.ask_reasons[call_id] = decision.reason
            return True
        if decision.action == "deny":
            self.denied[call_id] = decision.reason or "sama tool gate: deny"
        return False

    def guardrail(self, data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        call_id = data.context.tool_call_id
        reason = self.denied.get(call_id)
        if reason is None:
            return ToolGuardrailFunctionOutput.allow()
        name = self.sama_name(data.context.tool_name)
        return ToolGuardrailFunctionOutput.reject_content(
            f"The tool policy denied the {name} tool call. Reason: {reason}"
        )


@dataclass
class _GatedAgent(SandboxAgent[Any]):
    """A ``SandboxAgent`` whose tools all ask the sama gate. The SDK makes new MCP tool objects
    for each turn, so the wrap happens in ``get_all_tools``."""

    sama_gate: _RunGate | None = None

    async def get_all_tools(self, run_context: RunContextWrapper[Any]) -> list[Tool]:
        tools = await super().get_all_tools(run_context)
        gate = self.sama_gate
        if gate is not None:
            for tool in tools:
                if isinstance(tool, FunctionTool) and not getattr(tool, "_sama_gated", False):
                    gate.wrap(tool)
                if isinstance(tool, FunctionTool) and tool.name == SHELL_TOOL:
                    gate.shell = tool
        return tools


@dataclass
class _SubAgent(Agent[Any]):
    """A sub-agent of the run (Sections 13.1 and 13.3). It has only the tools of its
    definition: the sama MCP tools that the definition names, and the shell tool when it names
    ``Bash``. Each call asks the gate as a child call."""

    sama_gate: _RunGate | None = None
    allowed: frozenset[str] = frozenset()
    """The sama names of the tools of the sub-agent."""

    async def get_all_tools(self, run_context: RunContextWrapper[Any]) -> list[Tool]:
        gate = self.sama_gate
        if gate is None:
            return []
        found = await super().get_all_tools(run_context)
        if SAMA_SHELL_TOOL in self.allowed and gate.shell is not None:
            found.append(gate.shell)
        tools: list[Tool] = []
        for tool in found:
            if not isinstance(tool, FunctionTool) or _tool_name(tool) not in self.allowed:
                continue
            # A copy: the main agent can hold the same object with its own gate hook.
            own = copy.copy(tool)
            gate.wrap(own, child=True)
            tools.append(own)
        return tools


def subagent_tool_name(name: str) -> str:
    """The SDK name of the tool of a sub-agent. A sub-agent name has letters, digits, and
    hyphens, which a function name can have."""
    return f"{SUBAGENT_TOOL_PREFIX}{name}"


def _allowed(definition: SubagentDefinition) -> frozenset[str]:
    return frozenset(
        tool if tool == SAMA_SHELL_TOOL else f"{SAMA_MCP_PREFIX}{tool}" for tool in definition.tools
    )


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def sama_usage(usage: SdkUsage | Any) -> Usage:
    """The usage of the SDK or of a ``/responses/compact`` answer. Both count the cached
    tokens in ``input_tokens``. Sama counts them apart, as the model proxy does
    (``openai_counts``). The cost stays zero: the proxy record is the source of truth for
    cost."""
    if usage is None:
        return Usage()
    total = int(getattr(usage, "input_tokens", 0) or 0)
    details = getattr(usage, "input_tokens_details", None)
    cached = int(getattr(details, "cached_tokens", 0) or 0)
    return Usage(
        input_tokens=max(0, total - cached),
        output_tokens=max(0, int(getattr(usage, "output_tokens", 0) or 0)),
        cache_read_tokens=max(0, cached),
    )


def _at_least(usage: Usage, floor: Usage) -> Usage:
    """Field by field, the larger value. The usage of a stream never goes down."""
    return Usage(
        input_tokens=max(usage.input_tokens, floor.input_tokens),
        output_tokens=max(usage.output_tokens, floor.output_tokens),
        cache_read_tokens=max(usage.cache_read_tokens, floor.cache_read_tokens),
        cache_write_tokens=max(usage.cache_write_tokens, floor.cache_write_tokens),
        cost_usd=max(usage.cost_usd, floor.cost_usd),
    )


def _preview(content: Any) -> str:
    text = content if isinstance(content, str) else json.dumps(content, default=str)
    return output_preview(text, _PREVIEW_CHARS)


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        value = json.loads(raw) if isinstance(raw, str) and raw else {}
    except ValueError:
        return {"raw": str(raw)}
    return value if isinstance(value, dict) else {"value": value}


def _field(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, Mapping) else getattr(item, name, None)


def reject_text(tool_name: str, reason: str | None) -> str:
    text = f"The user rejected the {tool_name} tool call. Do not run it again."
    if reason:
        text += f" Reason from the user: {reason}"
    return text + " Continue without it."


def compact_focus(text: str) -> str | None:
    """The focus of a ``/compact [focus]`` input, or None for any other input."""
    stripped = text.strip()
    if stripped == COMPACT_COMMAND or stripped.startswith(COMPACT_COMMAND + " "):
        return stripped[len(COMPACT_COMMAND) :].strip()
    return None


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """``exc`` and its causes, each one time."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


class StoreFailure(Exception):
    """An operation of the item store failed. The message has no secret."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _store_text(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    return type(error).__name__


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [str(_field(part, "text") or "") for part in content]
        return "\n".join(part for part in parts if part)
    return ""


def archive_entries(items: Sequence[Mapping[str, Any]]) -> tuple[list[ArchiveEntry], int]:
    """The archive entries of Responses items. Reasoning and compaction items have no text that
    a person can read, so they are left out. An entry over the limits counts as omitted."""
    entries: list[ArchiveEntry] = []
    omitted = 0
    for item in items:
        kind = item.get("type")
        role = item.get("role")
        fields: dict[str, Any] | None = None
        if kind == "function_call":
            name = str(item.get("name", ""))
            fields = {
                "role": "assistant",
                "tool_name": SAMA_SHELL_TOOL if name == SHELL_TOOL else name,
                "tool_input": _arguments(item.get("arguments")),
            }
        elif kind == "function_call_output":
            output = item.get("output")
            fields = {"role": "tool", "text": _text_of(output) or json.dumps(output, default=str)}
        elif role in {"user", "assistant", "system", "developer"} and kind in {None, "message"}:
            text = _text_of(item.get("content"))
            if text:
                fields = {"role": "system" if role == "developer" else role, "text": text}
        if fields is None:
            continue
        try:
            entries.append(ArchiveEntry(**fields))
        except ValueError:
            omitted += 1
    return entries, omitted


def _estimate_tokens(items: Sequence[Mapping[str, Any]]) -> int:
    return len(json.dumps(list(items), default=str)) // 4


def _last_compaction(items: Sequence[Any]) -> int | None:
    for index in range(len(items) - 1, -1, -1):
        if _field(items[index], "type") == "compaction":
            return index
    return None


def _load(config: RunConfig, raw: bytes | None) -> tuple[_State | None, str | None]:
    """The state of a request, or the problem of the request."""
    if config.runtime is not RuntimeKind.OPENAI_AGENTS_SDK:
        return None, f"config is for runtime {config.runtime}, not openai_agents_sdk"
    try:
        return _State.load(raw), None
    except ValidationError:
        return None, "runtime_state is not valid for the openai runtime"


def _kept_count(previous: _State | None) -> int:
    """The item count that a new run starts from. After an unanswered approval, the items of
    the paused run up to the pause stay."""
    if previous is None:
        return 0
    if previous.pending is not None and previous.paused_count is not None:
        return previous.paused_count
    return previous.item_count


def _needs_repair(previous: _State | None) -> bool:
    """Whether the paused calls of an unanswered approval can have no output."""
    return previous is not None and (previous.pending is not None or previous.repair)


class _WindowCompaction(Compaction):
    """The SDK ``Compaction`` cuts the context before the last ``compaction`` item. The first
    ``floor`` items are the answer of a manual compaction, which must stay whole, so a
    ``compaction`` item in them does not cut the context. A ``compaction`` item of a
    server-side compaction comes after them, and it cuts the context as before."""

    floor: int = 0

    def process_context(self, context: list[TResponseInputItem]) -> list[TResponseInputItem]:
        index = _last_compaction(context)
        if index is None or index < self.floor:
            return context
        return context[index:]


def _invalid(message: str) -> RunFailed:
    return RunFailed(code=FailureCode.INVALID_REQUEST, message=message)


def _cancelled(usage: Usage) -> RunFailed:
    return RunFailed(code=FailureCode.CANCELLED, message="cancelled", usage=usage)


# ---------------------------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------------------------


class _Done:
    """Queue marker: the pump stopped."""


@dataclass
class _Job:
    """What one stream does, and what it learned so far."""

    scope: RunScope
    config: RunConfig
    budget: Budget
    conversation: str
    base_count: int
    """The item count that a failure of this run goes back to."""
    input: str | None = None
    compact: str | None = None
    """The focus of a manual compaction."""
    resume: _State | None = None
    decision: ApprovalDecision | None = None
    reason: str | None = None
    start_usage: Usage = field(default_factory=Usage)
    extra_usage: Usage = field(default_factory=Usage)
    """Usage that the SDK does not count (a manual compaction)."""
    elapsed_before_s: float = 0.0
    started_at: float = 0.0
    result: RunResultStreaming | None = None
    window: int = 0
    """``_State.window`` of this run."""
    repair: bool = False
    """``_State.repair`` of a failure of this run. It is false after ``_prepare``."""

    @property
    def usage(self) -> Usage:
        if self.result is None:
            return _at_least(self.extra_usage, self.start_usage)
        sdk = sama_usage(self.result.context_wrapper.usage)
        return _at_least(sdk + self.extra_usage, self.start_usage)

    def elapsed_s(self) -> float:
        if not self.started_at:
            return self.elapsed_before_s
        return self.elapsed_before_s + asyncio.get_running_loop().time() - self.started_at

    def state(self, *, item_count: int, pending: _PendingCall | None = None, **extra: Any) -> bytes:
        return _State(
            conversation=self.conversation,
            run_id=self.scope.run_id,
            item_count=item_count,
            usage=self.usage,
            elapsed_s=self.elapsed_s(),
            pending=pending,
            window=self.window,
            repair=self.repair,
            **extra,
        ).dump()

    def failure_state(self) -> bytes:
        return self.state(item_count=self.base_count)


@dataclass(slots=True)
class _ActiveRun:
    """One active stream. ``send`` queues an event and drops events after the terminal one."""

    job: _Job
    queue: asyncio.Queue[RunEvent | _Done] = field(default_factory=asyncio.Queue)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    pump: asyncio.Task[None] | None = None
    sent_terminal: bool = False
    completed: bool = False
    """The terminal event is ``RunCompleted``. Only then does a keeper keep the connections."""

    def send(self, event: RunEvent) -> None:
        if self.sent_terminal:
            return
        if isinstance(event, RunFailed) and event.runtime_state is None:
            event = event.model_copy(update={"runtime_state": self.job.failure_state()})
        self.sent_terminal = is_terminal(event)
        self.completed = isinstance(event, RunCompleted)
        self.queue.put_nowait(event)


class _Failure(Exception):
    def __init__(self, event: RunFailed) -> None:
        super().__init__(event.message)
        self.event = event


# ---------------------------------------------------------------------------------------------
# The runtime
# ---------------------------------------------------------------------------------------------


class OpenAIRuntime:
    """The second runtime. The same rules of the contract as ``ClaudeRuntime``:

    - One stream at a time per run id. The run id is freed before the terminal event.
    - An approval resumes once, and only for its run.
    - ``cancel`` stops the SDK run at once. If the pump does not stop in ``cancel_grace_s``,
      the adapter ends the stream. ``cancel`` also makes the approvals of a paused run invalid.
    """

    def __init__(
        self,
        *,
        items: SessionItems,
        gate: ToolGate | None = None,
        settings: OpenAISettings | None = None,
        archiver: Archiver | None = None,
    ) -> None:
        self._items = items
        self._gate = gate or AllowAllGate()
        self._settings = settings or OpenAISettings()
        self._archiver = archiver
        self._client: AsyncOpenAI | None = None
        self._active: dict[str, _ActiveRun] = {}
        self._paused = BoundedSet()
        self._used_approvals = BoundedSet()
        self._cancelled_runs = BoundedSet()
        self._pumps: set[asyncio.Task[None]] = set()
        self._closing: set[asyncio.Task[None]] = set()

    def __repr__(self) -> str:
        return f"OpenAIRuntime(settings={self._settings!r})"

    @property
    def kind(self) -> RuntimeKind:
        return RuntimeKind.OPENAI_AGENTS_SDK

    def capabilities(self) -> Capabilities:
        return Capabilities(
            streaming=True,
            native_subagents=True,
            native_compaction=True,
            native_skills=False,
            native_file_checkpoints=False,
            resumable_sessions=True,
            mcp_client=True,
            manual_compaction=True,
            compaction_archive=True,
        )

    def _openai(self) -> AsyncOpenAI:
        keeper = self._settings.keeper
        if keeper is not None:
            return keeper.client(self._settings)
        if self._client is None:
            self._client = _model_client(self._settings)
        return self._client

    def _mark(self, name: str) -> None:
        if self._settings.mark is not None:
            self._settings.mark(name)

    async def aclose(self) -> None:
        for task in list(self._pumps):
            task.cancel()
        for task in list(self._pumps):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        while self._closing:
            await asyncio.wait(set(self._closing))
        if self._client is not None:
            await self._client.close()
            self._client = None

    # -- the protocol ----------------------------------------------------------------------

    async def run(self, request: RunRequest) -> AsyncIterator[RunEvent]:
        run_id = request.scope.run_id
        previous, problem = _load(request.config, request.runtime_state)
        if problem is None and (run_id in self._active or run_id in self._paused):
            problem = f"run {run_id} is already active"
        focus = request.compact.focus if request.compact is not None else None
        if focus is None:
            focus = compact_focus(request.input)
        if problem is None and focus is not None and previous is None:
            problem = "there is nothing to compact"
        if problem is not None:
            yield _invalid(problem)
            return
        job = _Job(
            scope=request.scope,
            config=request.config,
            budget=request.budget,
            conversation=previous.conversation if previous else _new_conversation(run_id),
            # A failure before ``_prepare`` ends must keep the items of the run before.
            base_count=_kept_count(previous),
            window=previous.window if previous else 0,
            repair=_needs_repair(previous),
            input=None if focus is not None else request.input,
            compact=focus,
        )
        stream = self._stream(job, previous)
        async with contextlib.aclosing(stream):
            async for event in stream:
                yield event

    async def resume(self, request: ResumeRequest) -> AsyncIterator[RunEvent]:
        state, problem = _load(request.config, request.runtime_state)
        # No await from here to the registration of the run in ``_stream``: the checks and the
        # use of the approval are one step.
        if problem is None:
            problem = self._check_resume(request, state)
        if problem is not None or state is None or state.pending is None:
            yield _invalid(problem or "runtime_state has no pending approval")
            return
        self._used_approvals.add(request.approval_id)
        self._paused.discard(request.scope.run_id)
        job = _Job(
            scope=request.scope,
            config=request.config,
            budget=request.budget,
            conversation=state.conversation,
            base_count=state.item_count,
            window=state.window,
            resume=state,
            decision=request.decision,
            reason=request.reason,
            start_usage=state.usage,
            elapsed_before_s=state.elapsed_s,
        )
        stream = self._stream(job, None)
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
        result = active.job.result
        if result is not None:
            with contextlib.suppress(Exception):
                result.cancel(mode="immediate")
        pump = active.pump
        if pump is None or pump.done():
            return
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(pump), timeout=self._settings.cancel_grace_s)
        if not pump.done():
            # The SDK did not stop in time. End the stream now; the pump cleans up after.
            active.send(_cancelled(active.job.usage))
            pump.cancel()

    # -- checks ----------------------------------------------------------------------------

    def _check_resume(self, request: ResumeRequest, state: _State | None) -> str | None:
        run_id = request.scope.run_id
        if state is None or state.pending is None or state.run_state is None:
            return "runtime_state has no pending approval"
        if state.pending.call_id != request.approval_id:
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

    async def _stream(self, job: _Job, previous: _State | None) -> AsyncGenerator[RunEvent]:
        run_id = job.scope.run_id
        active = _ActiveRun(job=job)
        self._active[run_id] = active
        pump = asyncio.create_task(self._pump(active, previous))
        active.pump = pump
        self._pumps.add(pump)
        pump.add_done_callback(self._pumps.discard)
        try:
            while True:
                item = await active.queue.get()
                if isinstance(item, _Done):
                    self._release(run_id, active)
                    yield RunFailed(
                        code=FailureCode.INTERNAL,
                        message="the stream ended without a terminal event",
                        usage=job.usage,
                        runtime_state=job.failure_state(),
                    )
                    return
                if is_terminal(item):
                    self._release(run_id, active)
                    if isinstance(item, ApprovalRequired):
                        if active.cancelled.is_set():
                            item = _cancelled(item.usage).model_copy(
                                update={"runtime_state": job.failure_state()}
                            )
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

    async def _pump(self, active: _ActiveRun, previous: _State | None) -> None:
        """Owns the SDK run, the MCP connections, and the sandbox session. Every SDK call
        happens in this task."""
        job = active.job
        job.started_at = asyncio.get_running_loop().time()
        wall_time: float | None = None
        servers: _ServerSet | None = None
        if job.budget.max_wall_time_s is not None:
            wall_time = max(0.0, job.budget.max_wall_time_s - job.elapsed_before_s)
        try:
            async with asyncio.timeout(wall_time):
                if wall_time == 0.0:
                    raise TimeoutError
                store = _Store(self._items, job.scope, job.conversation)
                if job.compact is None:
                    # The servers connect while the items load: both wait for the network.
                    servers = await self._open_servers(job)
                if job.resume is None:
                    job.base_count = await self._prepare(store, previous)
                    job.repair = False
                    self._mark("items_ready")
                if active.cancelled.is_set():
                    active.send(_cancelled(job.usage))
                elif job.compact is not None:
                    await self._compact(active, store, job.compact)
                else:
                    assert servers is not None  # noqa: S101 -- set above for a run with no compact
                    await self._agent_run(active, store, servers)
        except TimeoutError:
            active.send(
                RunFailed(
                    code=FailureCode.TIMEOUT,
                    message="the run reached max_wall_time_s",
                    usage=job.usage,
                )
            )
        except asyncio.CancelledError:
            active.send(_cancelled(job.usage))
            raise
        except _Failure as failure:
            active.send(failure.event.model_copy(update={"usage": job.usage}))
        except Exception as exc:
            active.send(self._failure(exc, job))
        finally:
            try:
                await self._end_servers(servers, keep=active.completed)
            finally:
                active.queue.put_nowait(_Done())

    async def _open_servers(self, job: _Job) -> _ServerSet:
        """The MCP servers of the run: the kept ones, or new ones that start to connect now."""
        settings = self._settings
        relay = settings.relay
        key = self._servers_key(job.config)
        if settings.keeper is not None:
            kept = await settings.keeper.take(key)
            if kept is not None:
                self._mark("mcp_kept")
                return kept
        return _ServerSet(key, [_Owned(server) for server in _mcp_servers(job.config, relay)])

    def _servers_key(self, config: RunConfig) -> str:
        relay = self._settings.relay
        return hashlib.sha256(
            f"{config.fingerprint()}\0{relay.secret if relay else ''}".encode()
        ).hexdigest()

    async def prepare(self, request: RunRequest | ResumeRequest) -> None:
        """Before the token of the run is active: closes the kept connections if this run
        cannot use them (the runner calls it)."""
        keeper = self._settings.keeper
        if keeper is not None and keeper.holds:
            await keeper.retire_unless(self._servers_key(request.config))

    async def _end_servers(self, servers: _ServerSet | None, *, keep: bool) -> None:
        """Keeps the servers after a run that completed, else closes them."""
        if servers is None:
            return
        keeper = self._settings.keeper
        if keeper is not None and keep:
            keeper.put(servers)
            return
        # A task closes them. A cancel of this pump (the caller has its terminal event and
        # stops the pump) must not leave a connection half closed. ``aclose`` waits for it.
        task = asyncio.create_task(servers.close())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)
        await asyncio.wait({task})

    def _failure(self, exc: Exception, job: _Job) -> RunFailed:
        if isinstance(exc, MaxTurnsExceeded):
            return RunFailed(
                code=FailureCode.BUDGET_EXCEEDED,
                message="the run reached max_turns",
                usage=job.usage,
            )
        chain = list(_chain(exc))
        store = next((e for e in chain if isinstance(e, StoreFailure)), None)
        if store is not None:
            log.warning("the session items failed run=%s: %s", job.scope.run_id, store)
            return RunFailed(
                code=FailureCode.INTERNAL,
                message=f"the session items failed: {store}",
                # A refusal of the store (for example, too many items) does not go away.
                retryable=store.retryable,
                usage=job.usage,
            )
        status = next(
            (s for e in chain if isinstance(s := getattr(e, "status_code", None), int)), None
        )
        if status == _PROXY_BUDGET_STATUS:
            return RunFailed(
                code=FailureCode.BUDGET_EXCEEDED,
                message=(
                    "the model proxy refused the call: a run budget or a model quota of the plan"
                    " is used up. An org admin can see the quotas on the Billing page."
                ),
                usage=job.usage,
            )
        if status is not None:
            return RunFailed(
                code=FailureCode.MODEL_ERROR,
                message=f"the model call failed with HTTP {status}",
                retryable=status in _RETRYABLE_STATUS,
                usage=job.usage,
            )
        log.warning("the openai run failed run=%s: %s", job.scope.run_id, type(exc).__name__)
        return RunFailed(
            code=FailureCode.INTERNAL,
            message=f"unexpected error: {type(exc).__name__}",
            usage=job.usage,
        )

    async def _prepare(self, store: _Store, previous: _State | None) -> int:
        """Makes the items ready for a new run, and returns the count that a failure of the
        run goes back to. The items of a run that did not complete go away. After an
        unanswered approval, the paused call gets an output, because the Responses API needs
        an output for each call."""
        count = await store.trim(_kept_count(previous))
        if not _needs_repair(previous):
            return count
        items = await store.get_items()
        answered = {i.get("call_id") for i in items if i.get("type") == "function_call_output"}
        dangling = [
            {"type": "function_call_output", "call_id": i["call_id"], "output": _NOT_RUN_OUTPUT}
            for i in items
            if i.get("type") == "function_call" and i.get("call_id") not in answered
        ]
        if dangling:
            await store.add_items(dangling)
            count += len(dangling)
        return count

    def _agent(
        self, job: _Job, gate: _RunGate, servers: Sequence[MCPServer], active: _ActiveRun
    ) -> _GatedAgent:
        return _GatedAgent(
            name="sama",
            instructions=job.config.render_prefix() or None,
            model=OpenAIResponsesModel(model=job.config.model, openai_client=self._openai()),
            model_settings=_model_settings(job.config),
            mcp_servers=list(servers),
            tools=[
                self._subagent_tool(definition, job, gate, servers, active)
                for definition in job.config.subagents
            ],
            capabilities=[
                Shell(configure_tools=_shell_tools),
                _WindowCompaction(floor=job.window),
            ],
            sama_gate=gate,
        )

    def _subagent_tool(
        self,
        definition: SubagentDefinition,
        job: _Job,
        gate: _RunGate,
        servers: Sequence[MCPServer],
        active: _ActiveRun,
    ) -> FunctionTool:
        """One sub-agent as a tool of the main agent (agents as tools)."""
        sdk_name = subagent_tool_name(definition.name)
        gate.subagents[sdk_name] = definition.name
        agent = _SubAgent(
            name=definition.name,
            instructions=definition.prompt,
            model=OpenAIResponsesModel(
                model=definition.model or job.config.model, openai_client=self._openai()
            ),
            model_settings=ModelSettings(parallel_tool_calls=False),
            mcp_servers=list(servers),
            sama_gate=gate,
            allowed=_allowed(definition),
        )

        def on_stream(event: Any) -> None:
            text = _subagent_text(event.get("event"))
            if text is None:
                return
            call = event.get("tool_call")
            call_id = _field(call, "call_id") if call is not None else None
            active.send(
                SubagentEvent(
                    subagent=definition.name,
                    text=bound_subagent_text(text),
                    call_id=None if call_id is None else str(call_id),
                    usage=job.usage,
                )
            )

        return agent.as_tool(
            tool_name=sdk_name,
            tool_description=definition.description,
            on_stream=on_stream,
            run_config=SdkRunConfig(tracing_disabled=True),
            max_turns=definition.max_turns or DEFAULT_SUBAGENT_TURNS,
        )

    async def _agent_run(self, active: _ActiveRun, store: _Store, owned: _ServerSet) -> None:
        job = active.job
        gate = _RunGate(self._gate, job.scope)
        client = UnixLocalSandboxClient()
        sandbox: Any = None
        async with contextlib.AsyncExitStack() as stack:
            await owned.ready()
            self._mark("mcp_ready")
            servers = owned.servers
            manifest = Manifest(root=self._settings.cwd) if self._settings.cwd else Manifest()
            sandbox = await client.create(manifest=manifest, snapshot=NoopSnapshotSpec())
            stack.push_async_callback(_cleanup, _close_sandbox(client, sandbox))
            await sandbox.start()
            self._mark("sandbox_ready")
            agent = self._agent(job, gate, servers, active)
            run_config = SdkRunConfig(
                sandbox=SandboxRunConfig(session=sandbox), tracing_disabled=True
            )
            start: str | RunState[Any] = job.input or ""
            if job.resume is not None:
                start = await self._resumed_state(job, agent, store, gate)
            max_turns = job.budget.max_turns or DEFAULT_MAX_TURNS
            result = Runner.run_streamed(
                agent, start, session=store, run_config=run_config, max_turns=max_turns
            )
            job.result = result
            try:
                if active.cancelled.is_set():
                    result.cancel(mode="immediate")
                await self._events(active, result, gate)
            finally:
                if not result.is_complete:
                    result.cancel(mode="immediate")
            if active.cancelled.is_set():
                active.send(_cancelled(job.usage))
                return
            await self._finish(active, store, result, gate)

    async def _resumed_state(
        self, job: _Job, agent: _GatedAgent, store: _Store, gate: _RunGate
    ) -> RunState[Any]:
        state = job.resume
        # ``_check_resume`` checked the state before the run started.
        assert state is not None  # noqa: S101
        assert state.pending is not None  # noqa: S101
        assert state.run_state is not None  # noqa: S101
        if state.paused_count is not None:
            await store.trim(state.paused_count)
        run_state: RunState[Any] = await RunState.from_json(agent, state.run_state)
        item = _find_interruption(run_state.get_interruptions(), state.pending.call_id)
        if item is None:
            raise _Failure(_invalid("the run state has no pending call for the approval"))
        if job.decision is ApprovalDecision.APPROVE:
            run_state.approve(item)
        else:
            gate.rejected.add(state.pending.call_id)
            run_state.reject(
                item, rejection_message=reject_text(state.pending.tool_name, job.reason)
            )
        return run_state

    async def _events(self, active: _ActiveRun, result: RunResultStreaming, gate: _RunGate) -> None:
        job = active.job
        async for event in result.stream_events():
            if event.type == "raw_response_event":
                data = event.data
                if data.type == "response.output_text.delta" and data.delta:
                    active.send(TextDelta(text=data.delta, usage=job.usage))
                elif data.type == "response.created":
                    # The model started a turn: a client shows the phase before the first text.
                    active.send(RunPhase(phase=RunPhaseName.THINKING, usage=job.usage))
            elif event.type == "run_item_stream_event":
                if event.name == "tool_called":
                    raw = event.item.raw_item
                    call_id = _field(raw, "call_id")
                    sdk_name = _field(raw, "name")
                    if not call_id or not sdk_name:
                        continue
                    subagent = gate.subagents.get(str(sdk_name))
                    if subagent is not None:
                        gate.subagent_calls[str(call_id)] = subagent
                    active.send(
                        ToolCallStarted(
                            call_id=str(call_id),
                            name=gate.sama_name(str(sdk_name)),
                            arguments=gate.sama_arguments(
                                str(sdk_name), _arguments(_field(raw, "arguments"))
                            ),
                            usage=job.usage,
                        )
                    )
                elif event.name == "tool_output":
                    call_id = _field(event.item.raw_item, "call_id")
                    if not call_id:
                        continue
                    call_id = str(call_id)
                    subagent = gate.subagent_calls.pop(call_id, None)
                    if subagent is not None:
                        active.send(
                            SubagentEvent(
                                subagent=subagent,
                                # The whole output: the control plane redacts it and then
                                # cuts it, so a cut never splits a secret (Section 13.1, A9).
                                text=bound_subagent_text(
                                    f"done: {_text_of(getattr(event.item, 'output', ''))}"
                                ),
                                call_id=call_id,
                                done=True,
                                usage=job.usage,
                            )
                        )
                    active.send(
                        ToolCallFinished(
                            call_id=call_id,
                            is_error=call_id in gate.denied or call_id in gate.rejected,
                            output_preview=_preview(getattr(event.item, "output", "")),
                            usage=job.usage,
                        )
                    )

    async def _finish(
        self, active: _ActiveRun, store: _Store, result: RunResultStreaming, gate: _RunGate
    ) -> None:
        job = active.job
        interruptions = list(result.interruptions)
        if interruptions:
            item = interruptions[0]
            raw = item.raw_item
            call_id = str(_field(raw, "call_id") or "")
            sdk_name = str(item.tool_name or _field(raw, "name") or "")
            name = gate.sama_name(sdk_name)
            arguments = gate.sama_arguments(sdk_name, _arguments(_field(raw, "arguments")))
            pending = _PendingCall(call_id=call_id, tool_name=name)
            paused_count = await store.count()
            state = job.state(
                item_count=job.base_count,
                pending=pending,
                paused_count=paused_count,
                run_state=result.to_state().to_json(),
            )
            active.send(
                ApprovalRequired(
                    approval_id=call_id,
                    call_id=call_id,
                    tool_name=name,
                    arguments=arguments,
                    reason=gate.ask_reasons.get(call_id, ""),
                    runtime_state=state,
                    usage=job.usage,
                )
            )
            return
        compacted = await self._drop_compacted(job, store)
        if compacted is not None:
            active.send(compacted)
        count = await store.count()
        output = result.final_output
        active.send(
            RunCompleted(
                output="" if output is None else str(output),
                runtime_state=job.state(item_count=count),
                usage=job.usage,
            )
        )

    async def _drop_compacted(self, job: _Job, store: _Store) -> Compacted | None:
        """After a server-side compaction, the items before the compaction item are no longer
        part of the context. The adapter archives them and drops them from the store."""
        items = await store.get_items()
        index = _last_compaction(items)
        if index is None or index == 0 or index < job.window:
            return None
        before = _estimate_tokens(items)
        await self._archive(job.scope, "auto", items[:index])
        await store.replace(items[index:])
        job.window = 0
        return Compacted(
            tokens_before=before,
            tokens_after=_estimate_tokens(items[index:]),
            trigger="auto",
            usage=job.usage,
        )

    async def _compact(self, active: _ActiveRun, store: _Store, focus: str) -> None:
        job = active.job
        items = await store.get_items()
        if not items:
            raise _Failure(_invalid("there is nothing to compact"))
        # The store has only the context: each run drops the items before a compaction.
        context = items
        await self._archive(job.scope, "manual", context)
        instructions = job.config.render_prefix()
        if focus:
            instructions = f"{instructions}\n\nFocus of the summary: {focus}".strip()
        response = await self._openai().responses.compact(
            model=job.config.model,
            input=context,
            instructions=instructions or None,
        )
        job.extra_usage = job.extra_usage + sama_usage(response.usage)
        # The answer is the whole new context window, also the user messages before its
        # compaction item. It goes to the next call unchanged.
        kept = [_plain(item) for item in response.output]
        count = await store.replace(kept)
        job.window = len(kept)
        active.send(
            Compacted(
                tokens_before=_estimate_tokens(context),
                tokens_after=_estimate_tokens(kept),
                trigger="manual",
                usage=job.usage,
            )
        )
        active.send(
            RunCompleted(output="", runtime_state=job.state(item_count=count), usage=job.usage)
        )

    async def _archive(
        self, scope: RunScope, trigger: Literal["auto", "manual"], items: Sequence[dict[str, Any]]
    ) -> None:
        """Gives the items that a compaction drops to the archiver (Section 6.3). A failed
        archive does not stop the compaction."""
        if self._archiver is None:
            return
        try:
            entries, left_out = archive_entries(items)
            if entries:
                bounded, omitted = bound_for_wire(entries, left_out)
                await self._archiver.archive(
                    scope, TranscriptArchive(trigger=trigger, entries=bounded, omitted=omitted)
                )
        except Exception as exc:
            log.warning(
                "the transcript archive failed run=%s: %s", scope.run_id, type(exc).__name__
            )


def _subagent_text(event: Any) -> str | None:
    """One line of progress of a sub-agent from a stream event of its run, or ``None``: a
    tool call of the sub-agent, or its final message."""
    if getattr(event, "type", None) != "run_item_stream_event":
        return None
    item = getattr(event, "item", None)
    raw = getattr(item, "raw_item", None)
    if event.name == "tool_called":
        return f"calls {_field(raw, 'name') or 'a tool'}"
    if event.name == "message_output_created":
        text = _text_of(_field(raw, "content"))
        return text or None
    return None


def _new_conversation(run_id: str) -> str:
    return f"c-{uuid.uuid5(uuid.NAMESPACE_URL, run_id).hex}"


def _model_settings(config: RunConfig) -> ModelSettings:
    """The model settings of a run. A reasoning effort of the profile goes to the model; without
    one, the model keeps its default."""
    if config.reasoning_effort is None:
        return ModelSettings(parallel_tool_calls=False)
    return ModelSettings(
        parallel_tool_calls=False, reasoning=Reasoning(effort=config.reasoning_effort)
    )


def _shell_tools(toolset: Any) -> None:
    """Only ``exec_command``: ``write_stdin`` would let a call go on after the gate allowed
    it."""
    toolset.write_stdin = None


def _mcp_servers(config: RunConfig, relay: TokenRelay | None = None) -> list[MCPServer]:
    """The MCP clients of a run. With a relay, a client calls the relay with the local secret,
    and the relay adds the token of the active run (``sama.runtime.relay``)."""
    servers: list[MCPServer] = []
    for server in config.mcp_servers:
        headers: dict[str, str] = {}
        url = server.url
        if relay is not None:
            url = relay.mcp_url(server.name)
            headers["Authorization"] = f"Bearer {relay.secret}"
        elif server.bearer_token is not None:
            headers["Authorization"] = f"Bearer {server.bearer_token.get_secret_value()}"
        servers.append(
            MCPServerStreamableHttp(
                params={"url": url, "headers": headers},
                name=server.name,
                cache_tools_list=True,
                client_session_timeout_seconds=MCP_CALL_TIMEOUT_S,
            )
        )
    return servers


def _find_interruption(items: Sequence[ToolApprovalItem], call_id: str) -> ToolApprovalItem | None:
    for item in items:
        if str(_field(item.raw_item, "call_id") or "") == call_id:
            return item
    return None


async def _cleanup(close: Any) -> bool:
    """Runs one close step in the calling task, with a time limit. Returns whether it
    completed. A step that fails or runs too long is only logged."""
    try:
        async with asyncio.timeout(_CLEANUP_S):
            await close
    except Exception as exc:
        log.warning("an openai run cleanup step failed: %s", type(exc).__name__)
        return False
    return True


async def _close_sandbox(client: UnixLocalSandboxClient, sandbox: Any) -> None:
    try:
        await sandbox.shutdown()
    finally:
        await client.delete(sandbox)
