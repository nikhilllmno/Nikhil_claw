"""Native sub-agents of the OpenAI adapter: agents as tools, with the real OpenAI Agents SDK and
the scripted Responses API (docs/architecture.md, Sections 13.1 and 13.3).

The main agent calls the tool of a sub-agent. The sub-agent calls a tool of the MCP server. The
tests check that the gate sees an ``Agent`` call and a child call, that a gate ``ask`` inside
the sub-agent is a ``deny``, and that the sub-agent has only the tools of its definition.
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
from sama.runtime.contract import RunEvent, RunScope
from sama.runtime.gating import GateDecision
from tests.conformance.conftest import OPENAI_MODEL
from tests.conformance.scripted_openai import ScriptedResponses
from tests.conformance.targets import mcp_config
from tests.conformance.tool_server import LOOKUP, SEND_NOTE, ToolServer, tool_id
from tests.support import collect, make_config, make_scope

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("agents") is None, reason="openai-agents is not installed"
)


class RecordingGate:
    """Allows every tool, except that it asks for ``send_note``. It does not map ``ask`` to
    ``deny`` for a child call, so the adapter must do it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], bool]] = []

    async def check(
        self,
        scope: RunScope,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        call_id: str,
        child: bool = False,
    ) -> GateDecision:
        self.calls.append((tool_name, dict(arguments), child))
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


def subagent_call(task: str) -> str:
    return "CALL subagent_researcher " + json.dumps({"input": task})


async def run(
    tmp_path: Path,
    model_url: str,
    tools: ToolServer,
    tools_url: str,
    prompt: str,
    *subagents: SubagentDefinition,
) -> tuple[RecordingGate, list[RunEvent]]:
    from sama.runtime.openai_agents import OpenAIRuntime, OpenAISettings
    from sama.runtime.session_items import MemorySessionItems

    tools.calls.clear()  # the server of the suite lives for all tests
    tools.notes.clear()
    workdir = tmp_path / "work"
    workdir.mkdir()
    gate = RecordingGate()
    runtime = OpenAIRuntime(
        items=MemorySessionItems(),
        gate=gate,
        settings=OpenAISettings(cwd=str(workdir), model_url=f"{model_url}/v1", api_key="x"),
    )
    config = make_config(RuntimeKind.OPENAI_AGENTS_SDK).model_copy(
        update={
            "mcp_servers": mcp_config(tools, tools_url),
            "model": OPENAI_MODEL,
            "subagents": subagents,
        }
    )
    request = RunRequest(
        scope=make_scope(f"run-{uuid.uuid4().hex[:8]}"), input=prompt, config=config
    )
    try:
        events = await collect(runtime.run(request))
    finally:
        await runtime.aclose()
    return gate, events


async def test_a_subagent_is_a_tool_and_its_call_goes_through_the_gate_as_a_child(
    tmp_path: Path,
    scripted_responses: tuple[ScriptedResponses, str],
    tool_server: tuple[ToolServer, str],
) -> None:
    tools, tools_url = tool_server
    model, model_url = scripted_responses
    task = "CALL " + tool_id(LOOKUP) + ' {"key": "colour"}'
    gate, events = await run(
        tmp_path,
        model_url,
        tools,
        tools_url,
        subagent_call(task),
        researcher(LOOKUP),
    )
    done = events[-1]
    assert isinstance(done, RunCompleted), done
    assert tools.calls == [LOOKUP]
    # The gate judged the Agent call of the main agent, then the call inside the sub-agent.
    assert [(name, child) for name, _, child in gate.calls] == [
        ("Agent", False),
        (tool_id(LOOKUP), True),
    ]
    assert gate.calls[0][1] == {"subagent_type": "researcher", "prompt": task}
    started = [e for e in events if isinstance(e, ToolCallStarted)]
    assert [e.name for e in started] == ["Agent"]
    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert tools.secret in " ".join(e.output_preview for e in finished)
    sub = [e for e in events if isinstance(e, SubagentEvent)]
    assert sub, events
    assert all(e.subagent == "researcher" for e in sub)
    assert sub[-1].done
    assert sub[-1].call_id == started[0].call_id
    # The sub-agent got its own prompt and only its own tools: no tool of another sub-agent.
    nested = [r for r in model.requests if "You look things up" in r.instructions]
    assert nested
    assert all("subagent_researcher" not in r.tool_names for r in nested)
    assert all(SEND_NOTE not in r.tool_names for r in nested)


async def test_an_ask_inside_a_subagent_is_a_deny(
    tmp_path: Path,
    scripted_responses: tuple[ScriptedResponses, str],
    tool_server: tuple[ToolServer, str],
) -> None:
    tools, tools_url = tool_server
    _, model_url = scripted_responses
    task = "CALL " + tool_id(SEND_NOTE) + ' {"text": "hello"}'
    gate, events = await run(
        tmp_path, model_url, tools, tools_url, subagent_call(task), researcher(SEND_NOTE)
    )
    done = events[-1]
    # The run does not pause: the call of the sub-agent is denied, and the note is not sent.
    assert isinstance(done, RunCompleted), done
    assert tools.notes == []
    assert (tool_id(SEND_NOTE), True) in [(name, child) for name, _, child in gate.calls]


async def test_a_main_agent_without_subagents_has_no_subagent_tool(
    tmp_path: Path,
    scripted_responses: tuple[ScriptedResponses, str],
    tool_server: tuple[ToolServer, str],
) -> None:
    tools, tools_url = tool_server
    model, model_url = scripted_responses
    _, events = await run(tmp_path, model_url, tools, tools_url, "Say hi")
    assert isinstance(events[-1], RunCompleted)
    assert all(not n.startswith("subagent_") for n in model.requests[-1].tool_names)


async def test_a_subagent_with_bash_uses_the_shell_of_the_run(
    tmp_path: Path,
    scripted_responses: tuple[ScriptedResponses, str],
    tool_server: tuple[ToolServer, str],
) -> None:
    tools, tools_url = tool_server
    _, model_url = scripted_responses
    task = 'CALL Bash {"command": "echo fig > sub.txt"}'
    gate, events = await run(
        tmp_path, model_url, tools, tools_url, subagent_call(task), researcher("Bash")
    )
    assert isinstance(events[-1], RunCompleted), events[-1]
    assert ("Bash", True) in [(name, child) for name, _, child in gate.calls], events
    assert (tmp_path / "work" / "sub.txt").read_text().strip() == "fig"


async def test_the_last_event_of_a_subagent_has_the_whole_output(
    tmp_path: Path,
    scripted_responses: tuple[ScriptedResponses, str],
    tool_server: tuple[ToolServer, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The preview of a tool call is short, but the sub-agent event is not cut by the runner:
    # the control plane redacts the whole text and then cuts it (Section 13.1, A9).
    import sama.runtime.openai_agents as adapter

    monkeypatch.setattr(adapter, "_PREVIEW_CHARS", 5)
    tools, tools_url = tool_server
    _, model_url = scripted_responses
    task = "CALL " + tool_id(LOOKUP) + ' {"key": "colour"}'
    _, events = await run(
        tmp_path, model_url, tools, tools_url, subagent_call(task), researcher(LOOKUP)
    )
    last = [e for e in events if isinstance(e, SubagentEvent)][-1]
    assert last.done
    assert tools.secret in last.text
    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert all(tools.secret not in e.output_preview for e in finished)
