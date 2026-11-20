"""Native sub-agents of the Claude adapter with the real CLI and the scripted model
(docs/architecture.md, Section 13.1).

The model of the test starts a sub-agent. The sub-agent calls a tool of the MCP server. The
tests check what the CLI does, and not only the hooks of the adapter: the sub-agent runs in the
foreground, its tool call goes through the tool gate, and the CLI has only the sub-agents of the
config.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from sama.runtime import (
    RunCompleted,
    RunRequest,
    RuntimeKind,
    SubagentDefinition,
    SubagentEvent,
    ToolCallFinished,
    ToolCallStarted,
)
from sama.runtime.claude import ClaudeRuntime, ClaudeSettings, GateDecision
from sama.runtime.contract import RunEvent, RunScope
from tests.conformance.scripted_model import ScriptedModel
from tests.conformance.targets import claude_env, mcp_config
from tests.conformance.tool_server import LOOKUP, SEND_NOTE, ToolServer, tool_id
from tests.support import collect, make_config, make_scope

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("claude_agent_sdk") is None,
    reason="claude-agent-sdk is not installed",
)


class RecordingGate:
    """Allows every tool, except that it asks for ``send_note``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def check(
        self,
        scope: RunScope,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        call_id: str,
    ) -> GateDecision:
        self.calls.append((tool_name, call_id))
        if tool_name == tool_id(SEND_NOTE):
            return GateDecision("ask", "a write")
        return GateDecision("allow")


def researcher(*tools: str) -> SubagentDefinition:
    return SubagentDefinition(
        name="researcher",
        description="Looks things up.",
        prompt="You look things up with your tools.",
        tools=tools,
        max_turns=5,
    )


def agent_call(subagent: str, task: str, **extra: Any) -> str:
    arguments = {"subagent_type": subagent, "description": "look", "prompt": task, **extra}
    return "CALL Agent " + json.dumps(arguments)


async def run(
    tmp_path: Path,
    model_url: str,
    tools: ToolServer,
    tools_url: str,
    prompt: str,
    *subagents: SubagentDefinition,
) -> tuple[RecordingGate, list[RunEvent]]:
    tools.calls.clear()  # the server of the suite lives for all tests
    tools.notes.clear()
    workdir = tmp_path / "work"
    workdir.mkdir()
    settings = ClaudeSettings(cwd=str(workdir), env=claude_env(tmp_path / "claude", model_url))
    gate = RecordingGate()
    runtime = ClaudeRuntime(settings=settings, gate=gate)
    config = make_config(RuntimeKind.CLAUDE_AGENT_SDK).model_copy(
        update={
            "mcp_servers": mcp_config(tools, tools_url),
            "model": "claude-sonnet-4-5",
            "subagents": subagents,
        }
    )
    request = RunRequest(
        scope=make_scope(f"run-{uuid.uuid4().hex[:8]}"), input=prompt, config=config
    )
    events = await collect(runtime.run(request))
    await runtime.aclose()
    return gate, events


async def test_a_subagent_runs_in_the_foreground_and_its_tool_call_goes_through_the_gate(
    tmp_path: Path,
    scripted_model: tuple[ScriptedModel, str],
    tool_server: tuple[ToolServer, str],
) -> None:
    tools, tools_url = tool_server
    _, model_url = scripted_model
    task = "CALL " + tool_id(LOOKUP) + ' {"key": "colour"}'
    gate, events = await run(
        tmp_path,
        model_url,
        tools,
        tools_url,
        agent_call("researcher", task),
        researcher(tool_id(LOOKUP).removeprefix("mcp__sama__")),
    )
    done = events[-1]
    assert isinstance(done, RunCompleted), done
    # The sub-agent called the tool of the MCP server. It did not run in the background.
    assert tools.calls == [LOOKUP]
    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    outputs = " ".join(e.output_preview for e in finished)
    assert tools.secret in outputs
    assert "Async agent launched" not in outputs
    # The gate judged the Agent call and the call inside the sub-agent.
    assert [name for name, _ in gate.calls] == ["Agent", tool_id(LOOKUP)]
    agent_id = gate.calls[0][1]
    assert gate.calls[1][1] != agent_id
    # The events: the Agent tool call, then the events of the sub-agent with the call id.
    assert any(isinstance(e, ToolCallStarted) and e.call_id == agent_id for e in events)
    assert any(isinstance(e, ToolCallFinished) and e.call_id == agent_id for e in events)
    sub = [e for e in events if isinstance(e, SubagentEvent)]
    assert sub, "the sub-agent has events"
    assert {e.subagent for e in sub} == {"researcher"}
    assert {e.call_id for e in sub} == {agent_id}
    assert [e.done for e in sub].count(True) == 1
    assert sub[-1].done is True


async def test_a_call_inside_a_subagent_that_the_gate_asks_for_is_denied(
    tmp_path: Path,
    scripted_model: tuple[ScriptedModel, str],
    tool_server: tuple[ToolServer, str],
) -> None:
    tools, tools_url = tool_server
    _, model_url = scripted_model
    task = "CALL " + tool_id(SEND_NOTE) + ' {"text": "from the sub-agent"}'
    gate, events = await run(
        tmp_path,
        model_url,
        tools,
        tools_url,
        agent_call("researcher", task),
        researcher("send_note"),
    )
    assert isinstance(events[-1], RunCompleted), events[-1]
    assert [name for name, _ in gate.calls] == ["Agent", tool_id(SEND_NOTE)]
    # Nobody can approve inside a sub-agent, so the tool did not run.
    assert tools.calls == []
    assert tools.notes == []


async def test_a_subagent_has_only_the_tools_of_its_definition(
    tmp_path: Path,
    scripted_model: tuple[ScriptedModel, str],
    tool_server: tuple[ToolServer, str],
) -> None:
    tools, tools_url = tool_server
    _, model_url = scripted_model
    task = "CALL " + tool_id(LOOKUP) + ' {"key": "colour"}'
    _gate, events = await run(
        tmp_path,
        model_url,
        tools,
        tools_url,
        agent_call("researcher", task),
        researcher(),  # no tool
    )
    assert isinstance(events[-1], RunCompleted), events[-1]
    assert tools.calls == [], "the sub-agent has no tool, so the call has no effect"


@pytest.mark.parametrize(
    "prompt",
    [
        agent_call("general-purpose", "x"),
        agent_call("Explore", "x"),
    ],
    ids=["builtin", "builtin-explore"],
)
async def test_a_subagent_that_the_config_does_not_allow_is_denied(
    tmp_path: Path,
    scripted_model: tuple[ScriptedModel, str],
    tool_server: tuple[ToolServer, str],
    prompt: str,
) -> None:
    tools, tools_url = tool_server
    _, model_url = scripted_model
    gate, events = await run(tmp_path, model_url, tools, tools_url, prompt, researcher())
    assert isinstance(events[-1], RunCompleted), events[-1]
    assert gate.calls == [], "the hook denies the call before the gate"
    assert not [e for e in events if isinstance(e, SubagentEvent)]


async def test_a_request_for_a_background_subagent_runs_in_the_foreground(
    tmp_path: Path,
    scripted_model: tuple[ScriptedModel, str],
    tool_server: tuple[ToolServer, str],
) -> None:
    """The adapter turns off the background tasks of the CLI. The CLI then drops the flag, so
    the hook of the adapter does not see it (it denies the flag, too, as a second guard)."""
    tools, tools_url = tool_server
    _, model_url = scripted_model
    task = "CALL " + tool_id(LOOKUP) + ' {"key": "colour"}'
    _gate, events = await run(
        tmp_path,
        model_url,
        tools,
        tools_url,
        agent_call("researcher", task, run_in_background=True),
        researcher("lookup"),
    )
    assert isinstance(events[-1], RunCompleted), events[-1]
    outputs = " ".join(e.output_preview for e in events if isinstance(e, ToolCallFinished))
    assert tools.secret in outputs
    assert "Async agent launched" not in outputs
