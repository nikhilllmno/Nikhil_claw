"""``/claude``, ``/codex``, and ``/sama`` in the web API (M7.2, docs/architecture.md, Section
15.4)."""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.coding.commands import DIRECT_OFF, DIRECT_ON, NOT_ALLOWED, NOT_DIRECT
from sama.db.models import Membership, Message, Run, Session
from tests.api.conftest import bearer
from tests.orchestrator.conftest import Tenant

pytestmark = pytest.mark.usefixtures("tenant")


def mia(tenant: Tenant) -> dict[str, str]:
    return bearer(tenant.ids["mia_auth"])


async def mode(engine: AsyncEngine, session_id: uuid.UUID) -> str | None:
    async with engine.connect() as connection:
        return (
            await connection.execute(select(Session.coding_agent).where(Session.id == session_id))
        ).scalar_one()


async def sent(engine: AsyncEngine, run_id: str) -> tuple[str | None, str]:
    """``(coding_agent, text)`` of a run."""
    async with engine.connect() as connection:
        agent = (
            await connection.execute(select(Run.coding_agent).where(Run.id == uuid.UUID(run_id)))
        ).scalar_one()
        content = (
            await connection.execute(
                select(Message.content).where(
                    Message.run_id == uuid.UUID(run_id), Message.role == "user"
                )
            )
        ).scalar_one()
    return agent, content["text"]


async def test_a_message_with_a_command_goes_to_the_coding_agent(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    messages = f"/sessions/{tenant.ids['mine']}/messages"
    first = await client.post(messages, headers=mia(tenant), json={"text": "/claude fix tests"})
    assert first.status_code == 202
    assert await sent(engine, first.json()["run_id"]) == ("claude_code", "fix tests")

    second = await client.post(messages, headers=mia(tenant), json={"text": " /CODEX@bot x\ny "})
    assert second.status_code == 202
    assert await sent(engine, second.json()["run_id"]) == ("codex", "x\ny")

    # A command without a task, or for another name, is a normal message.
    for text in ("/claude", "/sama", "/claudex fix", "please /claude fix"):
        reply = await client.post(messages, headers=mia(tenant), json={"text": text})
        assert reply.status_code == 202
        assert await sent(engine, reply.json()["run_id"]) == (None, text)
    assert await mode(engine, tenant.ids["mine"]) is None


async def test_the_route_starts_and_ends_the_direct_mode(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    session = tenant.ids["mine"]
    route = f"/sessions/{session}/coding-agent"

    async def post(agent: str | None) -> httpx.Response:
        return await client.post(route, headers=mia(tenant), json={"agent": agent})

    off = await post(None)
    assert (off.status_code, off.json()) == (200, {"text": NOT_DIRECT})
    on = await post("claude_code")
    assert (on.status_code, on.json()) == (200, {"text": DIRECT_ON.format(name="Claude Code")})
    assert await mode(engine, session) == "claude_code"
    assert (await post("codex")).json() == {"text": DIRECT_ON.format(name="Codex")}
    assert await mode(engine, session) == "codex"
    assert (await post(None)).json() == {"text": DIRECT_OFF}
    assert await mode(engine, session) is None


async def test_the_route_checks_the_body_and_the_access(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    session = tenant.ids["mine"]
    route = f"/sessions/{session}/coding-agent"
    for body in ({}, {"agent": "sama"}, {"agent": "claude_code", "x": 1}):
        response = await client.post(route, headers=mia(tenant), json=body)
        assert response.status_code == 422
    assert (await client.post(route, json={"agent": None})).status_code == 401
    stranger = await client.post(route, headers=bearer(uuid.uuid4()), json={"agent": "codex"})
    assert stranger.status_code in (403, 404)
    assert await mode(engine, session) is None


async def test_a_member_cannot_switch_a_shared_session_but_can_send_a_task(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    """The rule of the settings console (M7.10): a switch changes the session for each person in
    it, so in a shared session only the owner or an admin may switch."""
    shared = tenant.ids["s1"]
    refused = await client.post(
        f"/sessions/{shared}/coding-agent", headers=mia(tenant), json={"agent": "codex"}
    )
    assert refused.status_code == 403
    assert refused.json()["detail"] == NOT_ALLOWED
    assert await mode(engine, shared) is None
    # A task is one message: each member who may send messages may send it.
    task = await client.post(
        f"/sessions/{shared}/messages", headers=mia(tenant), json={"text": "/codex fix it"}
    )
    assert task.status_code == 202
    assert await sent(engine, task.json()["run_id"]) == ("codex", "fix it")
    # A workspace admin may switch.
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership)
            .where(
                Membership.org_id == tenant.org,
                Membership.user_id == tenant.ids["mia"],
                Membership.workspace_id == tenant.ids["ws"],
            )
            .values(role="admin")
        )
    allowed = await client.post(
        f"/sessions/{shared}/coding-agent", headers=mia(tenant), json={"agent": "codex"}
    )
    assert allowed.status_code == 200
    assert await mode(engine, shared) == "codex"
