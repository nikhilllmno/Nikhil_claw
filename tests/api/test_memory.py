"""The memory routes of the HTTP API (Section 8)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AuditEntry, Membership
from tests.api.conftest import bearer
from tests.api.test_app import add_user, mia
from tests.orchestrator.conftest import Tenant

pytestmark = pytest.mark.usefixtures("tenant")


def url(tenant: Tenant, suffix: str = "") -> str:
    return f"/workspaces/{tenant.ids['ws']}/memory{suffix}"


def profile(tenant: Tenant) -> dict[str, str]:
    return {"agent_profile_id": str(tenant.ids["profile"])}


async def make_admin(engine: AsyncEngine, tenant: Tenant) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership)
            .where(
                Membership.user_id == tenant.ids["mia"],
                Membership.workspace_id == tenant.ids["ws"],
            )
            .values(role="admin")
        )


def by_target(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {note["target"]: note for note in body["targets"]}


async def test_the_notes_are_listed_with_usage_and_rights(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    response = await client.get(url(tenant), headers=mia(tenant), params=profile(tenant))
    assert response.status_code == 200
    notes = by_target(response.json())
    assert set(notes) == {"agent", "user", "workspace"}
    assert notes["user"] == {
        "target": "user",
        "entries": [],
        "chars": 0,
        "cap": 1_375,
        "revision": 0,
        "can_edit": True,
    }
    assert notes["agent"]["can_edit"] is True
    assert notes["workspace"]["can_edit"] is False
    assert notes["workspace"]["cap"] == 2_200


async def test_the_agent_notes_need_a_profile(client: httpx.AsyncClient, tenant: Tenant) -> None:
    listed = await client.get(url(tenant), headers=mia(tenant))
    assert set(by_target(listed.json())) == {"user", "workspace"}
    missing = await client.put(url(tenant, "/agent"), headers=mia(tenant), json={"entries": []})
    assert missing.status_code == 422
    unknown = await client.put(
        url(tenant, "/agent"),
        headers=mia(tenant),
        json={"entries": [], "agent_profile_id": str(uuid.uuid4())},
    )
    assert unknown.status_code == 404


async def test_a_person_edits_their_notes_and_the_history_grows(
    client: httpx.AsyncClient, tenant: Tenant, engine: AsyncEngine
) -> None:
    saved = await client.put(
        url(tenant, "/user"),
        headers=mia(tenant),
        json={"entries": ["  likes tea ", "likes tea", "uses uv"], "reason": "my profile"},
    )
    assert saved.status_code == 200
    assert saved.json() == {
        "target": "user",
        "entries": ["likes tea", "uses uv"],
        "chars": len("likes tea\n§\nuses uv"),
        "cap": 1_375,
        "revision": 1,
    }
    listed = await client.get(url(tenant), headers=mia(tenant))
    assert by_target(listed.json())["user"]["entries"] == ["likes tea", "uses uv"]
    history = await client.get(url(tenant, "/user/revisions"), headers=mia(tenant))
    (revision,) = history.json()
    assert revision["seq"] == 1
    assert revision["action"] == "edit"
    assert revision["author_kind"] == "user"
    assert revision["author_user_id"] == str(tenant.ids["mia"])
    assert revision["reason"] == "my profile"
    async with engine.connect() as connection:
        actions = (
            (
                await connection.execute(
                    select(AuditEntry.action).where(AuditEntry.org_id == tenant.org)
                )
            )
            .scalars()
            .all()
        )
    assert "memory.edited" in actions


async def test_the_user_notes_belong_to_the_caller(
    client: httpx.AsyncClient, tenant: Tenant, engine: AsyncEngine
) -> None:
    await client.put(url(tenant, "/user"), headers=mia(tenant), json={"entries": ["mine"]})
    other = await add_user(engine, tenant, workspace_member=True)
    seen = await client.get(url(tenant), headers=bearer(other))
    assert by_target(seen.json())["user"]["entries"] == []
    history = await client.get(url(tenant, "/user/revisions"), headers=bearer(other))
    assert history.json() == []


async def test_a_member_cannot_edit_workspace_notes_but_an_admin_can(
    client: httpx.AsyncClient, tenant: Tenant, engine: AsyncEngine
) -> None:
    body = {"entries": ["builds with make"]}
    denied = await client.put(url(tenant, "/workspace"), headers=mia(tenant), json=body)
    assert denied.status_code == 403
    await make_admin(engine, tenant)
    listed = await client.get(url(tenant), headers=mia(tenant))
    assert by_target(listed.json())["workspace"]["can_edit"] is True
    allowed = await client.put(url(tenant, "/workspace"), headers=mia(tenant), json=body)
    assert allowed.status_code == 200
    assert allowed.json()["entries"] == ["builds with make"]


async def test_any_workspace_member_edits_the_agent_notes(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    saved = await client.put(
        url(tenant, "/agent"),
        headers=mia(tenant),
        json={"entries": ["uses uv"], **profile(tenant)},
    )
    assert saved.status_code == 200
    history = await client.get(
        url(tenant, "/agent/revisions"), headers=mia(tenant), params=profile(tenant)
    )
    assert [r["entries"] for r in history.json()] == [["uses uv"]]


async def test_a_user_who_cannot_see_the_workspace_gets_404(
    client: httpx.AsyncClient, tenant: Tenant, engine: AsyncEngine
) -> None:
    outsider = await add_user(engine, tenant, workspace_member=False)
    headers = bearer(outsider)
    assert (await client.get(url(tenant), headers=headers)).status_code == 404
    put = await client.put(url(tenant, "/user"), headers=headers, json={"entries": ["x"]})
    assert put.status_code == 404
    history = await client.get(url(tenant, "/user/revisions"), headers=headers)
    assert history.status_code == 404
    stranger = bearer(uuid.uuid4())
    assert (await client.get(url(tenant), headers=stranger)).status_code == 404


async def test_a_person_needs_to_sign_in(client: httpx.AsyncClient, tenant: Tenant) -> None:
    assert (await client.get(url(tenant))).status_code == 401
    assert (await client.put(url(tenant, "/user"), json={"entries": []})).status_code == 401


async def test_a_full_memory_and_a_secret_are_refused(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    full = await client.put(
        url(tenant, "/user"), headers=mia(tenant), json={"entries": ["x" * 1_376]}
    )
    assert full.status_code == 422
    assert "memory is full" in full.json()["detail"]
    secret = "sk-ant-api03-" + "a" * 40
    leaked = await client.put(
        url(tenant, "/user"), headers=mia(tenant), json={"entries": [f"key {secret}"]}
    )
    assert leaked.status_code == 422
    assert secret not in leaked.text
    empty = await client.put(url(tenant, "/user"), headers=mia(tenant), json={"entries": [" "]})
    assert empty.status_code == 422
    unknown = await client.put(
        url(tenant, "/user"), headers=mia(tenant), json={"entries": [], "extra": 1}
    )
    assert unknown.status_code == 422
    listed = await client.get(url(tenant), headers=mia(tenant))
    assert by_target(listed.json())["user"]["revision"] == 0


async def test_a_revert_sets_the_notes_back_and_keeps_the_history(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    for entries in (["one"], ["one", "two"]):
        await client.put(url(tenant, "/user"), headers=mia(tenant), json={"entries": entries})
    reverted = await client.post(
        url(tenant, "/user/revisions/1/revert"), headers=mia(tenant), json={"reason": "undo"}
    )
    assert reverted.status_code == 200
    assert reverted.json()["entries"] == ["one"]
    assert reverted.json()["revision"] == 3
    history = await client.get(url(tenant, "/user/revisions"), headers=mia(tenant))
    assert [(r["seq"], r["action"], r["reverted_to"]) for r in history.json()] == [
        (3, "revert", 1),
        (2, "edit", None),
        (1, "edit", None),
    ]
    limited = await client.get(
        url(tenant, "/user/revisions"), headers=mia(tenant), params={"limit": 1}
    )
    assert [r["seq"] for r in limited.json()] == [3]
    missing = await client.post(
        url(tenant, "/user/revisions/9/revert"), headers=mia(tenant), json={}
    )
    assert missing.status_code == 404


async def test_a_member_cannot_revert_workspace_notes(
    client: httpx.AsyncClient, tenant: Tenant, engine: AsyncEngine
) -> None:
    await make_admin(engine, tenant)
    await client.put(url(tenant, "/workspace"), headers=mia(tenant), json={"entries": ["a"]})
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership).where(Membership.user_id == tenant.ids["mia"]).values(role="member")
        )
    denied = await client.post(
        url(tenant, "/workspace/revisions/1/revert"), headers=mia(tenant), json={}
    )
    assert denied.status_code == 403
