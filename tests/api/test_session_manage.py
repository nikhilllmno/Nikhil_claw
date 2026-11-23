"""Rename, archive, and restore of a session, and the list of archived sessions (M7.10)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AuditEntry, Session
from sama.orchestrator.queue import QueueError, RunQueue
from tests.api.conftest import bearer
from tests.api.test_app import add_user, mia
from tests.orchestrator.conftest import Tenant

pytestmark = pytest.mark.usefixtures("tenant")


async def audit_actions(engine: AsyncEngine, session_id: uuid.UUID) -> list[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(AuditEntry.action)
            .where(AuditEntry.target_id == str(session_id), AuditEntry.target_type == "session")
            .order_by(AuditEntry.id)
        )
        return [str(action) for (action,) in rows]


async def status_of(engine: AsyncEngine, session_id: uuid.UUID) -> str:
    async with engine.connect() as connection:
        return str(await connection.scalar(select(Session.status).where(Session.id == session_id)))


async def test_the_owner_renames_a_private_session(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    mine = tenant.ids["mine"]

    response = await client.patch(
        f"/sessions/{mine}", headers=mia(tenant), json={"title": "  A new name  "}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "A new name"
    assert (body["id"], body["status"], body["audience"]) == (str(mine), "active", "private")
    assert set(body) == {
        "id",
        "org_id",
        "workspace_id",
        "agent_profile_id",
        "audience",
        "title",
        "status",
        "created_at",
        "lineage",
        "parent_session_id",
        "coding_agent",
        "browser_target",
        "can_manage",
        "runtime",
        "model",
        "default_model",
        "model_is_override",
        "run_state",
        "last_activity_at",
    }
    assert await audit_actions(engine, mine) == ["session.renamed"]
    again = await client.patch(
        f"/sessions/{mine}", headers=mia(tenant), json={"title": "A new name"}
    )
    assert again.status_code == 200
    assert await audit_actions(engine, mine) == ["session.renamed"], "the same title is no change"


async def test_the_owner_archives_and_restores_and_the_list_follows(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    mine = tenant.ids["mine"]

    archived = await client.patch(
        f"/sessions/{mine}", headers=mia(tenant), json={"status": "archived", "title": "Old"}
    )

    assert archived.status_code == 200, archived.text
    assert (archived.json()["status"], archived.json()["title"]) == ("archived", "Old")
    active = (await client.get("/sessions", headers=mia(tenant))).json()
    assert str(mine) not in [s["id"] for s in active]
    listed = (await client.get("/sessions?status=archived", headers=mia(tenant))).json()
    assert [s["id"] for s in listed] == [str(mine)]
    default = (await client.get("/sessions?status=active", headers=mia(tenant))).json()
    assert default == active
    assert (await client.get("/sessions?status=other", headers=mia(tenant))).status_code == 422

    restored = await client.patch(
        f"/sessions/{mine}", headers=mia(tenant), json={"status": "active"}
    )

    assert restored.json()["status"] == "active"
    assert await audit_actions(engine, mine) == [
        "session.renamed",
        "session.archived",
        "session.restored",
    ]
    assert (await client.get("/sessions?status=archived", headers=mia(tenant))).json() == []
    # The same status again is no change.
    await client.patch(f"/sessions/{mine}", headers=mia(tenant), json={"status": "active"})
    assert len(await audit_actions(engine, mine)) == 3


async def test_a_member_cannot_change_a_shared_session_but_an_admin_can(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    shared = tenant.ids["s1"]
    member = await client.patch(f"/sessions/{shared}", headers=mia(tenant), json={"title": "x"})
    assert member.status_code == 403
    admin = bearer(await add_user(engine, tenant, workspace_member=False, role="admin"))

    response = await client.patch(f"/sessions/{shared}", headers=admin, json={"status": "archived"})

    assert response.status_code == 200, response.text
    assert await status_of(engine, shared) == "archived"
    entry = (await client.get("/sessions?status=archived", headers=mia(tenant))).json()
    assert [s["id"] for s in entry] == [str(shared)]


async def test_a_session_that_the_user_cannot_see_gets_404(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    stranger = bearer(await add_user(engine, tenant, workspace_member=True))
    body = {"title": "x"}
    assert (
        await client.patch(f"/sessions/{tenant.ids['mine']}", headers=stranger, json=body)
    ).status_code == 404
    assert (
        await client.patch(f"/sessions/{uuid.uuid4()}", headers=mia(tenant), json=body)
    ).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 201},
        {"title": None},
        {"status": "gone"},
    ],
)
async def test_a_bad_body_gets_422(
    client: httpx.AsyncClient, tenant: Tenant, body: dict[str, Any]
) -> None:
    response = await client.patch(f"/sessions/{tenant.ids['mine']}", headers=mia(tenant), json=body)
    assert response.status_code == 422


async def test_a_session_with_an_open_run_cannot_be_archived(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    mine = tenant.ids["mine"]
    sent = await client.post(
        f"/sessions/{mine}/messages", headers=mia(tenant), json={"text": "hello"}
    )
    assert sent.status_code == 202, sent.text

    response = await client.patch(
        f"/sessions/{mine}", headers=mia(tenant), json={"status": "archived"}
    )

    assert response.status_code == 409
    assert await status_of(engine, mine) == "active"
    # A rename is allowed while a run is open.
    renamed = await client.patch(f"/sessions/{mine}", headers=mia(tenant), json={"title": "busy"})
    assert renamed.status_code == 200


async def test_a_compaction_keeps_its_status(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    live = tenant.ids["mine"]
    archive = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(Session).values(
                id=archive,
                org_id=tenant.org,
                workspace_id=tenant.ids["ws"],
                agent_profile_id=tenant.ids["profile"],
                audience="private",
                owner_user_id=tenant.ids["mia"],
                parent_session_id=live,
                lineage="compaction",
                title="Before compaction 1",
                status="archived",
            )
        )

    restore = await client.patch(
        f"/sessions/{archive}", headers=mia(tenant), json={"status": "active"}
    )
    archive_live = await client.patch(
        f"/sessions/{live}", headers=mia(tenant), json={"status": "archived"}
    )
    rename = await client.patch(f"/sessions/{archive}", headers=mia(tenant), json={"title": "Kept"})

    assert (restore.status_code, archive_live.status_code) == (409, 409)
    assert await status_of(engine, archive) == "archived"
    assert await status_of(engine, live) == "active"
    assert rename.status_code == 200


async def test_an_enqueue_waits_for_an_archive_of_the_session_and_then_refuses(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    mine = tenant.ids["mine"]
    queue = RunQueue(engine)
    async with engine.connect() as holder, holder.begin():
        # The archive holds the row for update.
        await holder.execute(select(Session.id).where(Session.id == mine).with_for_update())
        task = asyncio.create_task(
            queue.enqueue(
                org_id=tenant.org, session_id=mine, text="late", user_id=tenant.ids["mia"]
            )
        )
        await asyncio.sleep(0.3)
        assert not task.done(), "the enqueue waits for the lock of the row"
        await holder.execute(update(Session).where(Session.id == mine).values(status="archived"))
    with pytest.raises(QueueError, match="not active"):
        await task


async def test_an_archive_waits_for_an_enqueue_and_then_sees_the_open_run(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    mine = tenant.ids["mine"]
    queue = RunQueue(engine)
    async with engine.connect() as holder, holder.begin():
        await queue.enqueue_in(
            holder, org_id=tenant.org, session_id=mine, text="first", user_id=tenant.ids["mia"]
        )
        task = asyncio.create_task(
            client.patch(f"/sessions/{mine}", headers=mia(tenant), json={"status": "archived"})
        )
        await asyncio.sleep(0.3)
        assert not task.done(), "the archive waits for the enqueue"
    response = await task
    assert response.status_code == 409
    assert await status_of(engine, mine) == "active"


async def test_each_session_answer_says_if_the_caller_can_manage_it(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    mine, shared = str(tenant.ids["mine"]), str(tenant.ids["s1"])
    admin = bearer(await add_user(engine, tenant, workspace_member=False, role="admin"))
    member = bearer(await add_user(engine, tenant, workspace_member=True))

    async def listed(headers: dict[str, str]) -> dict[str, bool]:
        rows = (await client.get("/sessions", headers=headers)).json()
        return {row["id"]: row["can_manage"] for row in rows}

    owner_view = await listed(mia(tenant))
    assert (owner_view[mine], owner_view[shared]) == (True, False)
    admin_view = await listed(admin)
    assert admin_view[shared] is True
    assert mine not in admin_view, "a private session of another user stays hidden"
    assert (await listed(member))[shared] is False
    detail = (await client.get(f"/sessions/{mine}", headers=mia(tenant))).json()
    assert detail["can_manage"] is True
    assert (await client.get(f"/sessions/{shared}", headers=mia(tenant))).json()[
        "can_manage"
    ] is False
    assert (await client.get(f"/sessions/{shared}", headers=admin)).json()["can_manage"] is True
    created = await client.post(
        "/sessions",
        headers=mia(tenant),
        json={
            "workspace_id": str(tenant.ids["ws"]),
            "agent_profile_id": str(tenant.ids["profile"]),
        },
    )
    assert created.json()["can_manage"] is True
    shared_new = await client.post(
        "/sessions",
        headers=mia(tenant),
        json={
            "workspace_id": str(tenant.ids["ws"]),
            "agent_profile_id": str(tenant.ids["profile"]),
            "audience": "shared",
        },
    )
    assert shared_new.json()["can_manage"] is False
    patched = await client.patch(f"/sessions/{shared}", headers=admin, json={"title": "t"})
    assert patched.json()["can_manage"] is True
    # The flag agrees with the PATCH rule.
    assert (
        await client.patch(f"/sessions/{shared}", headers=mia(tenant), json={"title": "x"})
    ).status_code == 403
