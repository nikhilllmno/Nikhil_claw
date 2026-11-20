"""The native sub-agents of the Claude adapter: the ``agents`` option, the hook that limits
the sub-agent tool, and the events of a sub-agent (docs/architecture.md, Section 13.1)."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("claude_agent_sdk")

from claude_agent_sdk import SystemMessage, TextBlock, ToolUseBlock
from claude_agent_sdk.types import TaskNotificationMessage, TaskStartedMessage

from sama.runtime import RunRequest, SubagentDefinition, SubagentEvent
from sama.runtime.claude import (
    BACKGROUND_TASKS_ENV,
    TOOL_SEARCH_ENV,
    ClaudeRuntime,
    GateDecision,
)
from sama.runtime.contract import SUBAGENT_WIRE_MAX_CHARS
from tests.runtime.test_claude import (
    Factory,
    ScriptedGate,
    assistant,
    call_hook,
    config,
    result,
)
from tests.support import collect, make_scope

RESEARCHER = SubagentDefinition(
    name="researcher",
    description="Looks things up.",
    prompt="You look things up.",
    tools=("Read", "web_search", "lookup"),
    max_turns=5,
)


def with_subagents(*agents: SubagentDefinition) -> RunRequest:
    return RunRequest(
        scope=make_scope("run-1"),
        input="hello",
        config=config().model_copy(update={"subagents": agents}),
    )


# -- the agents option -------------------------------------------------------------------------


async def test_a_config_without_subagents_passes_no_agents() -> None:
    factory = Factory([result()])
    await collect(ClaudeRuntime(client_factory=factory).run(with_subagents()))
    options = factory.clients[0].options
    assert options.agents is None
    assert BACKGROUND_TASKS_ENV not in (options.env or {})


async def test_the_subagents_of_the_config_become_the_agents_option() -> None:
    other = SubagentDefinition(
        name="writer", description="Writes.", prompt="You write.", model="claude-haiku-4-5"
    )
    factory = Factory([result()])
    await collect(ClaudeRuntime(client_factory=factory).run(with_subagents(RESEARCHER, other)))
    options = factory.clients[0].options
    assert options.agents is not None
    assert list(options.agents) == ["researcher", "writer"]
    researcher = options.agents["researcher"]
    assert researcher.description == "Looks things up."
    assert researcher.prompt == "You look things up."
    # A native tool keeps its name. Every other sama tool is a tool of the sama MCP server.
    assert researcher.tools == ["Read", "mcp__sama__web_search", "mcp__sama__lookup"]
    assert researcher.maxTurns == 5
    assert researcher.model == "inherit"
    assert researcher.background is False
    # A sub-agent cannot start a sub-agent.
    assert researcher.disallowedTools == ["Agent", "Task"]
    writer = options.agents["writer"]
    assert writer.model == "claude-haiku-4-5"
    assert writer.tools == [], "an empty list is no tool, and not all tools"
    assert writer.maxTurns is None


async def test_subagents_turn_off_background_tasks_and_keep_the_other_env() -> None:
    factory = Factory([result()])
    await collect(ClaudeRuntime(client_factory=factory).run(with_subagents(RESEARCHER)))
    env = factory.clients[0].options.env
    assert env == {TOOL_SEARCH_ENV: "false", BACKGROUND_TASKS_ENV: "1"}


# -- the hook ----------------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["Agent", "Task"])
async def test_a_defined_subagent_goes_to_the_gate_and_is_allowed(tool: str) -> None:
    factory = Factory(
        [call_hook(tool, "t1", tool_input={"subagent_type": "researcher", "prompt": "x"}), result()]
    )
    gate = ScriptedGate(GateDecision("allow"))
    await collect(ClaudeRuntime(gate=gate, client_factory=factory).run(with_subagents(RESEARCHER)))
    output = factory.clients[0].hook_outputs[0]
    assert output["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert [name for name, _ in gate.calls] == [tool]


@pytest.mark.parametrize(
    ("tool_input", "reason"),
    [
        ({"subagent_type": "general-purpose", "prompt": "x"}, "does not define"),
        ({"subagent_type": "Explore", "prompt": "x"}, "does not define"),
        ({"prompt": "x"}, "does not define"),
        ({"subagent_type": 7, "prompt": "x"}, "does not define"),
        ({"subagent_type": "researcher", "run_in_background": True}, "background"),
    ],
)
async def test_the_hook_denies_a_subagent_that_the_profile_does_not_define(
    tool_input: dict[str, Any], reason: str
) -> None:
    factory = Factory([call_hook("Agent", "t1", tool_input=tool_input), result()])
    gate = ScriptedGate(GateDecision("allow"))
    await collect(ClaudeRuntime(gate=gate, client_factory=factory).run(with_subagents(RESEARCHER)))
    hook = factory.clients[0].hook_outputs[0]["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny"
    assert reason in hook["permissionDecisionReason"]
    assert gate.calls == [], "the gate has no record of a call that cannot start"


async def test_the_hook_denies_the_subagent_tool_when_the_config_has_no_subagents() -> None:
    factory = Factory(
        [call_hook("Task", "t1", tool_input={"subagent_type": "researcher"}), result()]
    )
    gate = ScriptedGate(GateDecision("allow"))
    await collect(ClaudeRuntime(gate=gate, client_factory=factory).run(with_subagents()))
    hook = factory.clients[0].hook_outputs[0]["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny"


async def test_a_call_inside_a_subagent_that_the_gate_asks_for_is_denied() -> None:
    factory = Factory([call_hook("mcp__sama__send_note", "t2", subagent=True), result()])
    gate = ScriptedGate(GateDecision("ask", "a write"))
    await collect(ClaudeRuntime(gate=gate, client_factory=factory).run(with_subagents(RESEARCHER)))
    hook = factory.clients[0].hook_outputs[0]["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny"
    assert len(gate.calls) == 1, "the gate judges the call of a sub-agent"


# -- the events --------------------------------------------------------------------------------


def started(call_id: str | None, description: str = "look") -> TaskStartedMessage:
    return TaskStartedMessage(
        subtype="task_started",
        data={},
        task_id="task-1",
        description=description,
        uuid="u1",
        session_id="s1",
        tool_use_id=call_id,
    )


def notified(
    call_id: str | None,
    status: str = "completed",
    usage: dict[str, int] | None = None,
    summary: str = "found it",
) -> TaskNotificationMessage:
    return TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id="task-1",
        status=status,  # type: ignore[arg-type]
        output_file="/x",
        summary=summary,
        uuid="u2",
        session_id="s1",
        tool_use_id=call_id,
        usage=usage,  # type: ignore[arg-type]
    )


async def test_an_invocation_has_a_start_events_and_an_end_with_its_tokens() -> None:
    factory = Factory(
        [
            assistant(
                ToolUseBlock(id="task-1", name="Agent", input={"subagent_type": "researcher"})
            ),
            started("task-1"),
            assistant(TextBlock(text="Looking."), parent="task-1"),
            notified("task-1", usage={"total_tokens": 321, "tool_uses": 1, "duration_ms": 9}),
            result(),
        ]
    )
    events = await collect(ClaudeRuntime(client_factory=factory).run(with_subagents(RESEARCHER)))
    sub = [e for e in events if isinstance(e, SubagentEvent)]
    assert [(e.subagent, e.text, e.call_id, e.done, e.tokens) for e in sub] == [
        ("researcher", "started: look", "task-1", False, None),
        ("researcher", "Looking.", "task-1", False, None),
        ("researcher", "completed: found it", "task-1", True, 321),
    ]


async def test_an_end_without_usage_has_no_tokens() -> None:
    factory = Factory(
        [
            assistant(
                ToolUseBlock(id="task-1", name="Agent", input={"subagent_type": "researcher"})
            ),
            notified("task-1", status="failed"),
            result(),
        ]
    )
    events = await collect(ClaudeRuntime(client_factory=factory).run(with_subagents(RESEARCHER)))
    [end] = [e for e in events if isinstance(e, SubagentEvent)]
    assert (end.text, end.done, end.tokens) == ("failed: found it", True, None)


async def test_a_task_that_is_not_a_subagent_gives_no_event() -> None:
    factory = Factory(
        [
            started("unknown"),
            started(None),
            notified("unknown"),
            SystemMessage(subtype="task_progress", data={}),
            result(),
        ]
    )
    events = await collect(ClaudeRuntime(client_factory=factory).run(with_subagents(RESEARCHER)))
    assert not [e for e in events if isinstance(e, SubagentEvent)]


# A known secret that crosses the 2,000-character mark of the stored text. The runner has no
# redactor, so it must send the whole text. The control plane redacts it and then cuts it.
SECRET = "opaque-secret-0123456789abcdef"


def around_the_mark(secret: str = SECRET) -> str:
    return "x" * (2_000 - 10) + secret + "y" * 3_000


async def test_the_runner_sends_the_whole_text_of_a_start_and_an_end() -> None:
    text = around_the_mark()
    factory = Factory(
        [
            assistant(
                ToolUseBlock(id="task-1", name="Agent", input={"subagent_type": "researcher"})
            ),
            started("task-1", description=text),
            assistant(TextBlock(text=text), parent="task-1"),
            notified("task-1", summary=text),
            result(),
        ]
    )
    events = await collect(ClaudeRuntime(client_factory=factory).run(with_subagents(RESEARCHER)))
    lines = [e.text for e in events if isinstance(e, SubagentEvent)]
    assert lines == [f"started: {text}", text, f"completed: {text}"]
    assert all(SECRET in line for line in lines), "no part of the secret is cut"


async def test_a_text_over_the_wire_limit_is_left_out_whole_and_not_cut() -> None:
    text = "z" * (SUBAGENT_WIRE_MAX_CHARS + 1) + SECRET
    factory = Factory(
        [
            assistant(
                ToolUseBlock(id="task-1", name="Agent", input={"subagent_type": "researcher"})
            ),
            started("task-1", description=text),
            assistant(TextBlock(text=text), parent="task-1"),
            notified("task-1", summary=text),
            result(),
        ]
    )
    events = await collect(ClaudeRuntime(client_factory=factory).run(with_subagents(RESEARCHER)))
    lines = [e.text for e in events if isinstance(e, SubagentEvent)]
    assert len(lines) == 3
    for line in lines:
        assert "is not in this event" in line
        assert "z" not in line
        assert SECRET not in line
        assert len(line) < 200
