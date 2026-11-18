"""The sama MCP tool server over streamable HTTP (docs/architecture.md, Section 16.7)."""

from __future__ import annotations

import asyncio
import ipaddress
import secrets
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult, TextContent

from sama.tenancy import RunTokenSigner
from sama.tools import ClarifyBroker, MemoryTodoStore, OutboundClient, SearchHit, ToolServer
from sama.tools.outbound import IPAddress
from sama.tools.output import MAX_OUTPUT_BYTES
from tests.conformance.scripted_model import serve
from tests.support import make_scope


@dataclass
class FakeSearch:
    hits: list[SearchHit] = field(default_factory=list)
    queries: list[tuple[str, int]] = field(default_factory=list)

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        self.queries.append((query, limit))
        return self.hits[:limit]


@dataclass
class FakeSpill:
    files: dict[str, bytes] = field(default_factory=dict)

    async def save(self, claims: object, name: str, data: bytes) -> str | None:
        path = f"/work/.sama/spill/{name}"
        self.files[path] = data
        return path


@dataclass
class World:
    url: str
    signer: RunTokenSigner
    todos: MemoryTodoStore
    search: FakeSearch
    broker: ClarifyBroker
    spill: FakeSpill

    def token(self, run_id: str = "run-1", *, session: str = "sess-1") -> str:
        scope = make_scope(run_id).model_copy(update={"session_id": session})
        return self.signer.issue(scope, ttl_s=600)


def _no_network() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="x" * 70_000)

    return httpx.MockTransport(handler)


async def _public(host: str, port: int) -> Sequence[IPAddress]:
    return [ipaddress.ip_address("93.184.215.14")]


@pytest.fixture
def world() -> Iterator[World]:
    signer = RunTokenSigner(secrets.token_bytes(32))
    todos = MemoryTodoStore()
    search = FakeSearch([SearchHit(title="Sama", url="https://sama.example", snippet="An agent")])
    broker = ClarifyBroker(wait_s=5)
    spill = FakeSpill()
    server = ToolServer(
        signer=signer,
        todos=todos,
        outbound=OutboundClient(resolver=_public, transport=_no_network()),
        search=search,
        clarify=broker,
        spill=spill,
    )
    with serve(server.app()) as base:
        yield World(base + "/mcp", signer, todos, search, broker, spill)


@asynccontextmanager
async def connect(url: str, token: str) -> AsyncIterator[Client]:
    http = httpx2.AsyncClient(headers={"authorization": f"Bearer {token}"}, timeout=30)
    async with http, Client(streamable_http_client(url, http_client=http)) as client:
        yield client


def text_of(result: CallToolResult) -> str:
    return "\n".join(c.text for c in result.content if isinstance(c, TextContent))


async def test_health_needs_no_token(world: World) -> None:
    async with httpx.AsyncClient() as http:
        response = await http.get(world.url.removesuffix("/mcp") + "/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


@pytest.mark.parametrize("header", [None, "Bearer nope", "Basic abc", "Bearer"])
async def test_a_request_without_a_valid_run_token_gets_401(
    world: World, header: str | None
) -> None:
    headers = {"content-type": "application/json", "accept": "application/json"}
    if header is not None:
        headers["authorization"] = header
    async with httpx.AsyncClient() as http:
        response = await http.post(
            world.url,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    assert response.status_code == 401
    assert "run token" in response.json()["error"]


async def test_the_tools_are_listed(world: World) -> None:
    async with connect(world.url, world.token()) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"web_search", "web_extract", "clarify", "todo"}
    assert tools["web_search"].annotations is not None
    assert tools["web_search"].annotations.read_only_hint is True


async def test_tools_without_a_service_are_hidden() -> None:
    signer = RunTokenSigner(secrets.token_bytes(32))
    server = ToolServer(signer=signer, todos=MemoryTodoStore())
    with serve(server.app()) as base:
        token = signer.issue(make_scope(), ttl_s=600)
        async with connect(base + "/mcp", token) as client:
            names = {t.name for t in (await client.list_tools()).tools}
    assert names == {"web_extract", "todo"}


async def test_todo_is_scoped_by_the_run_token(world: World) -> None:
    async with connect(world.url, world.token()) as client:
        written = await client.call_tool(
            "todo",
            {
                "todos": [
                    {"id": "1", "content": "Plan", "status": "in_progress"},
                    {"id": "2", "content": "Build"},
                ]
            },
        )
        assert not written.is_error
        assert "[>] 1: Plan" in text_of(written)
        merged = await client.call_tool(
            "todo", {"todos": [{"id": "1", "status": "completed"}], "merge": True}
        )
        assert "[x] 1: Plan" in text_of(merged)
        assert "[ ] 2: Build" in text_of(merged)
    # A run in another session cannot see the list.
    async with connect(world.url, world.token("run-2", session="sess-2")) as client:
        other = await client.call_tool("todo", {})
    assert text_of(other) == "The task list is empty."
    # A later run of the same session sees it.
    async with connect(world.url, world.token("run-3")) as client:
        later = await client.call_tool("todo", {})
    assert "[x] 1: Plan" in text_of(later)


async def test_a_bad_todo_change_is_a_tool_error(world: World) -> None:
    async with connect(world.url, world.token()) as client:
        result = await client.call_tool("todo", {"todos": [{"id": "1"}]})
    assert result.is_error
    assert "needs content" in text_of(result)


async def test_web_search_wraps_results_as_untrusted(world: World) -> None:
    async with connect(world.url, world.token()) as client:
        result = await client.call_tool("web_search", {"query": "sama agent", "limit": 3})
    assert not result.is_error
    text = text_of(result)
    assert text.startswith('<untrusted_content source="web_search">')
    assert "https://sama.example" in text
    assert world.search.queries == [("sama agent", 3)]


async def test_web_search_checks_the_limit(world: World) -> None:
    async with connect(world.url, world.token()) as client:
        result = await client.call_tool("web_search", {"query": "x", "limit": 11})
    assert result.is_error
    assert world.search.queries == []


async def test_web_extract_blocks_a_private_address(world: World) -> None:
    async with connect(world.url, world.token()) as client:
        result = await client.call_tool("web_extract", {"urls": ["https://10.0.0.1/admin"]})
    assert not result.is_error
    assert "not a public address" in text_of(result)


async def test_a_large_result_spills(world: World) -> None:
    async with connect(world.url, world.token()) as client:
        result = await client.call_tool("web_extract", {"urls": ["https://big.example/"]})
    text = text_of(result)
    assert len(text.encode()) < MAX_OUTPUT_BYTES
    [(path, data)] = world.spill.files.items()
    assert path.startswith("/work/.sama/spill/web_extract-")
    assert f"The full output is in the file {path}" in text
    assert len(data) > 70_000


async def test_clarify_waits_for_the_answer(world: World) -> None:
    async def answer() -> None:
        for _ in range(200):
            pending = world.broker.pending("org-1", "sess-1")
            if pending:
                world.broker.answer("org-1", pending[0].id, ["Blue"])
                return
            await asyncio.sleep(0.02)

    async with connect(world.url, world.token()) as client:
        answering = asyncio.create_task(answer())
        result = await client.call_tool(
            "clarify",
            {"questions": [{"question": "Which color?", "choices": ["Red", "Blue"]}]},
        )
        await answering
    assert not result.is_error
    assert "Answer: Blue" in text_of(result)


async def test_clarify_refuses_a_single_choice(world: World) -> None:
    async with connect(world.url, world.token()) as client:
        result = await client.call_tool(
            "clarify", {"questions": [{"question": "Sure?", "choices": ["Yes"]}]}
        )
    assert result.is_error
    assert "2 or more choices" in text_of(result)
