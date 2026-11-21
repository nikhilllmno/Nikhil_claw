"""The six browser tools on the sama MCP tool server (docs/architecture.md, Section 14.1)."""

from __future__ import annotations

import base64
import secrets
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from mcp_types import ImageContent, TextContent

from sama.browser.service import BrowserService
from sama.tenancy import RunTokenSigner
from sama.tools import MemoryTodoStore, ToolServer
from sama.tools.browser import BROWSER_TOOLS
from tests.browser.fakes import PNG_1PX, DenyHosts, FakeTarget
from tests.conformance.scripted_model import serve
from tests.support import make_scope
from tests.tools.test_server import connect, text_of


@dataclass
class World:
    url: str
    signer: RunTokenSigner
    target: FakeTarget
    service: BrowserService

    def token(self, run_id: str = "run-1") -> str:
        return self.signer.issue(make_scope(run_id), ttl_s=600)


@pytest.fixture
def world() -> Iterator[World]:
    signer = RunTokenSigner(secrets.token_bytes(32))
    target = FakeTarget()
    service = BrowserService(target, DenyHosts(frozenset({"10.0.0.1"})))
    server = ToolServer(signer=signer, todos=MemoryTodoStore(), browser=service)
    with serve(server.app()) as base:
        yield World(base + "/mcp", signer, target, service)


async def test_the_server_lists_the_browser_tools(world: World) -> None:
    async with connect(world.url, world.token()) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) >= BROWSER_TOOLS
    act = tools["browser_act"]
    assert act.annotations is not None
    assert act.annotations.destructive_hint
    assert set(act.input_schema["properties"]["action"]["enum"]) >= {"click", "type", "dialog"}
    assert tools["browser_snapshot"].annotations is not None
    assert tools["browser_snapshot"].annotations.read_only_hint


async def test_a_server_without_a_browser_has_no_browser_tools() -> None:
    signer = RunTokenSigner(secrets.token_bytes(32))
    server = ToolServer(signer=signer, todos=MemoryTodoStore())
    with serve(server.app()) as base:
        async with connect(base + "/mcp", signer.issue(make_scope(), ttl_s=600)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
    assert not names & BROWSER_TOOLS


async def test_the_tools_work_on_one_session_of_the_run(world: World) -> None:
    async with connect(world.url, world.token()) as client:
        opened = await client.call_tool("browser_open", {"url": "example.com"})
        snapshot = await client.call_tool("browser_snapshot", {})
        act = await client.call_tool("browser_act", {"action": "click", "ref": "e1"})
        extract = await client.call_tool("browser_extract", {"kind": "text"})
        tabs = await client.call_tool("browser_tabs", {"action": "list"})
    for result in (opened, snapshot, act, extract, tabs):
        assert not result.is_error, text_of(result)
    assert 'url="https://example.com"' in text_of(opened)
    assert '- button "Go" [ref=e1]' in text_of(snapshot)
    assert "Hello from the page" in text_of(extract)
    assert text_of(tabs).startswith("1 tab(s).")
    (session,) = world.target.sessions
    assert session.calls == [
        "navigate https://example.com",
        "snapshot",
        "snapshot",
        "act click e1",
        "snapshot",
        "extract text",
        "tabs list None None",
    ]
    assert world.target.starts == [("org-1", "user-1", "run-1")]


async def test_the_screenshot_is_an_image(world: World) -> None:
    async with connect(world.url, world.token()) as client:
        result = await client.call_tool("browser_screenshot", {"full_page": True})
    assert not result.is_error
    caption, image = result.content
    assert isinstance(caption, TextContent)
    assert caption.text.startswith("Tab 0 of 1. A screenshot of")
    assert isinstance(image, ImageContent)
    assert image.mime_type == "image/png"
    assert base64.b64decode(image.data) == PNG_1PX


@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        ("browser_open", {"url": "http://10.0.0.1/admin"}, "the URL policy blocked"),
        ("browser_open", {"url": "file:///etc/passwd"}, "only http and https"),
        ("browser_act", {"action": "click"}, "click needs ref"),
        ("browser_act", {"action": "click", "x": 5}, "a point needs both x and y"),
        (
            "browser_act",
            {"action": "select", "x": 5, "y": 5, "values": ["a"]},
            "x and y work only with",
        ),
        ("browser_act", {"action": "click", "ref": "e1", "x": 5, "y": 5}, "not both"),
        ("browser_act", {"action": "click", "x": 5, "y": 5}, "cannot act at a point"),
        ("browser_tabs", {"action": "select"}, "select needs index"),
        ("browser_extract", {"kind": "fields"}, "kind fields needs fields"),
    ],
)
async def test_a_browser_error_is_a_tool_error_for_the_agent(
    world: World, tool: str, arguments: dict[str, object], message: str
) -> None:
    async with connect(world.url, world.token()) as client:
        result = await client.call_tool(tool, arguments)
    assert result.is_error
    assert message in text_of(result)


async def test_the_schema_refuses_a_bad_ref(world: World) -> None:
    async with connect(world.url, world.token()) as client:
        result = await client.call_tool(
            "browser_act", {"action": "click", "ref": "e1; document.cookie"}
        )
    assert result.is_error
    assert world.target.sessions == []


async def test_the_tools_take_a_target_when_the_service_has_two() -> None:
    import uuid

    from sama.browser.chrome import ChromeTarget
    from sama.gateway.chrome import ChromeHub
    from tests.browser.test_chrome import FakeLink, ok, page

    user = uuid.uuid4()
    link = FakeLink(ok({"page": page()}), ok({"page": page(), "tree": '- link "Home" [ref=e1]'}))
    hub = ChromeHub()
    hub.register(user, link)

    async def lookup(org_id: str, user_id: str) -> uuid.UUID | None:
        return user

    signer = RunTokenSigner(secrets.token_bytes(32))
    target = FakeTarget()
    service = BrowserService([target, ChromeTarget(hub, lookup)], DenyHosts(frozenset()))
    server = ToolServer(signer=signer, todos=MemoryTodoStore(), browser=service)
    with serve(server.app()) as base:
        async with connect(base + "/mcp", signer.issue(make_scope(), ttl_s=600)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            opened = await client.call_tool(
                "browser_open", {"url": "https://example.com", "target": "my_chrome"}
            )
    open_tool = tools["browser_open"]
    assert 'target="my_chrome"' in (open_tool.description or "")
    assert set(open_tool.input_schema["properties"]["target"]["anyOf"][0]["enum"]) == {
        "cloud",
        "my_chrome",
    }
    assert not opened.is_error, text_of(opened)
    assert '- link "Home" [ref=e1]' in text_of(opened)
    # The server closes the session when it stops: the extension gets a release.
    assert [frame["op"] for frame in link.frames] == ["navigate", "snapshot", "release"]
    assert link.frames[0]["approved"] is False
    assert target.sessions == []


async def test_browser_vault_is_listed_only_with_the_saved_logins(world: World) -> None:
    async with connect(world.url, world.token()) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert "browser_vault" not in names


async def test_browser_vault_fills_through_the_server_without_the_secret() -> None:
    from tests.browser.fakes import AllowAll, FillSession
    from tests.browser.test_vault_service import PASSWORD, USERNAME, Logins, login

    signer = RunTokenSigner(secrets.token_bytes(32))
    target = FakeTarget(session_class=FillSession)
    logins = Logins(login("Example", "https://example.com", personal=False))
    service = BrowserService(target, AllowAll(), logins=logins)
    server = ToolServer(signer=signer, todos=MemoryTodoStore(), browser=service)
    with serve(server.app()) as base:
        token = signer.issue(make_scope(), ttl_s=600)
        async with connect(base + "/mcp", token) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            listed = await client.call_tool("browser_vault", {"action": "list"})
            await client.call_tool("browser_open", {"url": "https://example.com/login"})
            (session,) = target.sessions
            session.tree = f'- textbox "Pin" [ref=e2]: {PASSWORD} for {USERNAME}'
            filled = await client.call_tool("browser_vault", {"action": "fill", "name": "Example"})
            missing = await client.call_tool("browser_vault", {"action": "fill"})
    assert set(tools["browser_vault"].input_schema["properties"]["action"]["enum"]) == {
        "list",
        "fill",
    }
    assert not listed.is_error
    assert '"name": "Example"' in text_of(listed)
    assert not filled.is_error, text_of(filled)
    for result in (listed, filled):
        assert PASSWORD not in text_of(result)
        assert USERNAME not in text_of(result)
    assert "[REDACTED] for [REDACTED]" in text_of(filled)
    assert missing.is_error
    assert "fill needs name" in text_of(missing)
    await service.aclose()


async def test_browser_act_takes_a_point_in_vision_mode(world: World) -> None:
    world.target.vision = True
    async with connect(world.url, world.token()) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        click = await client.call_tool(
            "browser_act", {"action": "click", "x": 120.5, "y": 80, "clicks": 2, "snapshot": False}
        )
        drag = await client.call_tool(
            "browser_act",
            {"action": "drag", "x": 10, "y": 20, "to_x": 300, "to_y": 40, "snapshot": False},
        )
    properties = tools["browser_act"].input_schema["properties"]
    assert set(properties) >= {"x", "y", "to_x", "to_y", "clicks"}
    assert "vision mode" in (tools["browser_act"].description or "")
    assert "vision mode" in (tools["browser_screenshot"].description or "")
    assert not click.is_error, text_of(click)
    assert not drag.is_error, text_of(drag)
    (session,) = world.target.sessions
    assert session.calls == ["act click None at (120.5, 80)", "act drag None at (10, 20)"]


@pytest.mark.parametrize("point", [{"x": -1, "y": 5}, {"x": 5, "y": 10_001}])
async def test_the_schema_refuses_a_point_out_of_range(
    world: World, point: dict[str, float]
) -> None:
    world.target.vision = True
    async with connect(world.url, world.token()) as client:
        result = await client.call_tool("browser_act", {"action": "click", **point})
    assert result.is_error
    assert world.target.sessions == []
