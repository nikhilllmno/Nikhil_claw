"""The deferred tool catalog and the tool search, call, and archive routes (Section 10.3)."""

from __future__ import annotations

import secrets
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from sama.gate import GateStep, Verdict
from sama.runtime.archive import (
    MAX_ARCHIVE_BODY_BYTES,
    ArchiveAnswer,
    ArchiveEntry,
    TranscriptArchive,
)
from sama.tenancy import RunTokenClaims, RunTokenSigner
from sama.tools import MemoryTodoStore, ToolServer
from sama.tools.deferred import (
    DeferredCatalog,
    DeferredTool,
    format_describe,
    format_search,
    missing_arguments,
)
from tests.conformance.scripted_model import serve
from tests.support import make_scope
from tests.tools.test_server import connect, text_of


async def echo(claims: RunTokenClaims, arguments: Mapping[str, Any]) -> str:
    return f"sent {arguments.get('to')} for {claims.run_id}"


SEND_SCHEMA = {
    "type": "object",
    "properties": {"to": {"type": "string"}},
    "required": ["to"],
}


def mail_tools() -> list[DeferredTool]:
    return [
        DeferredTool(
            "mail_send", "Send an email message.", "mail", echo, SEND_SCHEMA, tags=("gmail",)
        ),
        DeferredTool("mail_read", "Read the inbox.", "mail", echo, read_only=True),
        DeferredTool("speak", "Turn text into speech audio.", "media", echo, tags=("tts",)),
    ]


# -- the catalog -------------------------------------------------------------------------------


def test_the_catalog_groups_tools_by_toolset() -> None:
    catalog = DeferredCatalog(mail_tools())
    assert len(catalog) == 3
    assert catalog.toolsets() == {
        "mail": frozenset({"mail_send", "mail_read"}),
        "media": frozenset({"speak"}),
    }
    assert catalog.names() == {"mail_send", "mail_read", "speak"}
    catalog.unregister("speak")
    assert catalog.get("speak") is None


def test_a_name_can_be_registered_once() -> None:
    catalog = DeferredCatalog(mail_tools())
    with pytest.raises(ValueError, match="already registered"):
        catalog.register(mail_tools()[0])


@pytest.mark.parametrize(
    "name",
    [
        "",
        "1tool",
        "has space",
        "x" * 65,
        "web_search",
        "tool_call",
        "todo",
        "mcp__sama__mail",
    ],
)
def test_a_tool_cannot_take_a_bad_or_reserved_name(name: str) -> None:
    with pytest.raises(ValueError, match=r".+"):
        DeferredTool(name, "d", "mail", echo)


def test_a_tool_needs_a_toolset_an_object_schema_and_a_short_description() -> None:
    with pytest.raises(ValueError, match="toolset"):
        DeferredTool("t1", "d", " ", echo)
    with pytest.raises(ValueError, match="object"):
        DeferredTool("t1", "d", "mail", echo, {"type": "string"})
    with pytest.raises(ValueError, match="characters"):
        DeferredTool("t1", "d" * 2001, "mail", echo)


def test_the_search_ranks_by_name_tags_and_description() -> None:
    catalog = DeferredCatalog(mail_tools())
    allowed = catalog.names()
    assert [t.name for t in catalog.search("send email", allowed)] == ["mail_send"]
    assert [t.name for t in catalog.search("gmail", allowed)] == ["mail_send"]
    assert [t.name for t in catalog.search("tts audio", allowed)] == ["speak"]
    assert catalog.search("nothing like it", allowed) == []
    # No query: the tools by name.
    assert [t.name for t in catalog.search(None, allowed)] == ["mail_read", "mail_send", "speak"]
    assert [t.name for t in catalog.search("", allowed, limit=1)] == ["mail_read"]


def test_the_search_shows_only_allowed_tools() -> None:
    catalog = DeferredCatalog(mail_tools())
    assert [t.name for t in catalog.search("mail", {"mail_read"})] == ["mail_read"]
    assert catalog.search("mail", set()) == []


def test_required_arguments_are_checked() -> None:
    tool = mail_tools()[0]
    assert missing_arguments(tool, {}) == ["to"]
    assert missing_arguments(tool, {"to": "a"}) == []
    assert missing_arguments(mail_tools()[1], {}) == []


def test_the_texts_for_the_model() -> None:
    tools = mail_tools()
    assert format_search([], query="x") == "No tool matches."
    assert "no tools" in format_search([], query=None)
    listing = format_search(tools[:2], query="mail")
    assert "- mail_send: Send an email message." in listing
    described = format_describe(tools[0])
    assert "mail_send (can change things)" in described
    assert '"required"' in described
    assert "reads only" in format_describe(tools[1])


# -- the tools of the server -------------------------------------------------------------------


@dataclass
class VisibleGate:
    """A gate that allows, with the visible tools of the run."""

    verdict: Verdict = field(default_factory=lambda: Verdict("allow", GateStep.POLICY, "ok"))
    visible: frozenset[str] = frozenset({"mail_send", "mail_read", "speak"})
    screens: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def check(
        self, claims: RunTokenClaims, call_id: str, tool_name: str, arguments: Mapping[str, Any]
    ) -> Verdict:
        return self.verdict

    async def screen(
        self, claims: RunTokenClaims, tool_name: str, arguments: Mapping[str, Any]
    ) -> Verdict:
        self.screens.append((tool_name, dict(arguments)))
        return self.verdict

    async def visible_tools(self, claims: RunTokenClaims) -> frozenset[str]:
        return self.visible


@dataclass
class Rig:
    base: str
    signer: RunTokenSigner
    gate: VisibleGate

    def token(self) -> str:
        return self.signer.issue(make_scope("run-7"), ttl_s=600)


@pytest.fixture
def rig() -> Iterator[Rig]:
    signer = RunTokenSigner(secrets.token_bytes(32))
    gate = VisibleGate()
    tools = ToolServer(
        signer=signer,
        todos=MemoryTodoStore(),
        deferred=DeferredCatalog(mail_tools()),
        gate=gate,
    )
    with serve(tools.app()) as base:
        yield Rig(base, signer, gate)


async def test_the_server_lists_the_three_tools(rig: Rig) -> None:
    async with connect(rig.base + "/mcp", rig.token()) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert {"tool_search", "tool_describe", "tool_call"} <= names


async def test_an_empty_catalog_has_no_search_tools() -> None:
    signer = RunTokenSigner(secrets.token_bytes(32))
    tools = ToolServer(signer=signer, todos=MemoryTodoStore(), deferred=DeferredCatalog())
    with serve(tools.app()) as base:
        async with connect(base + "/mcp", signer.issue(make_scope(), ttl_s=600)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
    assert not {"tool_search", "tool_describe", "tool_call"} & names


async def test_tool_search_and_describe_find_a_tool(rig: Rig) -> None:
    async with connect(rig.base + "/mcp", rig.token()) as client:
        found = await client.call_tool("tool_search", {"query": "send email"})
        described = await client.call_tool("tool_describe", {"name": "mail_send"})
    assert "mail_send" in text_of(found)
    assert "mail_read" not in text_of(found)
    assert '"required"' in text_of(described)


async def test_tool_search_hides_the_tools_that_the_run_cannot_use(rig: Rig) -> None:
    rig.gate.visible = frozenset({"mail_read"})
    async with connect(rig.base + "/mcp", rig.token()) as client:
        found = await client.call_tool("tool_search", {"query": "mail"})
        hidden = await client.call_tool("tool_describe", {"name": "mail_send"})
        unknown = await client.call_tool("tool_describe", {"name": "nope"})
    assert "mail_send" not in text_of(found)
    assert hidden.is_error
    assert unknown.is_error
    # The two errors are the same: a run does not learn that a hidden tool exists.
    assert text_of(hidden).replace("mail_send", "X") == text_of(unknown).replace("nope", "X")


async def test_tool_call_runs_the_tool_after_the_gate_screen(rig: Rig) -> None:
    async with connect(rig.base + "/mcp", rig.token()) as client:
        result = await client.call_tool(
            "tool_call", {"name": "mail_send", "arguments": {"to": "a@b.c"}}
        )
    assert not result.is_error
    assert text_of(result) == "sent a@b.c for run-7"
    # The gate got the call as the client sent it: it judges the tool that the call names.
    assert rig.gate.screens == [("tool_call", {"name": "mail_send", "arguments": {"to": "a@b.c"}})]


async def test_tool_call_does_not_run_a_tool_that_the_gate_denies(rig: Rig) -> None:
    rig.gate.verdict = Verdict("deny", GateStep.POLICY, "mail_send is blocked")
    async with connect(rig.base + "/mcp", rig.token()) as client:
        result = await client.call_tool(
            "tool_call", {"name": "mail_send", "arguments": {"to": "a@b.c"}}
        )
    assert result.is_error
    assert "the tool gate denied the call: mail_send is blocked" in text_of(result)
    assert "sent" not in text_of(result)


async def test_tool_call_with_an_unknown_tool_or_missing_arguments_is_an_error(rig: Rig) -> None:
    async with connect(rig.base + "/mcp", rig.token()) as client:
        unknown = await client.call_tool("tool_call", {"name": "web_search", "arguments": {}})
        missing = await client.call_tool("tool_call", {"name": "mail_send"})
    assert unknown.is_error
    assert "no tool has the name web_search" in text_of(unknown)
    assert missing.is_error
    assert "missing arguments: to" in text_of(missing)


async def test_tool_call_redacts_the_result() -> None:
    async def leaks(claims: RunTokenClaims, arguments: Mapping[str, Any]) -> str:
        return "key sk-ant-api03-" + "a" * 40

    signer = RunTokenSigner(secrets.token_bytes(32))
    tools = ToolServer(
        signer=signer,
        todos=MemoryTodoStore(),
        deferred=DeferredCatalog([DeferredTool("leaky", "Leaks.", "x", leaks)]),
    )
    with serve(tools.app()) as base:
        async with connect(base + "/mcp", signer.issue(make_scope(), ttl_s=600)) as client:
            result = await client.call_tool("tool_call", {"name": "leaky"})
    assert "sk-ant-api03-" + "a" * 40 not in text_of(result)


# -- the archive route -------------------------------------------------------------------------


class FakeArchive:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.bodies: list[tuple[str, TranscriptArchive]] = []

    async def archive(self, claims: RunTokenClaims, body: TranscriptArchive) -> ArchiveAnswer:
        if self.error is not None:
            raise self.error
        self.bodies.append((claims.run_id, body))
        return ArchiveAnswer(entries=len(body.entries))


def archive_rig(archive: Any) -> tuple[ToolServer, RunTokenSigner]:
    signer = RunTokenSigner(secrets.token_bytes(32))
    return ToolServer(signer=signer, todos=MemoryTodoStore(), archive=archive), signer


async def post(base: str, token: str | None, body: Any = None, raw: bytes | None = None) -> Any:
    headers = {"content-type": "application/json"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient() as http:
        if raw is not None:
            return await http.post(base + "/archive", headers=headers, content=raw)
        return await http.post(base + "/archive", headers=headers, json=body)


GOOD = {"trigger": "manual", "entries": [{"role": "user", "text": "hello"}]}


async def test_the_archive_route_stores_the_transcript() -> None:
    archive = FakeArchive()
    tools, signer = archive_rig(archive)
    with serve(tools.app()) as base:
        response = await post(base, signer.issue(make_scope("run-7"), ttl_s=600), GOOD)
    assert response.status_code == 200
    assert response.json() == {"entries": 1, "truncated": False}
    assert archive.bodies == [
        (
            "run-7",
            TranscriptArchive(trigger="manual", entries=[ArchiveEntry(role="user", text="hello")]),
        )
    ]


@pytest.mark.parametrize("token", [None, "nope"])
async def test_the_archive_route_needs_a_run_token(token: str | None) -> None:
    archive = FakeArchive()
    tools, _signer = archive_rig(archive)
    with serve(tools.app()) as base:
        response = await post(base, token, GOOD)
    assert response.status_code == 401
    assert archive.bodies == []


async def test_the_archive_route_answers_503_without_a_store() -> None:
    tools, signer = archive_rig(None)
    with serve(tools.app()) as base:
        assert (await post(base, signer.issue(make_scope(), ttl_s=600), GOOD)).status_code == 503


@pytest.mark.parametrize(
    "body",
    [
        {"trigger": "other", "entries": []},
        {"trigger": "auto", "entries": [{"role": "robot", "text": "x"}]},
        {"trigger": "auto", "entries": [], "extra": 1},
        [1],
    ],
)
async def test_a_bad_archive_gets_422(body: Any) -> None:
    tools, signer = archive_rig(FakeArchive())
    with serve(tools.app()) as base:
        response = await post(base, signer.issue(make_scope(), ttl_s=600), body)
    assert response.status_code == 422


async def test_a_large_archive_gets_413() -> None:
    tools, signer = archive_rig(FakeArchive())
    raw = b'{"pad": "' + b"x" * MAX_ARCHIVE_BODY_BYTES + b'"}'
    with serve(tools.app()) as base:
        response = await post(base, signer.issue(make_scope(), ttl_s=600), raw=raw)
    assert response.status_code == 413


async def test_an_archive_error_is_409_and_a_crash_is_503_without_details() -> None:
    from sama.context import ArchiveError

    tools, signer = archive_rig(FakeArchive(ArchiveError("the run is not running")))
    with serve(tools.app()) as base:
        response = await post(base, signer.issue(make_scope(), ttl_s=600), GOOD)
    assert response.status_code == 409
    assert response.json() == {"error": "the run is not running"}
    tools, signer = archive_rig(FakeArchive(RuntimeError("database password is hunter2")))
    with serve(tools.app()) as base:
        response = await post(base, signer.issue(make_scope(), ttl_s=600), GOOD)
    assert response.status_code == 503
    assert "hunter2" not in response.text
