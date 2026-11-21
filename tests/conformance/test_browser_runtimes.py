"""The browser tools from both runtimes (roadmap M5.1, decision D2).

The real Claude CLI and the OpenAI Agents SDK call the sama tool server with a browser service on
a fake target. The scripted models call the tool and reply with the tool result, so the test
shows that each runtime reaches the browser tools over MCP and gets the result back.
"""

from __future__ import annotations

import importlib.util
import secrets
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from sama.browser.service import BrowserService
from sama.runtime import RunCompleted, RunRequest, RuntimeKind
from sama.runtime.contract import McpServerConfig, RunConfig, RunScope
from sama.runtime.gating import GateDecision
from sama.tenancy import RunTokenSigner
from sama.tools import MemoryTodoStore, ToolServer
from tests.browser.fakes import AllowAll, FakeTarget
from tests.conformance.scripted_model import ScriptedModel, serve
from tests.conformance.scripted_openai import ScriptedResponses
from tests.conformance.targets import claude_env
from tests.support import collect, make_scope

HAS_CLAUDE_SDK = importlib.util.find_spec("claude_agent_sdk") is not None
HAS_OPENAI_SDK = importlib.util.find_spec("agents") is not None


class AllowGate:
    async def check(
        self,
        scope: RunScope,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        call_id: str,
        child: bool = False,
    ) -> GateDecision:
        return GateDecision("allow")


class Browser:
    def __init__(self) -> None:
        self.signer = RunTokenSigner(secrets.token_bytes(32))
        self.target = FakeTarget()
        self.service = BrowserService(self.target, AllowAll())
        self.server = ToolServer(signer=self.signer, todos=MemoryTodoStore(), browser=self.service)


@pytest.fixture
def browser() -> Iterator[tuple[Browser, str]]:
    world = Browser()
    with serve(world.server.app()) as url:
        yield world, url


def request_for(world: Browser, url: str, kind: RuntimeKind, model: str, text: str) -> RunRequest:
    scope = make_scope(f"run-{uuid.uuid4().hex[:8]}")
    token = world.signer.issue(scope, ttl_s=600)
    config = RunConfig(
        runtime=kind,
        model=model,
        soul="You are a test agent. Follow the user's instructions exactly.",
        mcp_servers=(
            McpServerConfig(name="sama", url=f"{url}/mcp", bearer_token=SecretStr(token)),
        ),
    )
    return RunRequest(scope=scope, input=text, config=config)


OPEN = 'CALL mcp__sama__browser_open {"url": "https://example.com/rooms"}'
EXTRACT = 'CALL mcp__sama__browser_extract {"kind": "text"}'


def check_open(world: Browser, output: str) -> None:
    # The OpenAI runtime gives the model the MCP content list; the scripted model echoes it as
    # JSON, so compare the text without the JSON quotes.
    text = output.replace('\\"', '"')
    assert output.startswith("tool result: "), output
    assert "Tab 0 of 1. Use the [ref=...] values" in text
    assert '<untrusted_content source="browser" url="https://example.com/rooms">' in text
    assert '- button "Go" [ref=e1]' in text
    assert world.target.sessions[0].calls[:2] == ["navigate https://example.com/rooms", "snapshot"]


@pytest.mark.skipif(not HAS_CLAUDE_SDK, reason="claude-agent-sdk is not installed")
async def test_the_claude_runtime_uses_the_browser_tools(
    tmp_path: Path,
    scripted_model: tuple[ScriptedModel, str],
    browser: tuple[Browser, str],
) -> None:
    from sama.runtime.claude import ClaudeRuntime, ClaudeSettings

    world, url = browser
    _, model_url = scripted_model
    workdir = tmp_path / "work"
    workdir.mkdir()
    runtime = ClaudeRuntime(
        gate=AllowGate(),
        settings=ClaudeSettings(cwd=str(workdir), env=claude_env(tmp_path / "claude", model_url)),
    )
    try:
        first = request_for(world, url, RuntimeKind.CLAUDE_AGENT_SDK, "claude-sonnet-4-5", OPEN)
        events = await collect(runtime.run(first))
        done = events[-1]
        assert isinstance(done, RunCompleted), events
        check_open(world, done.output)
        second = first.model_copy(update={"input": EXTRACT, "runtime_state": done.runtime_state})
        events = await collect(runtime.run(second))
        assert isinstance(events[-1], RunCompleted), events
        assert "Hello from the page" in events[-1].output
        # The same run uses the same browser session.
        assert len(world.target.sessions) == 1
    finally:
        # The tool server thread owns the browser service; its lifespan closes the service.
        await runtime.aclose()


@pytest.mark.skipif(not HAS_OPENAI_SDK, reason="openai-agents is not installed")
async def test_the_openai_runtime_uses_the_browser_tools(
    tmp_path: Path,
    scripted_responses: tuple[ScriptedResponses, str],
    browser: tuple[Browser, str],
) -> None:
    from sama.runtime.openai_agents import OpenAIRuntime, OpenAISettings
    from sama.runtime.session_items import MemorySessionItems

    world, url = browser
    _, responses_url = scripted_responses
    workdir = tmp_path / "work"
    workdir.mkdir()
    runtime = OpenAIRuntime(
        items=MemorySessionItems(),
        gate=AllowGate(),
        settings=OpenAISettings(
            cwd=str(workdir), model_url=f"{responses_url}/v1", api_key="scripted-model"
        ),
    )
    try:
        first = request_for(world, url, RuntimeKind.OPENAI_AGENTS_SDK, "gpt-6-luna", OPEN)
        events = await collect(runtime.run(first))
        done = events[-1]
        assert isinstance(done, RunCompleted), events
        check_open(world, done.output)
        second = first.model_copy(update={"input": EXTRACT, "runtime_state": done.runtime_state})
        events = await collect(runtime.run(second))
        assert isinstance(events[-1], RunCompleted), events
        assert "Hello from the page" in events[-1].output
        assert len(world.target.sessions) == 1
    finally:
        # The tool server thread owns the browser service; its lifespan closes the service.
        await runtime.aclose()
