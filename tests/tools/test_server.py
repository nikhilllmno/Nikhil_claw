"""The sama MCP tool server over streamable HTTP (docs/architecture.md, Section 16.7)."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
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

from sama.skills import MemorySkillStore
from sama.tenancy import RunTokenSigner
from sama.tools import ClarifyBroker, MemoryTodoStore, OutboundClient, SearchHit, ToolServer
from sama.tools.outbound import IPAddress
from sama.tools.output import MAX_OUTPUT_BYTES
from sama.tools.server import MAX_ERROR_CHARS, quiet_dependency_logs
from tests.conformance.scripted_model import serve
from tests.skills.helpers import actor, doc, md
from tests.support import RecordingSpill, make_scope, wire_transport


@dataclass
class FakeSearch:
    hits: list[SearchHit] = field(default_factory=list)
    queries: list[tuple[str, int]] = field(default_factory=list)

    async def search(self, query: str, limit: int, *, org_id: str) -> list[SearchHit]:
        self.queries.append((query, limit))
        return self.hits[:limit]


@dataclass
class World:
    url: str
    signer: RunTokenSigner
    todos: MemoryTodoStore
    search: FakeSearch
    broker: ClarifyBroker
    spill: RecordingSpill

    def token(self, run_id: str = "run-1", *, session: str = "sess-1") -> str:
        scope = make_scope(run_id).model_copy(update={"session_id": session})
        return self.signer.issue(scope, ttl_s=600)


def _no_network() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="x" * 70_000)

    return wire_transport(handler)


async def _public(host: str, port: int) -> Sequence[IPAddress]:
    return [ipaddress.ip_address("93.184.215.14")]


@pytest.fixture
def world() -> Iterator[World]:
    signer = RunTokenSigner(secrets.token_bytes(32))
    todos = MemoryTodoStore()
    search = FakeSearch([SearchHit(title="Sama", url="https://sama.example", snippet="An agent")])
    broker = ClarifyBroker(wait_s=5)
    spill = RecordingSpill()
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


async def test_a_long_argument_error_is_cut(world: World) -> None:
    bad = [{"id": "x" * 500, "content": "y" * 500, "status": "nope"}] * 100
    async with connect(world.url, world.token()) as client:
        result = await client.call_tool("todo", {"todos": bad})
    assert result.is_error
    text = text_of(result)
    assert len(text) <= MAX_ERROR_CHARS + len(" [cut]")
    assert text.endswith(" [cut]")


async def test_a_disconnect_ends_a_waiting_clarify_call(world: World) -> None:
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "clarify",
            "arguments": {"questions": [{"question": "Which?", "choices": ["A", "B"]}]},
        },
    }
    headers = {
        "authorization": f"Bearer {world.token()}",
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }

    async def post() -> None:
        async with httpx.AsyncClient(timeout=30) as http:
            await http.post(world.url, headers=headers, json=call)

    posting = asyncio.create_task(post())
    for _ in range(200):
        if world.broker.pending("org-1", "sess-1"):
            break
        await asyncio.sleep(0.02)
    assert world.broker.pending("org-1", "sess-1")
    posting.cancel()
    for _ in range(200):
        if not world.broker.pending("org-1", "sess-1"):
            break
        await asyncio.sleep(0.02)
    assert world.broker.pending("org-1", "sess-1") == []


def test_dependency_logs_keep_only_warnings() -> None:
    names = ("httpx", "httpcore", "mcp")
    before = {n: logging.getLogger(n).level for n in names}
    try:
        quiet_dependency_logs()
        for name in names:
            assert not logging.getLogger(name).isEnabledFor(logging.INFO)
            assert logging.getLogger(name + ".child").getEffectiveLevel() == logging.WARNING
    finally:
        for name, level in before.items():
            logging.getLogger(name).setLevel(level)


async def test_the_tool_log_has_no_arguments(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="sama.tools")
    async with connect(world.url, world.token()) as client:
        await client.call_tool("web_search", {"query": "private words", "limit": 2})
        await client.call_tool("todo", {"todos": [{"id": "secret-id"}]})
    lines = [r.getMessage() for r in caplog.records if r.name == "sama.tools"]
    assert any("tool=web_search" in line and "ok=True" in line for line in lines)
    assert any("tool=todo" in line and "ok=False" in line for line in lines)
    assert not any("private words" in line or "secret-id" in line for line in lines)


@asynccontextmanager
async def skills_client(store: MemorySkillStore) -> AsyncIterator[Client]:
    signer = RunTokenSigner(secrets.token_bytes(32))
    server = ToolServer(signer=signer, todos=MemoryTodoStore(), skills=store)
    with serve(server.app()) as base:
        token = signer.issue(make_scope(), ttl_s=600)
        async with connect(base + "/mcp", token) as client:
            yield client


async def test_the_skill_tools_are_listed_with_a_store() -> None:
    store = MemorySkillStore(actor())
    async with skills_client(store) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert {"skills_list", "skill_view", "skill_manage"} <= set(tools)
    assert tools["skills_list"].annotations is not None
    assert tools["skills_list"].annotations.read_only_hint is True
    assert tools["skill_view"].annotations is not None
    assert tools["skill_view"].annotations.read_only_hint is True


async def test_the_skill_tools_create_list_view_and_patch() -> None:
    store = MemorySkillStore(actor(), {"base": doc("base", description="Builtin base")})
    async with skills_client(store) as client:
        created = await client.call_tool(
            "skill_manage", {"action": "create", "name": "notes", "content": md("notes", "Notes")}
        )
        assert not created.is_error
        assert "version 1" in text_of(created)
        listed = text_of(await client.call_tool("skills_list", {}))
        assert "- base (builtin, v1): Builtin base" in listed
        assert "- notes (user, v1): Notes" in listed
        assert "notes" not in text_of(await client.call_tool("skills_list", {"query": "base"}))
        await client.call_tool(
            "skill_manage",
            {
                "action": "write_file",
                "name": "notes",
                "file_path": "references/a.md",
                "file_content": "File A",
            },
        )
        viewed = text_of(await client.call_tool("skill_view", {"name": "notes"}))
        assert "Skill: notes (user, v2)" in viewed
        assert "Files: references/a.md" in viewed
        assert "Do it." in viewed
        file = await client.call_tool(
            "skill_view", {"name": "notes", "file_path": "references/a.md"}
        )
        assert text_of(file) == "File A"
        patched = await client.call_tool(
            "skill_manage",
            {"action": "patch", "name": "notes", "old_string": "Do it.", "new_string": "Done."},
        )
        assert "version 3" in text_of(patched)
        assert "Done." in text_of(await client.call_tool("skill_view", {"name": "notes"}))


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"action": "create", "name": "ours", "scope": "workspace"}, "admin"),
        ({"action": "create", "name": "all", "scope": "org"}, "org owner"),
        ({"action": "edit", "name": "base"}, "read-only"),
        ({"action": "archive", "name": "missing"}, "no skill"),
        ({"action": "create", "name": "Bad Name"}, "name"),
    ],
)
async def test_a_refused_skill_change_is_a_tool_error(
    arguments: dict[str, str], expected: str
) -> None:
    store = MemorySkillStore(actor(), {"base": doc("base")})
    arguments.setdefault("content", md(arguments["name"].lower().replace(" ", "-")))
    async with skills_client(store) as client:
        result = await client.call_tool("skill_manage", arguments)
    assert result.is_error
    assert expected in text_of(result)


async def test_viewing_an_unknown_skill_or_file_is_a_tool_error() -> None:
    store = MemorySkillStore(actor(), {"base": doc("base")})
    async with skills_client(store) as client:
        unknown = await client.call_tool("skill_view", {"name": "nope"})
        no_file = await client.call_tool("skill_view", {"name": "base", "file_path": "x.md"})
    assert unknown.is_error
    assert no_file.is_error
    assert "no file x.md" in text_of(no_file)


class FakeChanges:
    """Records the change proposals; a soul with the word ``bad`` is refused."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def propose(self, claims: object, request: object) -> str:
        from sama.review.soul import SoulError

        if "bad" in (getattr(request, "content", None) or ""):
            raise SoulError("the soul contains a secret. Remove it")
        self.calls.append((getattr(claims, "run_id", ""), request))
        return f"Proposed a change of the {getattr(request, 'target', '')}."


async def test_change_propose_is_hidden_without_a_proposer() -> None:
    signer = RunTokenSigner(secrets.token_bytes(32))
    server = ToolServer(signer=signer, todos=MemoryTodoStore())
    names = {tool.name for tool in await server.mcp().list_tools()}
    assert "change_propose" not in names


async def test_change_propose_sends_the_change_to_the_proposer() -> None:
    from sama.review.change import ChangeRequest

    signer = RunTokenSigner(secrets.token_bytes(32))
    changes = FakeChanges()
    server = ToolServer(signer=signer, todos=MemoryTodoStore(), changes=changes)  # type: ignore[arg-type]
    with serve(server.app()) as base:
        token = signer.issue(make_scope("run-7"), ttl_s=600)
        async with connect(base + "/mcp", token) as client:
            ok = await client.call_tool(
                "change_propose", {"target": "soul", "content": "Be kind.", "summary": "kind"}
            )
            refused = await client.call_tool(
                "change_propose", {"target": "soul", "content": "bad", "summary": "x"}
            )
            policy = await client.call_tool(
                "change_propose",
                {
                    "target": "policy",
                    "summary": "ask first",
                    "rules": [{"tool": "web_*", "action": "ask"}],
                },
            )
            tools = await client.call_tool(
                "change_propose",
                {"target": "toolsets", "summary": "web", "toolsets": ["files", "web"]},
            )
            wrong_target = await client.call_tool(
                "change_propose", {"target": "other", "summary": "x"}
            )
            wrong_action = await client.call_tool(
                "change_propose",
                {"target": "policy", "summary": "x", "rules": [{"tool": "a", "action": "maybe"}]},
            )
    assert not ok.is_error
    assert text_of(ok) == "Proposed a change of the soul."
    assert refused.is_error
    assert "secret" in text_of(refused)
    assert not policy.is_error
    assert not tools.is_error
    assert wrong_target.is_error
    assert wrong_action.is_error
    assert [run for run, _ in changes.calls] == ["run-7", "run-7", "run-7"]
    soul, rules, toolsets = (request for _, request in changes.calls)
    assert soul == ChangeRequest(target="soul", summary="kind", content="Be kind.")
    assert rules == ChangeRequest(target="policy", summary="ask first", rules=[("web_*", "ask")])
    assert toolsets == ChangeRequest(target="toolsets", summary="web", toolsets=["files", "web"])
    assert toolsets.fields() == {"toolsets": ["files", "web"]}
    assert rules.fields() == {"rules": [["web_*", "ask"]]}


async def test_the_hermes_parity_tools_show_with_their_services() -> None:
    """M7.5 (Section 10.2.2): each tool shows only when its service exists."""
    import httpx as _httpx

    from sama.media.meter import MediaMeter
    from sama.media.video import VideoService
    from sama.memory.provider import Mem0Provider, MemoryRecall
    from sama.moa import MixtureService
    from sama.modelproxy.prices import PriceTable
    from sama.modelproxy.store import MemoryUsageStore
    from sama.tools.x import XSearch
    from sama.vault.store import OrgKeys

    keys = OrgKeys(None, {})
    meter = MediaMeter(usage=MemoryUsageStore(), prices=PriceTable.load())
    signer = RunTokenSigner(secrets.token_bytes(32))

    def refuse(request: _httpx.Request) -> _httpx.Response:
        raise AssertionError("no provider call without a key")

    http = _httpx.AsyncClient(transport=_httpx.MockTransport(refuse))
    server = ToolServer(
        signer=signer,
        todos=MemoryTodoStore(),
        video=VideoService(keys=keys, meter=meter, http=http),
        x=XSearch(keys, http=http),
        moa=MixtureService(keys=keys, meter=meter, http=http),
        recall=MemoryRecall(None, Mem0Provider(keys, http=http)),  # type: ignore[arg-type]
    )
    names = {tool.name for tool in await server.mcp().list_tools()}
    assert {"video_analyze", "x_search", "moa", "memory_recall"} <= names
    with serve(server.app()) as base:
        token = signer.issue(make_scope(), ttl_s=600)
        async with connect(base + "/mcp", token) as client:
            x = await client.call_tool("x_search", {"query": "sama"})
            moa = await client.call_tool("moa", {"question": "why?"})
            video = await client.call_tool("video_analyze", {"source": "https://youtu.be/abc"})
    assert "no X API token" in x.content[0].text  # type: ignore[union-attr]
    assert "at least two" in moa.content[0].text  # type: ignore[union-attr]
    assert "no Gemini key" in video.content[0].text  # type: ignore[union-attr]
    await server.aclose()
    plain = {
        tool.name
        for tool in await ToolServer(signer=signer, todos=MemoryTodoStore()).mcp().list_tools()
    }
    assert not {"video_analyze", "x_search", "moa", "memory_recall"} & plain
