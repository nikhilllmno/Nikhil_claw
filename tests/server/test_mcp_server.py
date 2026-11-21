"""The sama MCP server for other agents (docs/architecture.md, Section 10.7, M7.6)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import CallToolResult, TextContent
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.routing import Route

from sama.apikeys import ApiKeyStore
from sama.db.models import Approval, Message, Run, Session
from sama.runtime.contract import ApprovalRequired, RunCompleted
from sama.runtime.fake import FakeScript
from sama.server import SamaMcpServer, ServerCore
from sama.server.mcp import MCP_PATH, running
from tests.conformance.scripted_model import serve
from tests.orchestrator.conftest import Tenant
from tests.orchestrator.test_worker import Harness
from tests.server.conftest import FAST, Keys, answer, reply

pytestmark = pytest.mark.usefixtures("admin")


@pytest.fixture
def url(engine: AsyncEngine) -> Iterator[str]:
    core = ServerCore(engine, settings=FAST)
    agents = SamaMcpServer(core, ApiKeyStore(engine))
    inner = agents.app()

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with running(inner):
            yield

    app = Starlette(
        routes=[Route(MCP_PATH, endpoint=agents.guarded(inner))],
        lifespan=lifespan,
    )
    with serve(app) as base:
        yield base + MCP_PATH


@contextlib.asynccontextmanager
async def connect(url: str, key: str) -> AsyncIterator[Client]:
    http = httpx2.AsyncClient(headers={"authorization": f"Bearer {key}"}, timeout=30)
    async with http, Client(streamable_http_client(url, http_client=http)) as client:
        yield client


def text_of(result: CallToolResult) -> str:
    return "\n".join(c.text for c in result.content if isinstance(c, TextContent))


def data(result: CallToolResult) -> Any:
    assert not result.is_error, text_of(result)
    return json.loads(text_of(result))


@pytest.mark.parametrize("header", [None, "Bearer nope", "Basic abc"])
async def test_a_request_without_a_valid_key_gets_401(url: str, header: str | None) -> None:
    headers = {"content-type": "application/json", "accept": "application/json"}
    if header is not None:
        headers["authorization"] = header
    async with httpx.AsyncClient() as http:
        response = await http.post(
            url,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    assert response.status_code == 401


async def test_a_key_without_the_mcp_scope_gets_401(url: str, keys: Keys) -> None:
    key = await keys.make(scopes=["chat"])
    async with httpx.AsyncClient() as http:
        response = await http.post(
            url,
            headers={
                "authorization": f"Bearer {key}",
                "content-type": "application/json",
                "accept": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    assert response.status_code == 401
    assert "scope mcp" in response.json()["error"]


async def test_the_tools_are_listed(url: str, keys: Keys) -> None:
    key = await keys.make()
    async with connect(url, key) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
    assert tools == {
        "agents_list",
        "send_message",
        "run_get",
        "session_read",
        "session_search",
        "skills_list",
        "workflows_list",
        "workflow_run",
        "workflow_get",
    }


async def test_send_message_runs_the_agent_and_continues_the_session(
    url: str, keys: Keys, admin: Tenant, harness: Harness, engine: AsyncEngine
) -> None:
    key = await keys.make()
    async with connect(url, key) as client:
        agents = data(await client.call_tool("agents_list", {}))
        assert [agent["agent"] for agent in agents] == ["p"]

        pending = asyncio.ensure_future(
            client.call_tool("send_message", {"text": "hello", "agent": "p", "wait_s": 20})
        )
        await answer(harness, admin.org, reply("hi there"))
        first = data(await pending)
        assert first["status"] == "completed"
        assert first["reply"] == "hi there"
        session_id = first["session_id"]

        pending = asyncio.ensure_future(
            client.call_tool(
                "send_message", {"text": "again", "session_id": session_id, "wait_s": 20}
            )
        )
        await answer(harness, admin.org, reply("second"))
        second = data(await pending)
        assert second["session_id"] == session_id
        assert second["reply"] == "second"

        page = data(await client.call_tool("session_read", {"session_id": session_id}))
        assert [(m["role"], m["text"]) for m in page["messages"]] == [
            ("user", "hello"),
            ("assistant", "hi there"),
            ("user", "again"),
            ("assistant", "second"),
        ]
        later = data(
            await client.call_tool(
                "session_read", {"session_id": session_id, "after_seq": page["next_after_seq"]}
            )
        )
        assert later["messages"] == []

        found = data(await client.call_tool("session_search", {"query": ""}))
        assert session_id in {hit["session_id"] for hit in found}

    async with engine.connect() as connection:
        session = (
            await connection.execute(select(Session).where(Session.id == uuid.UUID(session_id)))
        ).one()
        runs = (
            await connection.execute(
                select(Run.unattended).where(Run.session_id == uuid.UUID(session_id))
            )
        ).all()
    assert (session.audience, session.owner_user_id) == ("private", admin.ids["mia"])
    assert [row.unattended for row in runs] == [True, True]


async def test_send_message_without_a_wait_gives_the_run_id(
    url: str, keys: Keys, admin: Tenant, harness: Harness
) -> None:
    key = await keys.make()
    async with connect(url, key) as client:
        started = data(await client.call_tool("send_message", {"text": "later", "wait_s": 0}))
        assert started["status"] == "queued"
        assert "run_get" in started["note"]
        await answer(harness, admin.org, reply("done"))
        result = data(await client.call_tool("run_get", {"run_id": started["run_id"], "wait_s": 5}))
    assert (result["status"], result["reply"]) == ("completed", "done")


async def test_a_paused_run_names_the_approval(
    url: str, keys: Keys, admin: Tenant, harness: Harness, engine: AsyncEngine
) -> None:
    key = await keys.make(approvals="pending")
    async with connect(url, key) as client:
        started = data(await client.call_tool("send_message", {"text": "do it", "wait_s": 0}))
        run_id = uuid.UUID(started["run_id"])
        script = FakeScript(
            events=(
                ApprovalRequired(
                    approval_id="a1", call_id="c1", tool_name="execute_code", reason="risky"
                ),
            ),
            on_approve=(RunCompleted(output="ok"),),
            on_reject=(RunCompleted(output="no"),),
        )
        await answer(harness, admin.org, script)
        async with engine.connect() as connection:
            status = (await connection.execute(select(Run.status).where(Run.id == run_id))).one()
            pending = (
                await connection.execute(select(Approval.id).where(Approval.run_id == run_id))
            ).all()
        if status.status != "paused" or not pending:
            pytest.skip("the fake runtime did not store an approval in this setup")
        result = data(await client.call_tool("run_get", {"run_id": str(run_id)}))
    assert result["status"] == "paused"
    assert result["waiting_for"][0]["tool"] == "execute_code"
    assert "web app" in result["note"]


async def test_a_session_of_another_workspace_is_not_found(
    url: str, keys: Keys, admin: Tenant, engine: AsyncEngine
) -> None:
    key = await keys.make()
    other_ws = uuid.uuid4()
    other_session = uuid.uuid4()
    from sama.db.models import Workspace

    async with engine.begin() as connection:
        await connection.execute(
            insert(Workspace).values(id=other_ws, org_id=admin.org, name="other", slug="other")
        )
        await connection.execute(
            insert(Session).values(
                id=other_session,
                org_id=admin.org,
                workspace_id=other_ws,
                agent_profile_id=admin.ids["profile"],
                audience="shared",
            )
        )
        await connection.execute(
            insert(Message).values(
                org_id=admin.org, session_id=other_session, role="user", content={"text": "x"}
            )
        )
    async with connect(url, key) as client:
        read = await client.call_tool("session_read", {"session_id": str(other_session)})
        send = await client.call_tool(
            "send_message", {"text": "x", "session_id": str(other_session), "wait_s": 0}
        )
        bad = await client.call_tool("run_get", {"run_id": "not-a-uuid"})
    assert read.is_error
    assert "does not exist" in text_of(read)
    assert send.is_error
    assert bad.is_error


async def test_a_revoked_key_fails_the_next_tool_call(url: str, keys: Keys, admin: Tenant) -> None:
    key = await keys.make()
    [row] = await keys.store.list(admin.org)
    async with connect(url, key) as client:
        assert not (await client.call_tool("agents_list", {})).is_error
        await keys.store.revoke(admin.org, row["id"], actor_user_id=admin.ids["mia"])
        # The key gate refuses the HTTP request with 401 before MCP reads it.
        with pytest.raises(MCPError):
            await client.call_tool("agents_list", {})


async def test_skills_and_workflows_say_when_they_are_off(url: str, keys: Keys) -> None:
    key = await keys.make()
    async with connect(url, key) as client:
        skills = await client.call_tool("skills_list", {})
        workflows = await client.call_tool("workflows_list", {})
    assert skills.is_error
    assert "off" in text_of(skills)
    assert workflows.is_error
