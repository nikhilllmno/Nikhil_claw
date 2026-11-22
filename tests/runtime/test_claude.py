"""Unit tests for the Claude Agent SDK adapter. A fake SDK client replaces the CLI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from decimal import Decimal
from typing import Any, cast

import pytest

pytest.importorskip("claude_agent_sdk")

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    CLIJSONDecodeError,
    HookInput,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import (
    DeferredToolUse,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from sama.runtime import (
    ApprovalDecision,
    ApprovalRequired,
    Budget,
    Compacted,
    FailureCode,
    ResumeRequest,
    RunCompleted,
    RunEvent,
    RunFailed,
    RunPhase,
    RunPhaseName,
    RunRequest,
    RunScope,
    RuntimeKind,
    SubagentEvent,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    Usage,
    validate_event_stream,
)
from sama.runtime.claude import (
    DROP_DEFERRED_ENV,
    TOOL_SEARCH_ENV,
    ClaudeClient,
    ClaudeRuntime,
    ClaudeSettings,
    GateDecision,
)
from tests.support import collect, make_config, make_scope

SESSION = "sdk-session-1"

# A step is a message to send, or a callable that gets the client and returns messages.
Step = Any


def result(
    *,
    stop_reason: str | None = "end_turn",
    subtype: str = "success",
    is_error: bool = False,
    text: str | None = "done",
    input_tokens: int = 10,
    output_tokens: int = 5,
    cost: float | None = 0.01,
    deferred: DeferredToolUse | None = None,
    terminal_reason: str | None = "completed",
    errors: list[str] | None = None,
    api_error_status: int | None = None,
    num_turns: int = 1,
    cache_read: int = 2,
    cache_write: int = 1,
    model_usage: dict[str, Any] | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=num_turns,
        session_id=SESSION,
        stop_reason=stop_reason,
        total_cost_usd=cost,
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
        },
        model_usage=model_usage,
        result=text,
        deferred_tool_use=deferred,
        terminal_reason=terminal_reason,
        errors=errors,
        api_error_status=api_error_status,
    )


def delta(text: str, parent: str | None = None) -> StreamEvent:
    return StreamEvent(
        uuid="u",
        session_id=SESSION,
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
        parent_tool_use_id=parent,
    )


def assistant(*blocks: Any, parent: str | None = None) -> AssistantMessage:
    return AssistantMessage(
        content=list(blocks), model="claude-haiku-4-5", parent_tool_use_id=parent
    )


def tool_result(
    call_id: str, content: str | list[dict[str, Any]] = "ok", is_error: bool = False
) -> UserMessage:
    return UserMessage(
        content=[ToolResultBlock(tool_use_id=call_id, content=content, is_error=is_error)]
    )


class FakeClient:
    """Replays one scripted CLI process. ``block`` makes it wait until ``interrupt``."""

    def __init__(
        self,
        options: ClaudeAgentOptions,
        steps: Sequence[Step],
        *,
        block: bool = False,
        honour_interrupt: bool = True,
        connect_error: Exception | None = None,
        disconnect_s: float = 0.0,
    ) -> None:
        self.options = options
        self.disconnect_s = disconnect_s
        self.steps = list(steps)
        self.block = block
        self.honour_interrupt = honour_interrupt
        self.connect_error = connect_error
        self.prompts: list[str] = []
        self.interrupted = asyncio.Event()
        self.disconnected = False
        self.hook_outputs: list[dict[str, Any]] = []
        self.permission_results: list[Any] = []

    async def connect(self, prompt: str | None = None) -> None:
        if self.connect_error is not None:
            raise self.connect_error

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)

    async def receive_messages(self) -> AsyncIterator[Any]:
        for step in self.steps:
            messages = await step(self) if callable(step) else [step]
            for message in messages:
                yield message
        if self.block:
            await self.interrupted.wait()
            if self.honour_interrupt:
                yield result(stop_reason=None, terminal_reason="aborted_streaming", text=None)
            else:
                await asyncio.Event().wait()

    async def interrupt(self) -> None:
        self.interrupted.set()

    async def disconnect(self) -> None:
        await asyncio.sleep(self.disconnect_s)
        self.disconnected = True


class Factory:
    """Gives one ``FakeClient`` per phase, in order."""

    def __init__(self, *phases: Sequence[Step], **client_kwargs: Any) -> None:
        self.phases = list(phases)
        self.client_kwargs = client_kwargs
        self.clients: list[FakeClient] = []

    def __call__(self, options: ClaudeAgentOptions) -> ClaudeClient:
        steps = self.phases[len(self.clients)]
        client = FakeClient(options, steps, **self.client_kwargs)
        self.clients.append(client)
        return client


class ScriptedGate:
    def __init__(self, decision: GateDecision | Exception) -> None:
        self.decision = decision
        self.calls: list[tuple[str, Mapping[str, Any]]] = []
        self.children: list[bool] = []

    async def check(
        self,
        scope: RunScope,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        call_id: str,
        child: bool = False,
    ) -> GateDecision:
        self.calls.append((tool_name, arguments))
        self.children.append(child)
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision


def call_hook(
    tool_name: str,
    call_id: str,
    *,
    subagent: bool = False,
    tool_input: Mapping[str, Any] | None = None,
) -> Callable[[FakeClient], Any]:
    """A step that runs the PreToolUse hook of the options, like the CLI does."""

    async def step(client: FakeClient) -> list[Any]:
        hooks = client.options.hooks or {}
        matcher = hooks["PreToolUse"][0]
        data: dict[str, Any] = {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": dict(tool_input) if tool_input is not None else {"to": "a@b.c"},
            "tool_use_id": call_id,
            "session_id": SESSION,
            "transcript_path": "",
            "cwd": "/",
        }
        if subagent:
            data["agent_id"] = "agent-1"
        output = await matcher.hooks[0](cast(HookInput, data), call_id, {"signal": None})
        client.hook_outputs.append(dict(output))
        return []

    return step


def call_can_use(tool_name: str, call_id: str) -> Callable[[FakeClient], Any]:
    """A step that asks the ``can_use_tool`` callback of the options, like the CLI does."""

    async def step(client: FakeClient) -> list[Any]:
        callback = client.options.can_use_tool
        assert callback is not None
        context = ToolPermissionContext(tool_use_id=call_id)
        client.permission_results.append(await callback(tool_name, {"to": "a@b.c"}, context))
        return []

    return step


def config() -> Any:
    return make_config(RuntimeKind.CLAUDE_AGENT_SDK)


def request(
    text: str = "hello",
    *,
    run_id: str = "run-1",
    state: bytes | None = None,
    budget: Budget | None = None,
) -> RunRequest:
    return RunRequest(
        scope=make_scope(run_id),
        input=text,
        config=config(),
        runtime_state=state,
        budget=budget or Budget(),
    )


def resume_request(
    paused: ApprovalRequired,
    decision: ApprovalDecision,
    *,
    run_id: str = "run-1",
    approval_id: str | None = None,
    reason: str | None = None,
    budget: Budget | None = None,
) -> ResumeRequest:
    return ResumeRequest(
        scope=make_scope(run_id),
        config=config(),
        approval_id=approval_id or paused.approval_id,
        decision=decision,
        reason=reason,
        budget=budget or Budget(),
        runtime_state=paused.runtime_state,
    )


def deferred_phase() -> list[Step]:
    return [
        assistant(ToolUseBlock(id="call-1", name="mcp__sama__gmail_send", input={"to": "a@b.c"})),
        call_hook("mcp__sama__gmail_send", "call-1"),
        result(
            stop_reason="tool_deferred",
            text=None,
            deferred=DeferredToolUse(
                id="call-1", name="mcp__sama__gmail_send", input={"to": "a@b.c"}
            ),
        ),
    ]


async def pause(gate_reason: str = "write action") -> tuple[ClaudeRuntime, ApprovalRequired]:
    runtime = ClaudeRuntime(
        gate=ScriptedGate(GateDecision("ask", gate_reason)),
        client_factory=Factory(deferred_phase()),
    )
    events = await collect(runtime.run(request()))
    paused = events[-1]
    assert isinstance(paused, ApprovalRequired)
    return runtime, paused


# -- options -----------------------------------------------------------------------------------


async def test_the_reasoning_effort_of_the_config_goes_to_the_options() -> None:
    plain = request()
    assert plain.config.reasoning_effort is None
    wanted = plain.model_copy(
        update={"config": plain.config.model_copy(update={"reasoning_effort": "high"})}
    )
    efforts = []
    for given in (plain, wanted):
        factory = Factory([result()])
        runtime = ClaudeRuntime(settings=ClaudeSettings(), client_factory=factory)
        await collect(runtime.run(given))
        efforts.append(factory.clients[0].options.effort)
    assert efforts == [None, "high"]


async def test_maps_the_request_to_options() -> None:
    factory = Factory([result()])
    settings = ClaudeSettings(cwd="/workspace", env={"ANTHROPIC_BASE_URL": "https://proxy.test"})
    runtime = ClaudeRuntime(settings=settings, client_factory=factory)
    budget = Budget(max_turns=7, max_cost_usd=Decimal("1.5"))
    await collect(runtime.run(request(budget=budget)))
    options = factory.clients[0].options
    assert options.model == "claude-sonnet-4-5"
    assert options.system_prompt == {
        "type": "preset",
        "preset": "claude_code",
        "append": config().render_prefix(),
        "exclude_dynamic_sections": True,
    }
    assert options.mcp_servers == {
        "sama": {
            "type": "http",
            "url": "https://mcp.sama.test/mcp",
            "headers": {"Authorization": "Bearer run-token-1"},
        }
    }
    assert options.strict_mcp_config is True
    assert options.setting_sources == []
    assert options.max_turns == 7
    assert options.max_budget_usd == 1.5
    assert options.include_partial_messages is True
    assert options.resume is None
    assert options.disallowed_tools == []
    assert options.cwd == "/workspace"
    # Tool search off: the CLI loads the sama MCP tools at the start (no ``ToolSearch`` call).
    assert options.env == {TOOL_SEARCH_ENV: "false", "ANTHROPIC_BASE_URL": "https://proxy.test"}
    assert factory.clients[0].prompts == ["hello"]
    assert factory.clients[0].disconnected


async def test_the_settings_can_turn_tool_search_on() -> None:
    factory = Factory([result()])
    settings = ClaudeSettings(env={TOOL_SEARCH_ENV: "true"})
    await collect(ClaudeRuntime(settings=settings, client_factory=factory).run(request()))
    assert factory.clients[0].options.env[TOOL_SEARCH_ENV] == "true"


async def test_custom_system_prompt_without_the_preset() -> None:
    factory = Factory([result()])
    runtime = ClaudeRuntime(
        settings=ClaudeSettings(claude_code_preset=False), client_factory=factory
    )
    await collect(runtime.run(request()))
    assert factory.clients[0].options.system_prompt == config().render_prefix()


def test_settings_reject_a_long_cancel_grace() -> None:
    with pytest.raises(ValueError, match="cancel_grace_s"):
        ClaudeSettings(cancel_grace_s=9)


def test_capabilities_and_kind() -> None:
    runtime = ClaudeRuntime(client_factory=Factory())
    assert runtime.kind is RuntimeKind.CLAUDE_AGENT_SDK
    assert runtime.capabilities().native_subagents


# -- streaming and completion ------------------------------------------------------------------


async def test_streams_text_and_completes() -> None:
    factory = Factory(
        [delta("Hel"), delta("lo"), assistant(TextBlock(text="Hello")), result(text="Hello")]
    )
    runtime = ClaudeRuntime(client_factory=factory)
    events = await collect(runtime.run(request()))
    terminal = validate_event_stream(events)
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hel", "lo"]
    assert isinstance(terminal, RunCompleted)
    assert terminal.output == "Hello"
    assert terminal.usage == Usage(
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=2,
        cache_write_tokens=1,
        cost_usd=Decimal("0.01"),
    )
    assert terminal.runtime_state is not None
    state = json.loads(terminal.runtime_state)
    assert state["session_id"] == SESSION
    assert state["pending"] is None


def message_start(parent: str | None = None) -> StreamEvent:
    return StreamEvent(
        uuid="u", session_id=SESSION, event={"type": "message_start"}, parent_tool_use_id=parent
    )


async def test_a_model_turn_sends_the_thinking_phase_before_its_text() -> None:
    factory = Factory(
        [
            message_start(),
            message_start(parent="toolu_sub"),  # a sub-agent turn: no phase
            delta("Hi"),
            assistant(TextBlock(text="Hi")),
            result(text="Hi"),
        ]
    )
    events = await collect(ClaudeRuntime(client_factory=factory).run(request()))
    validate_event_stream(events)
    assert [type(e) for e in events[:2]] == [RunPhase, TextDelta]
    phase = events[0]
    assert isinstance(phase, RunPhase)
    assert phase.phase is RunPhaseName.THINKING


async def test_text_without_stream_events_comes_from_the_assistant_message() -> None:
    factory = Factory([assistant(TextBlock(text="Hi there")), result(text=None)])
    events = await collect(ClaudeRuntime(client_factory=factory).run(request()))
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hi there"]
    assert isinstance(events[-1], RunCompleted)
    assert events[-1].output == "Hi there"


async def test_session_resume_uses_the_sdk_session_id() -> None:
    factory = Factory([result(cost=0.01)], [result(cost=0.03)])
    runtime = ClaudeRuntime(client_factory=factory)
    first = (await collect(runtime.run(request(run_id="run-1"))))[-1]
    assert isinstance(first, RunCompleted)
    second = (await collect(runtime.run(request(run_id="run-2", state=first.runtime_state))))[-1]
    assert factory.clients[1].options.resume == SESSION
    assert isinstance(second, RunCompleted)
    # The CLI reports the cost of the whole SDK session. The second run costs the difference.
    assert second.usage.cost_usd == Decimal("0.02")
    assert second.usage.input_tokens == 10


async def test_a_run_whose_sdk_session_is_gone_starts_a_new_one() -> None:
    """A new sandbox has no transcript of the saved session (Section 16.2). The run starts a
    new SDK session, and its cost counts from zero."""
    factory = Factory([result(cost=0.01)], [result(cost=0.03)])
    runtime = ClaudeRuntime(client_factory=factory)
    first = (await collect(runtime.run(request(run_id="run-1"))))[-1]
    assert isinstance(first, RunCompleted)
    runtime._settings = ClaudeSettings(session_exists=lambda session: session != SESSION)
    second = (await collect(runtime.run(request(run_id="run-2", state=first.runtime_state))))[-1]
    assert isinstance(second, RunCompleted)
    assert factory.clients[1].options.resume is None
    assert second.usage.cost_usd == Decimal("0.03")


async def test_a_gone_session_with_an_open_approval_drops_nothing() -> None:
    runtime, paused = await pause()
    factory = Factory([result(text="Hi.", cost=0.01)])
    runtime._client_factory = factory
    runtime._settings = ClaudeSettings(session_exists=lambda _: False)
    events = await collect(runtime.run(request("next", run_id="run-2", state=paused.runtime_state)))
    assert isinstance(events[-1], RunCompleted)
    assert len(factory.clients) == 1
    client = factory.clients[0]
    assert client.options.resume is None
    assert DROP_DEFERRED_ENV not in client.options.env
    assert client.prompts == ["next"]


async def test_an_approval_resume_of_a_gone_session_fails() -> None:
    runtime, paused = await pause()
    factory = Factory()
    runtime._client_factory = factory
    runtime._settings = ClaudeSettings(session_exists=lambda _: False)
    events = await collect(runtime.resume(resume_request(paused, ApprovalDecision.APPROVE)))
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.INVALID_REQUEST
    assert "gone" in events[-1].message
    assert factory.clients == []


async def test_tool_calls_and_results() -> None:
    factory = Factory(
        [
            assistant(ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})),
            tool_result("t1", "a.txt"),
            UserMessage(content="plain text is ignored"),
            result(),
        ]
    )
    events = await collect(ClaudeRuntime(client_factory=factory).run(request()))
    started = [e for e in events if isinstance(e, ToolCallStarted)]
    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert started[0].name == "Bash"
    assert started[0].arguments == {"command": "ls"}
    assert finished[0].call_id == "t1"
    assert finished[0].output_preview == "a.txt"
    assert not finished[0].is_error


async def test_subagent_messages_become_subagent_events() -> None:
    factory = Factory(
        [
            assistant(
                ToolUseBlock(id="task-1", name="Agent", input={"subagent_type": "researcher"})
            ),
            delta("hidden", parent="task-1"),
            assistant(TextBlock(text="Looking."), parent="task-1"),
            assistant(ToolUseBlock(id="t9", name="WebSearch", input={}), parent="task-1"),
            UserMessage(
                content=[ToolResultBlock(tool_use_id="t9", content="x")],
                parent_tool_use_id="task-1",
            ),
            tool_result("task-1", [{"type": "text", "text": "found it"}]),
            result(),
        ]
    )
    events = await collect(ClaudeRuntime(client_factory=factory).run(request()))
    sub = [e for e in events if isinstance(e, SubagentEvent)]
    assert [(e.subagent, e.text) for e in sub] == [
        ("researcher", "Looking."),
        ("researcher", "calls WebSearch"),
    ]
    assert not any(isinstance(e, TextDelta) for e in events)
    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert [e.call_id for e in finished] == ["task-1"]
    assert finished[0].output_preview == "found it"


async def test_compaction_becomes_a_compacted_event() -> None:
    boundary = SystemMessage(
        subtype="compact_boundary",
        data={"compact_metadata": {"trigger": "auto", "pre_tokens": 9000, "post_tokens": 1200}},
    )
    factory = Factory([SystemMessage(subtype="init", data={}), boundary, result()])
    events = await collect(ClaudeRuntime(client_factory=factory).run(request()))
    compacted = [e for e in events if isinstance(e, Compacted)]
    assert [(e.tokens_before, e.tokens_after) for e in compacted] == [(9000, 1200)]


# -- failures ----------------------------------------------------------------------------------


@pytest.mark.parametrize("subtype", ["error_max_turns", "error_max_budget_usd"])
async def test_budget_results_fail_with_budget_exceeded(subtype: str) -> None:
    factory = Factory([result(subtype=subtype, is_error=True, text=None)])
    events = await collect(ClaudeRuntime(client_factory=factory).run(request()))
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.BUDGET_EXCEEDED


async def test_model_error_is_retryable_for_overload() -> None:
    factory = Factory([result(is_error=True, errors=["overloaded"], api_error_status=529)])
    events = await collect(ClaudeRuntime(client_factory=factory).run(request()))
    failed = events[-1]
    assert isinstance(failed, RunFailed)
    assert failed.code is FailureCode.MODEL_ERROR
    assert failed.retryable
    assert "overloaded" not in failed.message
    assert "529" in failed.message


async def test_cli_error_strings_do_not_leak() -> None:
    factory = Factory(
        [result(subtype="error_during_execution", is_error=True, errors=["token sk-123 bad"])]
    )
    events = await collect(ClaudeRuntime(client_factory=factory).run(request()))
    assert isinstance(events[-1], RunFailed)
    assert "sk-123" not in events[-1].message


async def test_sdk_error_text_does_not_leak() -> None:
    error = CLIJSONDecodeError('{"tool_input": "sk-123"', ValueError("bad json"))
    factory = Factory([], connect_error=error)
    events = await collect(ClaudeRuntime(client_factory=factory).run(request()))
    assert isinstance(events[-1], RunFailed)
    assert events[-1].message == "claude agent sdk error: CLIJSONDecodeError"


async def test_sdk_error_fails_the_run() -> None:
    factory = Factory([], connect_error=CLIConnectionError("no cli"))
    events = await collect(ClaudeRuntime(client_factory=factory).run(request()))
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.INTERNAL
    assert "CLIConnectionError" in events[-1].message


async def test_unexpected_error_does_not_leak_the_message() -> None:
    factory = Factory([], connect_error=RuntimeError("secret detail"))
    events = await collect(ClaudeRuntime(client_factory=factory).run(request()))
    assert isinstance(events[-1], RunFailed)
    assert "secret detail" not in events[-1].message


async def test_cli_that_stops_without_a_result_fails() -> None:
    factory = Factory([assistant(TextBlock(text="hi"))])
    events = await collect(ClaudeRuntime(client_factory=factory).run(request()))
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.INTERNAL


async def test_wall_time_budget_ends_the_run() -> None:
    factory = Factory([], block=True)
    budget = Budget(max_wall_time_s=0.1)
    events = await collect(ClaudeRuntime(client_factory=factory).run(request(budget=budget)))
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.TIMEOUT


async def test_wrong_runtime_and_bad_state_are_invalid() -> None:
    runtime = ClaudeRuntime(client_factory=Factory())
    wrong = RunRequest(scope=make_scope(), input="x", config=make_config(RuntimeKind.FAKE))
    events = await collect(runtime.run(wrong))
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.INVALID_REQUEST
    events = await collect(runtime.run(request(state=b'{"turns": 1}')))
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.INVALID_REQUEST


# -- tool gate ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decision", "subagent", "expected"),
    [
        (GateDecision("allow"), False, "allow"),
        (GateDecision("deny", "blocked"), False, "deny"),
        (GateDecision("ask", "write"), True, "deny"),
        (RuntimeError("gate down"), False, "deny"),
    ],
)
async def test_hook_maps_gate_decisions(
    decision: GateDecision | Exception, subagent: bool, expected: str
) -> None:
    factory = Factory([call_hook("Bash", "t1", subagent=subagent), result()])
    gate = ScriptedGate(decision)
    await collect(ClaudeRuntime(gate=gate, client_factory=factory).run(request()))
    output = factory.clients[0].hook_outputs[0]
    assert output["hookSpecificOutput"]["permissionDecision"] == expected
    assert gate.calls == [("Bash", {"to": "a@b.c"})]


@pytest.mark.parametrize("action", ["allow", "deny"])
async def test_hook_gives_the_steer_notes_to_the_model(action: str) -> None:
    factory = Factory([call_hook("Bash", "t1"), call_hook("Bash", "t2"), result()])
    gate = ScriptedGate(GateDecision(action, "ok", steer="go left"))  # type: ignore[arg-type]
    await collect(ClaudeRuntime(gate=gate, client_factory=factory).run(request()))
    output = factory.clients[0].hook_outputs[0]["hookSpecificOutput"]
    assert output["additionalContext"] == "go left"
    assert output["permissionDecision"] == action


async def test_the_model_gets_the_original_request_with_the_joined_messages() -> None:
    """M7.1: the scripted model sees the original request and both added messages."""
    from sama.orchestrator.queue import steer_block

    block = steer_block(
        ["Also put the word STEERx at the end of your reply.", "Reply with the word SECONDx."],
        request="Use session_search three times, then reply with the word FIRSTx",
    )
    factory = Factory([call_hook("Bash", "t1"), result()])
    gate = ScriptedGate(GateDecision("allow", "ok", steer=block))
    await collect(ClaudeRuntime(gate=gate, client_factory=factory).run(request()))
    context = factory.clients[0].hook_outputs[0]["hookSpecificOutput"]["additionalContext"]
    assert "None of them cancels or replaces the original request" in context
    assert all(word in context for word in ("FIRSTx", "STEERx", "SECONDx"))


async def test_hook_without_notes_has_no_extra_context() -> None:
    factory = Factory([call_hook("Bash", "t1"), result()])
    await collect(
        ClaudeRuntime(gate=ScriptedGate(GateDecision("allow")), client_factory=factory).run(
            request()
        )
    )
    assert "additionalContext" not in factory.clients[0].hook_outputs[0]["hookSpecificOutput"]


async def test_ask_pauses_the_run_with_approval_required() -> None:
    _, paused = await pause("needs a human")
    assert paused.approval_id == "call-1"
    assert paused.tool_name == "mcp__sama__gmail_send"
    assert paused.arguments == {"to": "a@b.c"}
    assert paused.reason == "needs a human"
    assert paused.runtime_state is not None
    state = json.loads(paused.runtime_state)
    assert state["pending"] == {"approval_id": "call-1", "tool_name": "mcp__sama__gmail_send"}


async def test_deferred_result_without_a_tool_fails() -> None:
    factory = Factory([result(stop_reason="tool_deferred", deferred=None)])
    events = await collect(ClaudeRuntime(client_factory=factory).run(request()))
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.INTERNAL


# -- approval resume ---------------------------------------------------------------------------


async def test_approve_resumes_the_session_with_no_prompt() -> None:
    runtime, paused = await pause()
    factory = Factory([tool_result("call-1", "sent"), result(text="Sent.", cost=0.03)])
    runtime._client_factory = factory
    events = await collect(runtime.resume(resume_request(paused, ApprovalDecision.APPROVE)))
    terminal = validate_event_stream(events, start=paused.usage)
    assert isinstance(terminal, RunCompleted)
    client = factory.clients[0]
    assert client.prompts == []
    assert client.options.resume == SESSION
    assert client.options.disallowed_tools == []
    assert DROP_DEFERRED_ENV not in client.options.env
    assert terminal.usage.input_tokens == 20
    assert terminal.usage.cost_usd == Decimal("0.03")


@pytest.mark.parametrize("via_hook", [False, True])
async def test_approve_allows_only_the_approved_call(via_hook: bool) -> None:
    # On resume the CLI checks the deferred call again: through can_use_tool for most calls,
    # through the PreToolUse hook for some read-only calls. Both must allow only that call.
    runtime, paused = await pause()
    gate = ScriptedGate(GateDecision("ask", "still a write"))
    runtime._gate = gate
    check = call_hook if via_hook else call_can_use
    factory = Factory(
        [
            check("mcp__sama__gmail_send", "call-1"),
            call_can_use("Bash", "call-other"),
            tool_result("call-1", "sent"),
            result(text="Sent."),
        ]
    )
    runtime._client_factory = factory
    events = await collect(runtime.resume(resume_request(paused, ApprovalDecision.APPROVE)))
    assert isinstance(events[-1], RunCompleted)
    client = factory.clients[0]
    assert gate.calls == []
    *approved, other = client.permission_results
    if via_hook:
        assert client.hook_outputs[0]["hookSpecificOutput"]["permissionDecision"] == "allow"
    else:
        assert [type(r) for r in approved] == [PermissionResultAllow]
    assert isinstance(other, PermissionResultDeny)


async def test_can_use_tool_allows_only_calls_the_gate_allowed() -> None:
    factory = Factory(
        [
            call_hook("Bash", "t1"),
            call_can_use("Bash", "t1"),
            call_can_use("Bash", "t2"),
            result(),
        ]
    )
    gate = ScriptedGate(GateDecision("allow"))
    await collect(ClaudeRuntime(gate=gate, client_factory=factory).run(request()))
    allowed, unknown = factory.clients[0].permission_results
    assert isinstance(allowed, PermissionResultAllow)
    assert isinstance(unknown, PermissionResultDeny)


async def pause_with_usage(*, cost: float, turns: int) -> tuple[ClaudeRuntime, ApprovalRequired]:
    phase = deferred_phase()
    final = phase[-1]
    assert isinstance(final, ResultMessage)
    final.total_cost_usd = cost
    final.num_turns = turns
    runtime = ClaudeRuntime(
        gate=ScriptedGate(GateDecision("ask", "write")), client_factory=Factory(phase)
    )
    events = await collect(runtime.run(request()))
    assert isinstance(events[-1], ApprovalRequired)
    return runtime, events[-1]


async def test_resume_gets_only_the_rest_of_the_budget() -> None:
    # The CLI counts max_turns and max_budget_usd per process. A resume must not get them again.
    runtime, paused = await pause_with_usage(cost=0.08, turns=3)
    factory = Factory([result(cost=0.09)])
    runtime._client_factory = factory
    budget = Budget(max_turns=5, max_cost_usd=Decimal("0.10"))
    request_ = resume_request(paused, ApprovalDecision.APPROVE, budget=budget)
    events = await collect(runtime.resume(request_))
    assert isinstance(events[-1], RunCompleted)
    options = factory.clients[0].options
    assert options.max_turns == 2
    assert options.max_budget_usd == pytest.approx(0.02)


@pytest.mark.parametrize(
    ("budget", "code"),
    [
        (Budget(max_cost_usd=Decimal("0.05")), FailureCode.BUDGET_EXCEEDED),
        (Budget(max_turns=3), FailureCode.BUDGET_EXCEEDED),
        (Budget(max_wall_time_s=0.000001), FailureCode.TIMEOUT),
    ],
)
async def test_resume_with_a_used_up_budget_fails_before_the_cli_starts(
    budget: Budget, code: FailureCode
) -> None:
    runtime, paused = await pause_with_usage(cost=0.08, turns=3)
    factory = Factory()
    runtime._client_factory = factory
    events = await collect(
        runtime.resume(resume_request(paused, ApprovalDecision.APPROVE, budget=budget))
    )
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is code
    assert factory.clients == []


async def test_reject_drops_the_call_then_tells_the_model() -> None:
    runtime, paused = await pause()
    factory = Factory(
        [
            tool_result("call-1", "not available", is_error=True),
            result(
                stop_reason="tool_deferred_unavailable",
                is_error=True,
                input_tokens=0,
                output_tokens=0,
                cost=0.01,
            ),
        ],
        [assistant(TextBlock(text="OK, I will not send it.")), result(text=None, cost=0.02)],
    )
    runtime._client_factory = factory
    events = await collect(
        runtime.resume(resume_request(paused, ApprovalDecision.REJECT, reason="wrong person"))
    )
    terminal = validate_event_stream(events, start=paused.usage)
    assert isinstance(terminal, RunCompleted)
    assert terminal.output == "OK, I will not send it."
    drop, follow = factory.clients
    assert drop.prompts == []
    assert drop.options.disallowed_tools == ["mcp__sama__gmail_send"]
    assert drop.options.env[DROP_DEFERRED_ENV] == "1"
    assert follow.options.disallowed_tools == []
    assert follow.options.resume == SESSION
    assert "rejected the mcp__sama__gmail_send tool call" in follow.prompts[0]
    assert "wrong person" in follow.prompts[0]
    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert finished[0].is_error


async def test_reject_fails_if_the_cli_does_not_drop_the_call() -> None:
    runtime, paused = await pause()
    runtime._client_factory = Factory(
        [tool_result("call-1", "sent"), result(stop_reason="end_turn")]
    )
    events = await collect(runtime.resume(resume_request(paused, ApprovalDecision.REJECT)))
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.INTERNAL
    assert "did not drop" in events[-1].message


async def test_new_message_after_an_unanswered_approval_drops_the_call_first() -> None:
    runtime, paused = await pause()
    factory = Factory(
        [result(stop_reason="tool_deferred_unavailable", is_error=True, cost=0.01)],
        [result(text="Hi.")],
    )
    runtime._client_factory = factory
    await runtime.cancel("run-1")
    events = await collect(
        runtime.run(request("new topic", run_id="run-2", state=paused.runtime_state))
    )
    assert isinstance(events[-1], RunCompleted)
    drop, follow = factory.clients
    assert drop.options.disallowed_tools == ["mcp__sama__gmail_send"]
    assert follow.prompts == ["new topic"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"approval_id": "other"}, "unknown approval"),
        ({"run_id": "run-9"}, "another run"),
    ],
)
async def test_resume_rejects_a_wrong_approval(change: dict[str, Any], message: str) -> None:
    runtime, paused = await pause()
    events = await collect(
        runtime.resume(resume_request(paused, ApprovalDecision.APPROVE, **change))
    )
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.INVALID_REQUEST
    assert message in events[-1].message


async def test_resume_without_state_is_invalid() -> None:
    runtime, _ = await pause()
    bare = ResumeRequest(
        scope=make_scope(), config=config(), approval_id="call-1", decision=ApprovalDecision.APPROVE
    )
    events = await collect(runtime.resume(bare))
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.INVALID_REQUEST
    events = await collect(runtime.resume(bare.model_copy(update={"runtime_state": b"not json"})))
    assert isinstance(events[-1], RunFailed)


async def test_each_approval_resumes_once() -> None:
    runtime, paused = await pause()
    runtime._client_factory = Factory([result()], [result()])
    first = await collect(runtime.resume(resume_request(paused, ApprovalDecision.APPROVE)))
    assert isinstance(first[-1], RunCompleted)
    second = await collect(runtime.resume(resume_request(paused, ApprovalDecision.APPROVE)))
    assert isinstance(second[-1], RunFailed)
    assert "already used" in second[-1].message


async def test_paused_run_id_cannot_start_a_new_run() -> None:
    runtime, _ = await pause()
    events = await collect(runtime.run(request()))
    assert isinstance(events[-1], RunFailed)
    assert "already active" in events[-1].message


async def test_cancel_makes_the_approvals_of_a_paused_run_invalid() -> None:
    runtime, paused = await pause()
    await runtime.cancel("run-1")
    events = await collect(runtime.resume(resume_request(paused, ApprovalDecision.APPROVE)))
    assert isinstance(events[-1], RunFailed)
    assert "cancelled" in events[-1].message


# -- cancel ------------------------------------------------------------------------------------


async def _cancel_while_streaming(runtime: ClaudeRuntime) -> tuple[list[RunEvent], float]:
    events: list[RunEvent] = []
    stream = runtime.run(request())
    first = await anext(stream)
    events.append(first)
    started = time.monotonic()
    cancel = asyncio.create_task(runtime.cancel("run-1"))
    events.extend([e async for e in stream])
    await cancel
    return events, time.monotonic() - started


async def test_cancel_interrupts_the_cli() -> None:
    factory = Factory([delta("Hi")], block=True)
    events, elapsed = await _cancel_while_streaming(ClaudeRuntime(client_factory=factory))
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.CANCELLED
    assert factory.clients[0].interrupted.is_set()
    assert elapsed < 10


async def test_cancel_stops_the_stream_when_the_cli_hangs() -> None:
    factory = Factory([delta("Hi")], block=True, honour_interrupt=False)
    runtime = ClaudeRuntime(settings=ClaudeSettings(cancel_grace_s=0.2), client_factory=factory)
    events, elapsed = await _cancel_while_streaming(runtime)
    validate_event_stream(events)
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.CANCELLED
    assert elapsed < 2


async def test_cancel_after_a_queued_approval_cancels_the_run() -> None:
    phase = [delta("Hi"), *deferred_phase()]
    runtime = ClaudeRuntime(
        gate=ScriptedGate(GateDecision("ask", "write")), client_factory=Factory(phase)
    )
    stream = runtime.run(request())
    assert isinstance(await anext(stream), TextDelta)
    pump = runtime._active["run-1"].pump
    assert pump is not None
    await asyncio.wait({pump})  # The pump queued ApprovalRequired. The consumer did not read it.
    await runtime.cancel("run-1")
    rest = [e async for e in stream]
    assert isinstance(rest[-1], RunFailed)
    assert rest[-1].code is FailureCode.CANCELLED
    assert "run-1" not in runtime._paused


async def test_slow_disconnect_is_not_cancelled() -> None:
    factory = Factory([delta("Hi")], block=True, honour_interrupt=False, disconnect_s=0.5)
    runtime = ClaudeRuntime(settings=ClaudeSettings(cancel_grace_s=0.1), client_factory=factory)
    events, elapsed = await _cancel_while_streaming(runtime)
    assert isinstance(events[-1], RunFailed)
    assert elapsed < 0.5
    client = factory.clients[0]
    assert not client.disconnected
    await runtime.aclose()
    assert client.disconnected


async def test_aclose_right_after_cancel_waits_for_the_cli_to_stop() -> None:
    factory = Factory([delta("Hi")], block=True, honour_interrupt=False, disconnect_s=0.3)
    runtime = ClaudeRuntime(settings=ClaudeSettings(cancel_grace_s=0.1), client_factory=factory)
    stream = runtime.run(request())
    await anext(stream)
    await runtime.cancel("run-1")
    await runtime.aclose()  # The stream is not drained yet.
    assert factory.clients[0].disconnected


async def test_a_completed_run_does_not_wait_for_the_cli_to_stop() -> None:
    factory = Factory([delta("Hi"), result()], disconnect_s=0.5)
    runtime = ClaudeRuntime(settings=ClaudeSettings(cancel_grace_s=2.0), client_factory=factory)
    started = time.monotonic()
    stream = cast(AsyncGenerator[RunEvent], runtime.run(request()))
    events: list[RunEvent] = []
    async with contextlib.aclosing(stream):
        async for event in stream:
            events.append(event)
            if isinstance(event, RunCompleted):
                break
    assert isinstance(events[-1], RunCompleted)
    assert time.monotonic() - started < 0.4
    assert not factory.clients[0].disconnected
    await runtime.aclose()  # The runner waits for the CLI before it exits.
    assert factory.clients[0].disconnected


async def test_a_follow_up_run_waits_for_the_cli_of_the_last_run_to_stop() -> None:
    factory = Factory([delta("Hi"), result()], [result()], disconnect_s=0.5)
    looked: list[bool] = []

    def exists(_: str) -> bool:
        # The transcript is complete only after the first CLI stopped.
        looked.append(factory.clients[0].disconnected)
        return True

    runtime = ClaudeRuntime(
        settings=ClaudeSettings(cancel_grace_s=2.0, session_exists=exists),
        client_factory=factory,
    )
    first = await collect(runtime.run(request()))
    completed = first[-1]
    assert isinstance(completed, RunCompleted)
    assert not factory.clients[0].disconnected
    second = await collect(runtime.run(request(run_id="run-2", state=completed.runtime_state)))
    assert isinstance(second[-1], RunCompleted)
    assert looked == [True]
    assert factory.clients[0].disconnected
    await runtime.aclose()


async def test_two_follow_ups_of_one_run_during_the_stop_start_one_cli() -> None:
    factory = Factory([delta("Hi"), result()], [result()], [result()], disconnect_s=0.3)
    runtime = ClaudeRuntime(settings=ClaudeSettings(cancel_grace_s=2.0), client_factory=factory)
    completed = (await collect(runtime.run(request())))[-1]
    assert isinstance(completed, RunCompleted)
    follow_up = request(run_id="run-2", state=completed.runtime_state)
    results = await asyncio.gather(collect(runtime.run(follow_up)), collect(runtime.run(follow_up)))
    terminals = sorted(type(events[-1]).__name__ for events in results)
    assert terminals == ["RunCompleted", "RunFailed"]
    assert len(factory.clients) == 2
    await runtime.aclose()


async def test_a_cancel_during_the_stop_wait_ends_the_follow_up() -> None:
    factory = Factory([delta("Hi"), result()], [result()], disconnect_s=0.3)
    runtime = ClaudeRuntime(settings=ClaudeSettings(cancel_grace_s=2.0), client_factory=factory)
    completed = (await collect(runtime.run(request())))[-1]
    assert isinstance(completed, RunCompleted)
    task = asyncio.create_task(
        collect(runtime.run(request(run_id="run-2", state=completed.runtime_state)))
    )
    await asyncio.sleep(0.05)
    await runtime.cancel("run-2")
    events = await task
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.CANCELLED
    assert len(factory.clients) == 1
    await runtime.aclose()


def _hold_stop(runtime: ClaudeRuntime) -> asyncio.Event:
    """Makes the runtime see a CLI stop of SESSION that ends when the event is set."""
    release = asyncio.Event()
    runtime._stopping[SESSION] = asyncio.create_task(_wait_for(release))
    return release


async def _wait_for(event: asyncio.Event) -> None:
    await event.wait()


async def test_two_resumes_of_one_approval_during_the_stop_use_it_once() -> None:
    runtime, paused = await pause()
    factory = Factory([tool_result("call-1", "sent"), result()], [result()])
    runtime._client_factory = factory
    release = _hold_stop(runtime)
    resume = resume_request(paused, ApprovalDecision.APPROVE)
    tasks = [asyncio.create_task(collect(runtime.resume(resume))) for _ in range(2)]
    await asyncio.sleep(0.05)
    release.set()
    results = await asyncio.gather(*tasks)
    terminals = sorted(type(events[-1]).__name__ for events in results)
    assert terminals == ["RunCompleted", "RunFailed"]
    assert len(factory.clients) == 1


async def test_a_cancel_during_the_stop_wait_ends_the_resume() -> None:
    runtime, paused = await pause()
    factory = Factory([tool_result("call-1", "sent"), result()])
    runtime._client_factory = factory
    release = _hold_stop(runtime)
    task = asyncio.create_task(
        collect(runtime.resume(resume_request(paused, ApprovalDecision.APPROVE)))
    )
    await asyncio.sleep(0.05)
    await runtime.cancel("run-1")
    release.set()
    events = await task
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.CANCELLED
    assert factory.clients == []


async def test_a_failed_run_still_waits_for_the_cli_to_stop() -> None:
    factory = Factory([result(subtype="error_during_execution", is_error=True)], disconnect_s=0.3)
    runtime = ClaudeRuntime(settings=ClaudeSettings(cancel_grace_s=2.0), client_factory=factory)
    events = await collect(runtime.run(request()))
    assert isinstance(events[-1], RunFailed)
    assert factory.clients[0].disconnected


async def test_approval_state_counts_the_time_to_stop_the_cli() -> None:
    runtime = ClaudeRuntime(
        gate=ScriptedGate(GateDecision("ask", "write")),
        settings=ClaudeSettings(cancel_grace_s=1.0),
        client_factory=Factory(deferred_phase(), disconnect_s=0.3),
    )
    events = await collect(runtime.run(request()))
    paused = events[-1]
    assert isinstance(paused, ApprovalRequired)
    assert paused.runtime_state is not None
    assert json.loads(paused.runtime_state)["elapsed_s"] >= 0.3


async def test_cancel_of_an_unknown_run_does_nothing() -> None:
    await ClaudeRuntime(client_factory=Factory()).cancel("nope")


async def test_second_stream_for_an_active_run_is_invalid() -> None:
    factory = Factory([delta("Hi")], block=True)
    runtime = ClaudeRuntime(client_factory=factory)
    stream = runtime.run(request())
    await anext(stream)
    events = await collect(runtime.run(request()))
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.INVALID_REQUEST
    await runtime.cancel("run-1")
    rest = [e async for e in stream]
    assert isinstance(rest[-1], RunFailed)


async def test_closing_the_stream_early_stops_the_cli() -> None:
    factory = Factory([delta("Hi")], block=True, honour_interrupt=False)
    runtime = ClaudeRuntime(client_factory=factory)
    stream = cast(AsyncGenerator[RunEvent], runtime.run(request()))
    await anext(stream)
    await stream.aclose()
    assert factory.clients[0].disconnected
    assert "run-1" not in runtime._active
