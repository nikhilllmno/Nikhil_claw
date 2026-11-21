"""Live checks of vision mode on Browserbase (roadmap M5.2). Run only with ``SAMA_LIVE_TESTS=1``.

They need ``BROWSERBASE_API_KEY``, ``BROWSERBASE_PROJECT_ID``, and ``SAMA_LIVE_CANVAS_URL``: a
public URL of the canvas app of ``tests.browser.canvas_app``. Browserbase runs outside our
network, so it cannot open a local server. For example::

    uv run python -m tests.browser.canvas_app --port 8765   # then make the port public
    SAMA_LIVE_TESTS=1 SAMA_LIVE_CANVAS_URL=https://<public host>/ \\
        uv run --all-groups pytest -m live tests/browser/test_vision_live.py

- The first test does the task at the known points of the canvas app, on a real Browserbase
  session, through ``BrowserService``.
- The model tests give a real model the browser tools over MCP, from each runtime, and a canvas
  app with a random layout (``?seed=N``). The model must look at screenshots and act at points.
  They need ``ANTHROPIC_API_KEY`` or ``OPENAI_API_KEY`` and cost money. After the run, the test
  reads the title of the page with ``browser_snapshot`` over MCP, so it does not trust the
  answer of the model.

Each test deletes the Browserbase contexts that it made.
"""

from __future__ import annotations

import importlib.util
import os
import re
import secrets
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from sama.browser.browserbase import API_URL, BrowserbaseTarget
from sama.browser.contexts import MemoryContextStore
from sama.browser.guard import UrlGuard
from sama.browser.service import BrowserService
from sama.browser.target import Act, BrowserError
from sama.runtime import AgentRuntime, RunCompleted, RunRequest, RuntimeKind
from sama.runtime.contract import McpServerConfig, RunConfig, RunScope
from sama.runtime.gating import GateDecision
from sama.tenancy import RunTokenSigner
from sama.tools import MemoryTodoStore, ToolServer
from tests.browser import canvas_app
from tests.conformance.scripted_model import serve
from tests.support import collect, make_claims, make_scope
from tests.tools.test_server import connect, text_of

LIVE = os.environ.get("SAMA_LIVE_TESTS") == "1"
KEY = os.environ.get("BROWSERBASE_API_KEY", "")
PROJECT = os.environ.get("BROWSERBASE_PROJECT_ID", "")
CANVAS_URL = os.environ.get("SAMA_LIVE_CANVAS_URL", "")
CLAUDE_MODEL = os.environ.get("SAMA_LIVE_VISION_CLAUDE_MODEL", "claude-sonnet-5-5")
OPENAI_MODEL = os.environ.get("SAMA_LIVE_VISION_OPENAI_MODEL", "gpt-5.5")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE, reason="set SAMA_LIVE_TESTS=1"),
    pytest.mark.skipif(not (KEY and PROJECT), reason="set the Browserbase key and project"),
    pytest.mark.skipif(not CANVAS_URL, reason="set SAMA_LIVE_CANVAS_URL"),
]

_TITLE = re.compile(r"Title: (Canvas quest[^\n<]*)")


def _browserbase(contexts: MemoryContextStore) -> BrowserbaseTarget:
    return BrowserbaseTarget(
        api_key=SecretStr(KEY), project_id=PROJECT, contexts=contexts, session_timeout_s=600
    )


async def _delete_contexts(contexts: MemoryContextStore) -> None:
    async with httpx.AsyncClient(
        base_url=API_URL, headers={"X-BB-API-Key": KEY}, timeout=30
    ) as client:
        for context_id in contexts.rows.values():
            await client.delete(f"/v1/contexts/{context_id}")


def _title(text: str) -> str:
    found = _TITLE.findall(text)
    assert found, text[:2000]
    return str(found[-1]).strip()


@pytest.fixture
async def service() -> AsyncIterator[BrowserService]:
    contexts = MemoryContextStore()
    service = BrowserService(_browserbase(contexts), UrlGuard())
    try:
        yield service
    finally:
        await service.aclose()
        await _delete_contexts(contexts)


async def test_vision_mode_does_the_canvas_task_on_browserbase(service: BrowserService) -> None:
    claims = make_claims(f"run-vision-{secrets.token_hex(4)}")
    opened = await service.open(claims, CANVAS_URL)
    assert _title(opened) == "Canvas quest 0/6"
    assert "vision mode" in opened
    caption, shot = await service.screenshot(claims)
    assert shot.data[:3] == b"\xff\xd8\xff"
    # One pixel of the image is one CSS pixel of the viewport: the canvas app needs 800x540.
    assert shot.width >= 800, caption
    assert shot.height >= 540, caption
    assert f"The image is {shot.width}x{shot.height} pixels. It is the viewport" in caption
    # One pixel of the image is one CSS pixel: the viewport check of the session has the same
    # size. (Browserbase has a device scale factor of 2, so an image in device pixels would be
    # twice as wide.)
    with pytest.raises(BrowserError, match=rf"outside the viewport \({shot.width}x{shot.height}\)"):
        await service.act(claims, Act(action="hover", x=float(shot.width), y=10.0), snapshot=False)

    async def act(action: str, point: canvas_app.Point, **fields: Any) -> str:
        step = Act(action=action, x=float(point[0]), y=float(point[1]), **fields)  # type: ignore[arg-type]
        return _title(await service.act(claims, step, snapshot=False))

    for number in (1, 2, 3):
        title = await act("click", canvas_app.CIRCLES[number])
    assert title == "Canvas quest 1/6"
    assert await act("click", canvas_app.DIAMOND, clicks=2) == "Canvas quest 2/6"
    assert await act("hover", canvas_app.HOVER) == "Canvas quest 3/6"
    box = canvas_app.BOX
    assert (
        await act("drag", canvas_app.SQUARE, to_x=float(box[0]), to_y=float(box[1]))
        == "Canvas quest 4/6"
    )
    assert await act("type", canvas_app.TEXT_BOX, text="sama", submit=True) == "Canvas quest 5/6"
    assert await act("scroll", canvas_app.GAUGE, amount=700) == canvas_app.SOLVED_TITLE
    _, after = await service.screenshot(claims)
    assert after.data != shot.data


# -- a real model --------------------------------------------------------------------------------


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


class World:
    def __init__(self) -> None:
        self.contexts = MemoryContextStore()
        self.signer = RunTokenSigner(secrets.token_bytes(32))
        self.service = BrowserService(_browserbase(self.contexts), UrlGuard())
        self.server = ToolServer(signer=self.signer, todos=MemoryTodoStore(), browser=self.service)


@pytest.fixture
def world() -> Iterator[tuple[World, str]]:
    world = World()
    try:
        # The lifespan of the tool server closes the browser service when the server stops.
        with serve(world.server.app()) as url:
            yield world, url
    finally:
        with httpx.Client(base_url=API_URL, headers={"X-BB-API-Key": KEY}, timeout=30) as client:
            for context_id in world.contexts.rows.values():
                client.delete(f"/v1/contexts/{context_id}")


SOUL = (
    "You are a careful agent that uses a browser. The page of this task is drawn on a canvas, "
    "so it has no element refs: use browser_screenshot (full_page=false) to look at it and "
    "browser_act with x and y in the pixels of that screenshot. Take a new screenshot after "
    "each action to check the result. Use snapshot=false with browser_act."
)


def _canvas_url(seed: int) -> str:
    return f"{CANVAS_URL.rstrip('/')}/?seed={seed}"


async def _solve_with_a_model(
    runtime: AgentRuntime, world: World, url: str, kind: RuntimeKind, model: str
) -> None:
    seed = secrets.randbelow(10_000) + 1
    scope = make_scope(f"run-vision-{secrets.token_hex(4)}")
    token = world.signer.issue(scope, ttl_s=3600)
    config = RunConfig(
        runtime=kind,
        model=model,
        soul=SOUL,
        mcp_servers=(
            McpServerConfig(name="sama", url=f"{url}/mcp", bearer_token=SecretStr(token)),
        ),
    )
    task = f"Open {_canvas_url(seed)} in the browser. {canvas_app.TASK}"
    events = await collect(runtime.run(RunRequest(scope=scope, input=task, config=config)))
    done = events[-1]
    assert isinstance(done, RunCompleted), events[-3:]
    # Read the page, not the answer of the model.
    async with connect(f"{url}/mcp", token) as client:
        result = await client.call_tool("browser_snapshot", {})
    assert _title(text_of(result)) == canvas_app.SOLVED_TITLE, (seed, done.output[-2000:])


@pytest.mark.skipif(
    importlib.util.find_spec("claude_agent_sdk") is None or not os.environ.get("ANTHROPIC_API_KEY"),
    reason="needs claude-agent-sdk and ANTHROPIC_API_KEY",
)
async def test_a_claude_model_does_the_canvas_task_in_vision_mode(
    tmp_path: Path, world: tuple[World, str]
) -> None:
    from sama.runtime.claude import ClaudeRuntime, ClaudeSettings

    state, url = world
    runtime = ClaudeRuntime(gate=AllowGate(), settings=ClaudeSettings(cwd=str(tmp_path)))
    try:
        await _solve_with_a_model(runtime, state, url, RuntimeKind.CLAUDE_AGENT_SDK, CLAUDE_MODEL)
    finally:
        await runtime.aclose()


@pytest.mark.skipif(
    importlib.util.find_spec("agents") is None or not os.environ.get("OPENAI_API_KEY"),
    reason="needs openai-agents and OPENAI_API_KEY",
)
async def test_an_openai_model_does_the_canvas_task_in_vision_mode(
    tmp_path: Path, world: tuple[World, str]
) -> None:
    from sama.runtime.openai_agents import OpenAIRuntime, OpenAISettings
    from sama.runtime.session_items import MemorySessionItems

    state, url = world
    runtime = OpenAIRuntime(
        items=MemorySessionItems(),
        gate=AllowGate(),
        settings=OpenAISettings(cwd=str(tmp_path), api_key=os.environ["OPENAI_API_KEY"]),
    )
    try:
        await _solve_with_a_model(runtime, state, url, RuntimeKind.OPENAI_AGENTS_SDK, OPENAI_MODEL)
    finally:
        await runtime.aclose()
