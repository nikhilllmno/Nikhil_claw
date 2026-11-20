"""``@`` references in a message (docs/architecture.md, Section 6.4; roadmap M4.9)."""

from __future__ import annotations

import asyncio
import ipaddress
import uuid
from collections.abc import Sequence
from typing import cast

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Message
from sama.orchestrator.queue import ClaimedRun
from sama.orchestrator.references import (
    HEADER,
    MAX_FILE_BYTES,
    MAX_REFERENCE_CHARS,
    MAX_REFERENCES,
    Reference,
    ReferenceExpander,
    find_references,
)
from sama.runtime.contract import RunCompleted, RunRequest
from sama.runtime.fake import FakeScript
from sama.sandbox.base import ExecResult, Sandbox, SandboxFileTooLargeError
from sama.search.service import SessionSearch
from sama.tools.outbound import IPAddress, OutboundClient
from tests.orchestrator.conftest import Tenant
from tests.orchestrator.test_worker import Harness
from tests.support import wire_transport


async def _resolve(host: str, port: int) -> Sequence[IPAddress]:
    return [ipaddress.ip_address("10.0.0.1" if host == "private.test" else "93.184.215.14")]


def _page(request: httpx.Request) -> httpx.Response:
    if request.headers["host"] == "big.test":
        return httpx.Response(
            200, headers={"content-type": "text/plain"}, text="x" * (MAX_REFERENCE_CHARS * 2)
        )
    return httpx.Response(
        200, headers={"content-type": "text/html"}, text="<title>Plan</title><p>Ship it</p>"
    )


def outbound() -> OutboundClient:
    return OutboundClient(resolver=_resolve, transport=wire_transport(_page))


class FakeSandbox:
    workdir = "/work/"

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {
            "/work/notes/todo.md": b"buy milk",
            "/work/blob.bin": b"\xff\xfe\x00",
        }
        # A link in the workspace that points out of it.
        self.links = {"/work/outside/passwd": "/etc/passwd", "/work/up": "/etc"}
        self.reads: list[str] = []

    async def exec(self, argv: list[str], **_: object) -> ExecResult:
        assert argv[:3] == ["realpath", "-e", "--"]
        resolved = []
        for path in argv[3:]:
            path = self.links.get(path, path).rstrip("/")
            if path not in {"/work", "/work/huge.txt", "/etc", "/etc/passwd", *self.files}:
                return ExecResult(1, "", "No such file or directory")
            resolved.append(path)
        return ExecResult(0, "\n".join(resolved) + "\n", "")

    async def read_file(self, path: str, *, max_bytes: int | None = None) -> bytes:
        self.reads.append(path)
        if path == "/work/huge.txt":
            raise SandboxFileTooLargeError(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


def claimed(text: str, tenant: Tenant | None = None, session: str = "s1") -> ClaimedRun:
    return ClaimedRun(
        org_id=tenant.org if tenant else uuid.uuid4(),
        workspace_id=tenant.ids["ws"] if tenant else uuid.uuid4(),
        session_id=tenant.ids[session] if tenant else uuid.uuid4(),
        run_id=uuid.uuid4(),
        agent_profile_id=uuid.uuid4(),
        user_id=None,
        audience="shared",
        owner_user_id=None,
        runtime="fake",
        model="m",
        soul="",
        budget={},
        input=text,
        runtime_state=None,
        resume=None,
        attempt=1,
        next_seq=0,
    )


# -- parsing -----------------------------------------------------------------------------------


def test_find_references_reads_each_kind_once_without_trailing_punctuation() -> None:
    text = (
        "Compare @url:https://a.test/x, with @file:notes/todo.md and (@session:abc). "
        "Again @url:https://a.test/x. Mail me at mia@url:nothing or @other:thing."
    )
    assert find_references(text) == [
        Reference("url", "https://a.test/x"),
        Reference("file", "notes/todo.md"),
    ]
    assert find_references("@session:abc) and @url:") == [Reference("session", "abc")]
    assert find_references("no references here") == []


# -- reading -----------------------------------------------------------------------------------


async def test_a_message_without_references_stays_the_same() -> None:
    expander = ReferenceExpander()
    assert await expander.expand(claimed("hello @mia")) == "hello @mia"


async def test_a_url_reference_attaches_the_page_as_untrusted_content() -> None:
    client = outbound()
    try:
        text = await ReferenceExpander(outbound=client).expand(
            claimed("Summarize @url:https://page.test/plan")
        )
    finally:
        await client.aclose()
    message, attached = text.split("\n\n", 1)
    assert message == "Summarize @url:https://page.test/plan"
    assert attached.startswith(HEADER)
    assert "@url:https://page.test/plan\n<untrusted_content" in attached
    assert "Title: Plan\n\nShip it" in attached


async def test_a_url_follows_the_address_rules_and_a_long_page_is_cut() -> None:
    client = outbound()
    try:
        text = await ReferenceExpander(outbound=client).expand(
            claimed("@url:https://private.test/ @url:ftp://x.test/ @url:https://big.test/")
        )
    finally:
        await client.aclose()
    assert "error: private.test resolves to 10.0.0.1" in text
    assert "@url:ftp://x.test/\nerror: the URL must start with https:// or http://" in text
    assert "[The reference was larger than the limit. It ends here.]" in text
    # The cut block is closed again.
    assert text.rstrip().endswith("</untrusted_content>")
    assert len(text) < MAX_REFERENCE_CHARS * 2


async def test_a_file_reference_reads_the_workspace_of_the_session() -> None:
    sandbox = FakeSandbox()
    asked: list[tuple[str, str]] = []

    async def session_sandbox(org_id: str, session_id: str) -> Sandbox | None:
        asked.append((org_id, session_id))
        return cast(Sandbox, sandbox)

    run = claimed(
        "Read @file:notes/todo.md @file:../etc/passwd @file:/etc/passwd "
        "@file:missing.txt @file:huge.txt"
    )
    text = await ReferenceExpander(session_sandbox=session_sandbox).expand(run)
    assert '<untrusted_content source="file" url="notes/todo.md">\nbuy milk' in text
    assert text.count("error: the path must be relative to the workspace and stay in it") == 2
    assert "@file:missing.txt\nerror: the file does not exist" in text
    assert f"@file:huge.txt\nerror: the file is larger than {MAX_FILE_BYTES} bytes" in text
    assert "/work/../etc/passwd" not in sandbox.reads
    assert "/etc/passwd" not in sandbox.reads
    assert set(asked) == {(str(run.org_id), str(run.session_id))}
    # A link that points out of the workspace is refused, and nothing is read.
    links = await ReferenceExpander(session_sandbox=session_sandbox).expand(
        claimed("@file:outside/passwd @file:up")
    )
    assert links.count("error: the path must be relative to the workspace and stay in it") == 2
    assert "/etc/passwd" not in sandbox.reads
    binary = await ReferenceExpander(session_sandbox=session_sandbox).expand(
        claimed("@file:blob.bin")
    )
    assert "error: the file is not UTF-8 text" in binary


async def test_a_file_reference_needs_a_workspace() -> None:
    async def no_sandbox(org_id: str, session_id: str) -> Sandbox | None:
        return None

    text = await ReferenceExpander(session_sandbox=no_sandbox).expand(claimed("@file:a.txt"))
    assert "error: the session has no workspace yet" in text
    plain = await ReferenceExpander().expand(claimed("@file:a.txt @url:https://a.test @session:x"))
    assert "@file:a.txt\nerror: files are not available" in plain
    assert "@url:https://a.test\nerror: web pages are not available" in plain
    assert "@session:x\nerror: sessions are not available" in plain


async def test_too_many_references_are_listed_but_not_read() -> None:
    labels = [f"@session:s{i}" for i in range(MAX_REFERENCES + 2)]
    text = await ReferenceExpander().expand(claimed(" ".join(labels)))
    assert text.count("error: sessions are not available") == MAX_REFERENCES
    assert f"Not read: a message can have {MAX_REFERENCES} references. " in text
    assert text.endswith(f"{labels[-2]}, {labels[-1]}")


async def test_a_slow_or_broken_reference_gives_an_error_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow(org_id: str, session_id: str) -> Sandbox | None:
        await asyncio.sleep(10)
        return None

    text = await ReferenceExpander(session_sandbox=slow, timeout_s=0.05).expand(
        claimed("@file:a.txt")
    )
    assert "@file:a.txt\nerror: the reference did not finish in time" in text

    async def broken(org_id: str, session_id: str) -> Sandbox | None:
        raise RuntimeError("secret detail")

    text = await ReferenceExpander(session_sandbox=broken).expand(claimed("@file:a.txt"))
    assert "@file:a.txt\nerror: the reference could not be read" in text
    assert "secret detail" not in text


# -- sessions and the worker (Postgres) ----------------------------------------------------------


async def test_a_session_reference_reads_a_visible_session_only(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="the answer is 42"),)))
    await harness.send(tenant, "s2", "what is the answer?")
    assert await harness.worker.run_once()
    expander = ReferenceExpander(sessions=SessionSearch(engine))
    run = claimed(
        f"See @session:{tenant.ids['s2']} and @session:{tenant.ids['mine']}",
        tenant,
    )
    text = await expander.expand(run)
    shared, private = text.rsplit(f"@session:{tenant.ids['mine']}\n", 1)
    assert "what is the answer?" in shared
    assert "the answer is 42" in shared
    # A shared session cannot read the private session of a user.
    assert private.startswith("error: ")


async def test_the_worker_gives_the_runtime_the_attached_input_and_keeps_the_message(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    client = outbound()
    harness = Harness(engine, references=ReferenceExpander(outbound=client))
    try:
        harness.runtime.add_script(FakeScript(events=(RunCompleted(output="ok"),)))
        run_id = await harness.send(tenant, "s1", "Read @url:https://page.test/plan")
        assert await harness.worker.run_once()
    finally:
        await client.aclose()
    [request] = harness.runtime.requests
    assert isinstance(request, RunRequest)
    assert request.input.startswith("Read @url:https://page.test/plan\n\n" + HEADER)
    assert "Ship it" in request.input
    assert await harness.status_is(run_id, "completed")
    async with engine.connect() as connection:
        stored = (
            await connection.execute(
                select(Message.content).where(Message.run_id == run_id, Message.role == "user")
            )
        ).scalar_one()
    assert stored == {"text": "Read @url:https://page.test/plan"}


async def test_a_reference_is_redacted_before_the_cut() -> None:
    sandbox = FakeSandbox()
    key = "sk-" + "a1B2c3D4" * 6
    # The key crosses the cut: without the redaction first, a part of it stays.
    sandbox.files["/work/env.txt"] = (
        "x" * (MAX_REFERENCE_CHARS - 80) + "\nOPENAI_API_KEY=" + key
    ).encode()

    async def session_sandbox(org_id: str, session_id: str) -> Sandbox | None:
        return cast(Sandbox, sandbox)

    text = await ReferenceExpander(session_sandbox=session_sandbox).expand(
        claimed("Read @file:env.txt")
    )
    assert key[:20] not in text
    assert "[REDACTED]" in text
