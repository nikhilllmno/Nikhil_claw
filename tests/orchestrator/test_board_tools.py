"""The delegation and task board tools over the MCP tool server (docs/architecture.md,
Sections 12.4 and 13.2)."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.client import Client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Session, Task
from sama.delegation import DelegationService
from sama.orchestrator.queue import RunQueue
from sama.runtime import Audience, RunScope
from sama.tasks.runs import TaskTools
from sama.tenancy import RunTokenSigner
from sama.tools import MemoryTodoStore, ToolServer
from tests.conformance.scripted_model import serve
from tests.orchestrator.conftest import Tenant
from tests.tools.test_server import connect, text_of

BOARD_TOOLS = {
    "delegate_task",
    "delegate_status",
    "task_create",
    "task_list",
    "task_complete",
    "task_block",
    "task_comment",
}


@asynccontextmanager
async def client_for(
    engine: AsyncEngine, tenant: Tenant, session: str = "s1"
) -> AsyncIterator[tuple[Client, uuid.UUID]]:
    queue = RunQueue(engine)
    run_id = await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids[session], text="hi", user_id=tenant.ids["mia"]
    )
    claimed = await queue.claim("worker-tools")
    assert claimed is not None
    assert claimed.run_id == run_id
    signer = RunTokenSigner(secrets.token_bytes(32))
    server = ToolServer(
        signer=signer,
        todos=MemoryTodoStore(),
        delegation=DelegationService(engine, queue),
        tasks=TaskTools(engine),
    )
    private = session == "mine"
    scope = RunScope(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(tenant.ids[session]),
        run_id=str(run_id),
        audience=Audience.PRIVATE if private else Audience.SHARED,
        user_id=str(tenant.ids["mia"]) if private else None,
    )
    with serve(server.app()) as base:
        async with connect(base + "/mcp", signer.issue(scope, ttl_s=600)) as client:
            yield client, run_id


async def test_the_tools_are_listed_with_the_services(engine: AsyncEngine, tenant: Tenant) -> None:
    async with client_for(engine, tenant) as (client, _):
        names = {t.name for t in (await client.list_tools()).tools}
    assert names >= BOARD_TOOLS


async def test_delegate_task_starts_children_without_a_wait(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    async with client_for(engine, tenant) as (client, run_id):
        result = await client.call_tool(
            "delegate_task",
            {"tasks": [{"goal": "Read file A"}, {"goal": "Read file B"}], "wait": False},
        )
        assert not result.is_error, text_of(result)
        bad = await client.call_tool("delegate_status", {"action": "status", "child_id": "nope"})
        assert bad.is_error
        too_many = await client.call_tool(
            "delegate_task", {"tasks": [{"goal": str(n)} for n in range(11)]}
        )
        assert too_many.is_error
    async with engine.connect() as connection:
        children = (
            await connection.execute(select(Session.id).where(Session.parent_run_id == run_id))
        ).all()
    assert len(children) == 2
    text = text_of(result)
    assert all(str(child.id) in text for child in children)


async def test_the_task_tools_make_and_list_cards(engine: AsyncEngine, tenant: Tenant) -> None:
    async with client_for(engine, tenant) as (client, _):
        made = await client.call_tool(
            "task_create", {"title": "Call the supplier", "assignee": "none"}
        )
        assert not made.is_error, text_of(made)
        listed = text_of(await client.call_tool("task_list", {}))
        assert "Call the supplier" in listed
        async with engine.connect() as connection:
            task_id = (
                await connection.execute(select(Task.id).where(Task.title == "Call the supplier"))
            ).scalar_one()
        commented = await client.call_tool(
            "task_comment", {"body": "I left a message.", "task_id": str(task_id)}
        )
        assert not commented.is_error, text_of(commented)
        not_mine = await client.call_tool("task_complete", {"result": "done"})
        assert not_mine.is_error
        assert "not a task run" in text_of(not_mine)
        done = await client.call_tool(
            "task_complete", {"result": "Booked.", "task_id": str(task_id)}
        )
        assert not done.is_error, text_of(done)
    async with engine.connect() as connection:
        row = (await connection.execute(select(Task).where(Task.id == task_id))).one()
    assert (row.status, row.result) == ("done", "Booked.")
