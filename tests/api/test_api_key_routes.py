"""The routes of the org API keys (docs/architecture.md, Sections 10.7 and 11.4, M7.6)."""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.apikeys import ApiKeyStore
from sama.db.models import Membership
from tests.api.conftest import bearer
from tests.orchestrator.conftest import Tenant


async def make_admin(engine: AsyncEngine, tenant: Tenant) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership)
            .where(Membership.user_id == tenant.ids["mia"], Membership.workspace_id.is_(None))
            .values(role="admin")
        )


async def test_an_admin_makes_lists_and_revokes_a_key(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await make_admin(engine, tenant)
    me = bearer(tenant.ids["mia_auth"])
    path = f"/orgs/{tenant.org}/api-keys"
    made = await client.post(
        path,
        headers=me,
        json={
            "name": "Claude Code",
            "workspace_id": str(tenant.ids["ws"]),
            "agent_profile_id": str(tenant.ids["profile"]),
            "approvals": "pending",
            "expires_in_days": 30,
        },
    )
    assert made.status_code == 201, made.text
    assert made.headers["cache-control"] == "no-store"
    body = made.json()
    key = body["key"]
    assert key.startswith("sama_sk_")
    assert body["scopes"] == ["chat", "mcp"]
    assert body["approvals"] == "pending"
    assert body["agent_profile_name"] == "p"
    assert body["expires_at"] is not None
    assert (await ApiKeyStore(engine).verify(key, "chat")).key_id == uuid.UUID(body["id"])

    listed = await client.get(path, headers=me)
    assert listed.status_code == 200
    [row] = listed.json()
    assert row["id"] == body["id"]
    assert "key" not in row
    assert key not in listed.text

    gone = await client.delete(f"{path}/{body['id']}", headers=me)
    assert gone.status_code == 204
    [row] = (await client.get(path, headers=me)).json()
    assert row["status"] == "revoked"
    missing = await client.delete(f"{path}/{uuid.uuid4()}", headers=me)
    assert missing.status_code == 404


async def test_a_member_cannot_manage_keys(client: httpx.AsyncClient, tenant: Tenant) -> None:
    me = bearer(tenant.ids["mia_auth"])
    path = f"/orgs/{tenant.org}/api-keys"
    assert (await client.get(path, headers=me)).status_code == 403
    made = await client.post(
        path, headers=me, json={"name": "x", "workspace_id": str(tenant.ids["ws"])}
    )
    assert made.status_code == 403


async def test_a_stranger_gets_404(client: httpx.AsyncClient, tenant: Tenant) -> None:
    response = await client.get(f"/orgs/{tenant.org}/api-keys", headers=bearer(uuid.uuid4()))
    assert response.status_code == 404


async def test_a_bad_body_is_422(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    await make_admin(engine, tenant)
    me = bearer(tenant.ids["mia_auth"])
    path = f"/orgs/{tenant.org}/api-keys"
    for body in (
        {"name": "x", "workspace_id": str(tenant.ids["ws"]), "scopes": ["root"]},
        {"name": "", "workspace_id": str(tenant.ids["ws"])},
        {"name": "x", "workspace_id": str(uuid.uuid4())},
        {"name": "x", "workspace_id": str(tenant.ids["ws"]), "expires_in_days": 999},
        {"name": "x", "workspace_id": str(tenant.ids["ws"]), "extra": 1},
    ):
        response = await client.post(path, headers=me, json=body)
        assert response.status_code == 422, body
