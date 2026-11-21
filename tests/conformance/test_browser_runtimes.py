"""The browser tools from both runtimes (roadmap M5.1, M5.2, and M5.6, decision D2).

The real Claude CLI and the OpenAI Agents SDK call the sama tool server with a browser service on
a fake target. The scripted models call the tool and reply with the tool result, so the test
shows that each runtime reaches the browser tools over MCP and gets the result back.

The vision tests (M5.2) give the service a local headless Chromium and a canvas app with no
accessibility tree. Each runtime does the task of the app only with a screenshot and
``browser_act`` at points.
"""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from sama.browser.service import VISION_HINT, BrowserService
from sama.browser.task import BrowserTaskRunner
from sama.modelproxy.prices import PriceTable
from sama.runtime import AgentRuntime, RunCompleted, RunRequest, RuntimeKind
from sama.runtime.contract import McpServerConfig, RunConfig, RunScope
from sama.runtime.gating import GateDecision
from sama.tenancy import RunTokenSigner
from sama.tools import MemoryTodoStore, ToolServer
from tests.browser import canvas_app
from tests.browser.fakes import AllowAll, FakeTarget
from tests.browser.local import LocalChromiumTarget
from tests.browser.test_task import ScriptedModel as SubAgentModel
from tests.browser.test_task import done as sub_agent_done
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


def _chromium_installed() -> bool:
    """The local Chromium headless shell of Playwright is installed."""
    root = Path(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright"
    )
    return any(root.glob("chromium_headless_shell-*")) or any(root.glob("chromium-*"))


class Browser:
    def __init__(self, target: FakeTarget | LocalChromiumTarget | None = None) -> None:
        self.signer = RunTokenSigner(secrets.token_bytes(32))
        self.target = target or FakeTarget()
        self.service = BrowserService(self.target, AllowAll())
        self.sub_agent = SubAgentModel([sub_agent_done("The title is Rooms.")])
        self.tasks = BrowserTaskRunner(
            self.service, self.sub_agent, PriceTable.load(), model="claude-sonnet-5-5"
        )
        self.server = ToolServer(
            signer=self.signer,
            todos=MemoryTodoStore(),
            browser=self.service,
            browser_tasks=self.tasks,
        )


@pytest.fixture
def browser() -> Iterator[tuple[Browser, str]]:
    world = Browser()
    with serve(world.server.app()) as url:
        yield world, url


@pytest.fixture
def vision() -> Iterator[tuple[Browser, str, str]]:
    """A tool server with a local Chromium target, and the URL of the canvas app."""
    if not _chromium_installed():
        pytest.skip("the Chromium headless shell is not installed")
    world = Browser(LocalChromiumTarget())
    with serve(canvas_app.app()) as canvas, serve(world.server.app()) as url:
        yield world, url, canvas + "/"


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
TASK = 'CALL mcp__sama__browser_task {"goal": "Read the title of the page", "max_steps": 3}'


def check_task(world: Browser, request: RunRequest, output: str) -> None:
    """The sub-agent ran with the run token of the run, and its result came back."""
    assert "browser_task done. Steps: 1 of 3." in output, output
    assert "The title is Rooms." in output
    (server,) = request.config.mcp_servers
    assert server.bearer_token is not None
    assert world.sub_agent.tokens == [server.bearer_token.get_secret_value()]


def check_open(world: Browser, output: str) -> None:
    # The OpenAI runtime gives the model the MCP content list; the scripted model echoes it as
    # JSON, so compare the text without the JSON quotes.
    text = output.replace('\\"', '"')
    assert output.startswith("tool result: "), output
    assert "Tab 0 of 1. Use the [ref=...] values" in text
    assert '<untrusted_content source="browser" url="https://example.com/rooms">' in text
    assert '- button "Go" [ref=e1]' in text
    target = world.target
    assert isinstance(target, FakeTarget)
    assert target.sessions[0].calls[:2] == ["navigate https://example.com/rooms", "snapshot"]


def _call(tool: str, **arguments: object) -> str:
    return f"CALL mcp__sama__{tool} {json.dumps(arguments)}"


def _act(action: str, point: canvas_app.Point, **arguments: object) -> str:
    return _call("browser_act", action=action, x=point[0], y=point[1], snapshot=False, **arguments)


VISION_SCRIPT: tuple[tuple[str, str], ...] = (
    (_call("browser_screenshot"), "The image is 1280x720 pixels. It is the viewport"),
    *((_act("click", canvas_app.CIRCLES[n]), "Canvas quest") for n in (1, 2)),
    (_act("click", canvas_app.CIRCLES[3]), "Title: Canvas quest 1/6"),
    (_act("click", canvas_app.DIAMOND, clicks=2), "Title: Canvas quest 2/6"),
    (_act("hover", canvas_app.HOVER), "Title: Canvas quest 3/6"),
    (
        _act("drag", canvas_app.SQUARE, to_x=canvas_app.BOX[0], to_y=canvas_app.BOX[1]),
        "Title: Canvas quest 4/6",
    ),
    (_act("type", canvas_app.TEXT_BOX, text="sama", submit=True), "Title: Canvas quest 5/6"),
    (_act("scroll", canvas_app.GAUGE, amount=700), f"Title: {canvas_app.SOLVED_TITLE}"),
)
"""The calls of the scripted model after it opens the canvas app, and a text of each result."""


async def solve_the_canvas_app(
    runtime: AgentRuntime, world: Browser, url: str, canvas: str, kind: RuntimeKind, model: str
) -> None:
    """Runs the vision script, one tool call for each run of the same session."""
    request = request_for(world, url, kind, model, _call("browser_open", url=canvas))
    events = await collect(runtime.run(request))
    done = events[-1]
    assert isinstance(done, RunCompleted), events
    assert "Canvas quest 0/6" in done.output
    assert VISION_HINT in done.output
    for text, expected in VISION_SCRIPT:
        request = request.model_copy(update={"input": text, "runtime_state": done.runtime_state})
        events = await collect(runtime.run(request))
        done = events[-1]
        assert isinstance(done, RunCompleted), events
        assert expected in done.output, (text, done.output[:2000])
    target = world.target
    assert isinstance(target, LocalChromiumTarget)
    # One browser session for the whole session of runs.
    assert len(target.sessions) == 1


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
        third = first.model_copy(update={"input": TASK, "runtime_state": events[-1].runtime_state})
        events = await collect(runtime.run(third))
        assert isinstance(events[-1], RunCompleted), events
        check_task(world, third, events[-1].output)
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
        third = first.model_copy(update={"input": TASK, "runtime_state": events[-1].runtime_state})
        events = await collect(runtime.run(third))
        assert isinstance(events[-1], RunCompleted), events
        check_task(world, third, events[-1].output)
    finally:
        # The tool server thread owns the browser service; its lifespan closes the service.
        await runtime.aclose()


@pytest.mark.skipif(not HAS_CLAUDE_SDK, reason="claude-agent-sdk is not installed")
async def test_the_claude_runtime_solves_a_canvas_app_in_vision_mode(
    tmp_path: Path,
    scripted_model: tuple[ScriptedModel, str],
    vision: tuple[Browser, str, str],
) -> None:
    from sama.runtime.claude import ClaudeRuntime, ClaudeSettings

    world, url, canvas = vision
    _, model_url = scripted_model
    workdir = tmp_path / "work"
    workdir.mkdir()
    runtime = ClaudeRuntime(
        gate=AllowGate(),
        settings=ClaudeSettings(cwd=str(workdir), env=claude_env(tmp_path / "claude", model_url)),
    )
    try:
        await solve_the_canvas_app(
            runtime, world, url, canvas, RuntimeKind.CLAUDE_AGENT_SDK, "claude-sonnet-4-5"
        )
    finally:
        await runtime.aclose()


@pytest.mark.skipif(not HAS_OPENAI_SDK, reason="openai-agents is not installed")
async def test_the_openai_runtime_solves_a_canvas_app_in_vision_mode(
    tmp_path: Path,
    scripted_responses: tuple[ScriptedResponses, str],
    vision: tuple[Browser, str, str],
) -> None:
    from sama.runtime.openai_agents import OpenAIRuntime, OpenAISettings
    from sama.runtime.session_items import MemorySessionItems

    world, url, canvas = vision
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
        await solve_the_canvas_app(
            runtime, world, url, canvas, RuntimeKind.OPENAI_AGENTS_SDK, "gpt-6-luna"
        )
    finally:
        await runtime.aclose()


class SlowTarget(FakeTarget):
    """A target that takes longer to start than the 5-second MCP default of the OpenAI SDK, as
    a Browserbase session does."""

    async def start(self, **kwargs: Any) -> Any:
        import asyncio

        await asyncio.sleep(6.5)
        return await super().start(**kwargs)


@pytest.mark.skipif(not HAS_OPENAI_SDK, reason="openai-agents is not installed")
async def test_the_openai_runtime_waits_for_a_slow_sama_tool(
    tmp_path: Path, scripted_responses: tuple[ScriptedResponses, str]
) -> None:
    from sama.runtime.openai_agents import OpenAIRuntime, OpenAISettings
    from sama.runtime.session_items import MemorySessionItems

    world = Browser(SlowTarget())
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
        with serve(world.server.app()) as url:
            request = request_for(world, url, RuntimeKind.OPENAI_AGENTS_SDK, "gpt-6-luna", OPEN)
            events = await collect(runtime.run(request))
        done = events[-1]
        assert isinstance(done, RunCompleted), events
        assert "An error occurred" not in done.output
        check_open(world, done.output)
    finally:
        await runtime.aclose()
