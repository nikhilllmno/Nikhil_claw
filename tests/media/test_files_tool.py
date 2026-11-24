"""The ``file_send`` tool of the sama MCP server (docs/architecture.md, Section 11.10; roadmap
M8.6)."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.gate.decide import DEFAULT_ALLOW, TOOLSETS
from sama.instructions import SYSTEM_PROMPT
from sama.runtime.contract import Audience, RunScope
from sama.tenancy import RunTokenSigner
from sama.tools import MemoryTodoStore, ToolServer
from tests.conformance.scripted_model import serve
from tests.media.test_files import WORKDIR
from tests.media.test_files import Rig as ServiceRig
from tests.media.test_files import rig as service_rig  # noqa: F401
from tests.tools.test_server import connect, text_of


@dataclass
class Rig:
    url: str
    signer: RunTokenSigner
    services: ServiceRig

    def token(self) -> str:
        claims = self.services.claims()
        scope = RunScope(
            org_id=claims.org_id,
            workspace_id=claims.workspace_id,
            session_id=claims.session_id,
            run_id=claims.run_id,
            audience=Audience.PRIVATE,
            user_id=claims.user_id,
        )
        return self.signer.issue(scope, ttl_s=600)


@pytest.fixture
async def rig(service_rig: ServiceRig) -> AsyncIterator[Rig]:  # noqa: F811
    signer = RunTokenSigner(secrets.token_bytes(32))
    server = ToolServer(signer=signer, todos=MemoryTodoStore(), files=service_rig.sender)
    with serve(server.app()) as base:
        yield Rig(base + "/mcp", signer, service_rig)


def test_the_tool_is_in_the_media_toolset_and_allowed_by_default() -> None:
    assert "file_send" in TOOLSETS["media"]
    assert "file_send" in DEFAULT_ALLOW


def test_the_system_text_tells_the_model_to_send_files_and_not_paths() -> None:
    assert "call file_send with its path" in SYSTEM_PROMPT
    assert "never give either one as the way to get a file" in SYSTEM_PROMPT


async def test_the_tool_is_hidden_without_the_service() -> None:
    signer = RunTokenSigner(secrets.token_bytes(32))
    server = ToolServer(signer=signer, todos=MemoryTodoStore())
    with serve(server.app()) as base:
        scope = RunScope(
            org_id="o", workspace_id="w", session_id="s", run_id="r", audience=Audience.SHARED
        )
        async with connect(base + "/mcp", signer.issue(scope, ttl_s=60)) as client:
            names = {t.name for t in (await client.list_tools()).tools}
    assert "file_send" not in names


async def test_the_tool_sends_a_file_and_explains_a_bad_path(rig: Rig, engine: AsyncEngine) -> None:
    rig.services.sandbox.files[f"{WORKDIR}/top5.csv"] = b"rank,country\n1,India\n"
    async with connect(rig.url, rig.token()) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        schema = tools["file_send"].input_schema
        assert set(schema["properties"]) == {"path", "name", "caption"}
        assert schema["required"] == ["path"]
        sent = await client.call_tool("file_send", {"path": "top5.csv"})
        bad = await client.call_tool("file_send", {"path": "/etc/passwd"})
    assert not sent.is_error
    assert "Sent top5.csv" in text_of(sent)
    assert bad.is_error
    assert "work folder" in text_of(bad)
    pending = await rig.services.store.pending(rig.services.tenant.org, rig.services.run_id)
    assert [m.name for m in pending] == ["top5.csv"]
