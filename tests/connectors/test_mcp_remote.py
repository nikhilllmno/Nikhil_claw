"""The client of remote MCP servers (docs/architecture.md, Section 10.3)."""

from __future__ import annotations

import json

import pytest

from sama.connectors.mcp_remote import (
    MAX_DESCRIPTION_CHARS,
    McpAuthError,
    McpError,
    McpSessionGoneError,
    McpUnavailableError,
    RemoteTool,
    answer_of,
    exposed_tools,
    parse_tool,
    render_result,
    sama_name,
    tool_access,
    tools_hash,
)
from tests.connectors.fake_mcp import GOOD_TOKEN, METADATA, URL, FakeMcp, client, tool

AUTH = {"authorization": f"Bearer {GOOD_TOKEN}"}


@pytest.mark.parametrize("stream", [False, True])
async def test_the_client_opens_a_session_lists_and_calls_tools(stream: bool) -> None:
    fake = FakeMcp(stream=stream)
    mcp = client(fake)
    session = await mcp.open(URL, AUTH)
    assert session.session_id == "s-1"
    tools = await mcp.list_tools(URL, AUTH, session)
    assert [t.name for t in tools] == ["list_items", "create_item", "drop_item"]
    assert tools[0].read_only is True
    out = await mcp.call_tool(URL, AUTH, session, "list_items", {"q": "x"})
    assert out == 'ran list_items {"q": "x"}'
    assert fake.calls == [("list_items", {"q": "x"})]
    # The session and the protocol version go with each request after initialize.
    last = fake.seen[-1]
    assert last.headers["mcp-session-id"] == "s-1"
    assert last.headers["mcp-protocol-version"] == session.version
    assert "text/event-stream" in last.headers["accept"]
    await mcp.aclose()


async def test_the_client_reads_all_pages_of_the_tool_list() -> None:
    fake = FakeMcp()
    fake.tools = [tool(f"t{i}") for i in range(7)]
    fake.page_size = 3
    mcp = client(fake)
    session = await mcp.open(URL, AUTH)
    assert [t.name for t in await mcp.list_tools(URL, AUTH, session)] == [f"t{i}" for i in range(7)]
    await mcp.aclose()


async def test_a_server_without_sessions_works() -> None:
    fake = FakeMcp(auth="none")
    fake.use_sessions = False
    mcp = client(fake)
    session = await mcp.open(URL, {})
    assert session.session_id is None
    assert "mcp-session-id" not in fake.seen[-1].headers
    assert len(await mcp.list_tools(URL, {}, session)) == 3
    await mcp.aclose()


async def test_the_client_maps_the_errors() -> None:
    fake = FakeMcp()
    mcp = client(fake)
    with pytest.raises(McpAuthError) as caught:
        await mcp.open(URL, {"authorization": "Bearer wrong"})
    assert caught.value.challenge is not None
    assert METADATA in caught.value.challenge
    session = await mcp.open(URL, AUTH)
    fake.sessions.clear()
    with pytest.raises(McpSessionGoneError):
        await mcp.list_tools(URL, AUTH, session)
    fake.down = True
    with pytest.raises(McpUnavailableError):
        await mcp.open(URL, AUTH)
    fake.down = False
    session = await mcp.open(URL, AUTH)
    with pytest.raises(McpError, match="no method"):
        await mcp._request(URL, AUTH, session, "resources/list", {})
    # An unknown tool is an error result: the model reads it as text.
    out = await mcp.call_tool(URL, AUTH, session, "nope", {})
    assert out.startswith("The tool reported an error:")
    assert "no tool nope" in out
    await mcp.aclose()


async def test_the_url_policy_holds_for_each_request() -> None:
    fake = FakeMcp()
    mcp = client(fake)
    with pytest.raises(McpError, match="URL policy"):
        await mcp.open("https://private.remote.test/mcp", AUTH)
    with pytest.raises(McpError, match="URL policy"):
        await mcp.open("http://mcp.remote.test/mcp", AUTH)
    assert not fake.seen
    await mcp.aclose()


def test_parse_tool_applies_the_limits() -> None:
    assert parse_tool({"name": ""}) is None
    assert parse_tool("x") is None
    long = parse_tool({"name": "a", "description": "x" * 5000, "inputSchema": {"type": "array"}})
    assert long is not None
    assert len(long.description) == MAX_DESCRIPTION_CHARS + 3
    assert long.input_schema == {"type": "object"}
    huge = parse_tool(
        {"name": "b", "inputSchema": {"type": "object", "properties": {"x": "y" * 30_000}}}
    )
    assert huge is not None
    assert huge.input_schema == {"type": "object"}
    hinted = parse_tool({"name": "c", "annotations": {"title": "C", "readOnlyHint": True}})
    assert hinted is not None
    assert (hinted.title, hinted.read_only) == ("C", True)
    assert RemoteTool.from_stored(hinted.stored()) == hinted
    assert RemoteTool.from_stored({"name": 1}) is None


def test_names_access_and_filters() -> None:
    assert sama_name("my-server", "get.thing") == "mcp_my_server_get_thing"
    assert sama_name("x", "y" * 70) is None
    tools = [parse_tool(tool(n)) for n in ("get_a", "get_b", "drop_a", "get_a")]
    remote = [t for t in tools if t is not None]
    assert tool_access(remote[0], {}) == "destructive"
    assert tool_access(RemoteTool("r", "", {}, read_only=True), {}) == "read"
    assert tool_access(RemoteTool("w", "", {}, destructive=False), {}) == "write"
    assert tool_access(remote[2], {"drop_*": "write"}) == "write"
    exposed = exposed_tools("srv", remote, include=("get_*",), exclude=("*_b",))
    # The second ``get_a`` has the same sama name, so it is not in the list.
    assert [e.name for e in exposed] == ["mcp_srv_get_a"]
    assert tools_hash(remote) == tools_hash(list(remote))
    assert tools_hash(remote) != tools_hash(remote[:1])


def test_render_result_gives_text() -> None:
    result = {
        "content": [
            {"type": "text", "text": "one"},
            {"type": "image", "mimeType": "image/png", "data": "AAA"},
            {"type": "resource_link", "name": "doc", "uri": "file:///doc"},
            {"type": "resource", "resource": {"uri": "x://1", "text": "inline"}},
            {"type": "resource", "resource": {"uri": "x://2", "blob": "AAA"}},
        ]
    }
    text = render_result(result)
    assert text.splitlines() == [
        "one",
        "[image (image/png) not shown]",
        "[resource doc: file:///doc]",
        "inline",
        "[resource x://2 not shown]",
    ]
    assert json.loads(render_result({"structuredContent": {"a": 1}})) == {"a": 1}
    assert render_result({}) == "(the tool gave no content)"
    with pytest.raises(McpError):
        render_result([1])


def test_answer_of_finds_the_answer_in_a_stream() -> None:
    message = {"jsonrpc": "2.0", "id": 4, "result": {"ok": True}}
    other = {"jsonrpc": "2.0", "id": 3, "result": {}}
    body = f"data: {json.dumps(other)}\r\n\r\ndata: not json\n\ndata: {json.dumps(message)}"
    assert answer_of(body.encode(), "text/event-stream", 4) == message
    assert answer_of(json.dumps([other, message]).encode(), "application/json", 4) == message
    assert answer_of(b"{", "application/json", 4) is None
