"""Tests of the task board routes (docs/architecture.md, Section 12.4)."""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Run, Task
from sama.orchestrator.queue import RunQueue
from tests.api.conftest import bearer
from tests.api.test_app import add_user, mia
from tests.orchestrator.conftest import Tenant

pytestmark = pytest.mark.usefixtures("tenant")


def board(tenant: Tenant) -> str:
    return f"/workspaces/{tenant.ids['ws']}/tasks"


async def test_a_member_creates_lists_and_edits_a_card(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    made = await client.post(
        board(tenant),
        headers=mia(tenant),
        json={"title": "Plan the launch", "body": "Week 3", "assignee_user_id": None},
    )
    assert made.status_code == 201, made.text
    task = made.json()
    assert (task["status"], task["audience"], task["title"]) == (
        "todo",
        "shared",
        "Plan the launch",
    )

    listed = await client.get(board(tenant), headers=mia(tenant))
    assert listed.status_code == 200
    body = listed.json()
    assert body["user_id"] == str(tenant.ids["mia"])
    assert [t["id"] for t in body["tasks"]] == [task["id"]]
    only_done = await client.get(board(tenant), headers=mia(tenant), params={"status": ["done"]})
    assert only_done.json()["tasks"] == []

    edited = await client.patch(
        f"{board(tenant)}/{task['id']}",
        headers=mia(tenant),
        json={
            "status": "done",
            "result": "Shipped.",
            "assignee": "person",
            "assignee_user_id": str(tenant.ids["mia"]),
        },
    )
    assert edited.status_code == 200, edited.text
    assert (edited.json()["status"], edited.json()["result"]) == ("done", "Shipped.")

    comment = await client.post(
        f"{board(tenant)}/{task['id']}/comments", headers=mia(tenant), json={"body": "Nice."}
    )
    assert comment.status_code == 201, comment.text
    detail = (await client.get(f"{board(tenant)}/{task['id']}", headers=mia(tenant))).json()
    assert [c["body"] for c in detail["comments"]] == ["Nice."]
    assert detail["comments"][0]["author_kind"] == "user"


async def test_the_routes_check_the_input(client: httpx.AsyncClient, tenant: Tenant) -> None:
    made = await client.post(board(tenant), headers=mia(tenant), json={"title": "x"})
    task_id = made.json()["id"]
    running = await client.patch(
        f"{board(tenant)}/{task_id}", headers=mia(tenant), json={"status": "running"}
    )
    assert running.status_code == 422
    missing = await client.get(f"{board(tenant)}/{uuid.uuid4()}", headers=mia(tenant))
    assert missing.status_code == 404
    bad_profile = await client.post(
        board(tenant),
        headers=mia(tenant),
        json={"title": "x", "assignee_profile_id": str(uuid.uuid4())},
    )
    assert bad_profile.status_code == 422
    both = await client.post(
        board(tenant),
        headers=mia(tenant),
        json={
            "title": "x",
            "assignee_profile_id": str(tenant.ids["profile"]),
            "assignee_user_id": str(tenant.ids["mia"]),
        },
    )
    assert both.status_code == 422


async def test_a_private_card_is_hidden_from_other_members(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    made = await client.post(
        board(tenant), headers=mia(tenant), json={"title": "Mine", "audience": "private"}
    )
    task_id = made.json()["id"]
    zed = bearer(await add_user(engine, tenant, workspace_member=True))
    assert (await client.get(board(tenant), headers=zed)).json()["tasks"] == []
    hidden = await client.get(f"{board(tenant)}/{task_id}", headers=zed)
    assert hidden.status_code == 404
    edit = await client.patch(f"{board(tenant)}/{task_id}", headers=zed, json={"status": "done"})
    assert edit.status_code == 404


async def test_a_person_who_is_not_in_the_workspace_gets_404(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    outsider = bearer(await add_user(engine, tenant, workspace_member=False))
    assert (await client.get(board(tenant), headers=outsider)).status_code == 404
    created = await client.post(board(tenant), headers=outsider, json={"title": "x"})
    assert created.status_code == 404
    stranger = bearer(uuid.uuid4())
    assert (await client.get(board(tenant), headers=stranger)).status_code == 404


async def test_moving_a_card_out_of_running_cancels_its_run(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    made = await client.post(board(tenant), headers=mia(tenant), json={"title": "x"})
    task_id = uuid.UUID(made.json()["id"])
    run_id = await RunQueue(engine).enqueue(
        org_id=tenant.org, session_id=tenant.ids["s1"], text="go"
    )
    async with engine.begin() as connection:
        await connection.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(status="running", session_id=tenant.ids["s1"], run_id=run_id)
        )
    moved = await client.patch(
        f"{board(tenant)}/{task_id}", headers=mia(tenant), json={"status": "todo"}
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["run_id"] is None
    async with engine.connect() as connection:
        status = (await connection.execute(select(Run.status).where(Run.id == run_id))).scalar_one()
    assert status == "cancelled"
