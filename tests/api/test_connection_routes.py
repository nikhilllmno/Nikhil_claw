"""Tests of the connection routes of the HTTP API (docs/architecture.md, Section 10.3)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import JwtVerifier, SamaApi
from sama.db.models import Connection, Membership
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import Vault
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.api.test_app import add_user, mia
from tests.connectors.support import NOTION_TOKEN, FakeComposio, FakeForm, service
from tests.orchestrator.conftest import Tenant

pytestmark = pytest.mark.usefixtures("tenant")


@pytest.fixture
def fake() -> FakeComposio:
    return FakeComposio()


@pytest.fixture
async def web(
    engine: AsyncEngine, tenant: Tenant, fake: FakeComposio
) -> AsyncIterator[httpx.AsyncClient]:
    connections = service(engine, Vault(engine, VaultKey(b"w" * KEY_BYTES)), fake, FakeForm())
    api = SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        connections=connections,
    )
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        yield client
    await connections.aclose()


def path(tenant: Tenant) -> str:
    return f"/workspaces/{tenant.ids['ws']}/connections"


async def test_the_routes_are_off_without_the_service(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    assert (await client.get("/config")).json()["connections"] is False
    assert (await client.get("/connectors", headers=mia(tenant))).status_code == 409


async def test_the_catalog_shows_no_tool_schema(web: httpx.AsyncClient, tenant: Tenant) -> None:
    assert (await web.get("/config")).json()["connections"] is True
    assert (await web.get("/connectors")).status_code == 401
    catalog = (await web.get("/connectors", headers=mia(tenant))).json()
    notion = next(c for c in catalog if c["name"] == "notion")
    assert notion["fields"][0]["secret"] is True
    assert all("input_schema" not in t for c in catalog for t in c["tools"])


async def test_a_user_connects_gmail_for_themselves(
    web: httpx.AsyncClient, tenant: Tenant, fake: FakeComposio
) -> None:
    started = await web.post(
        path(tenant), headers=mia(tenant), json={"connector": "gmail", "scope": "personal"}
    )
    assert started.status_code == 201
    body = started.json()
    assert body["redirect_url"].startswith("https://connect.composio.dev/")
    connection = body["connection"]
    assert (connection["state"], connection["scope"]) == ("initiated", "personal")
    # The client cannot set the state: only the check asks Composio.
    fake.status = "ACTIVE"
    checked = await web.post(f"/connections/{connection['id']}/check", headers=mia(tenant))
    assert checked.json()["state"] == "connected"
    listed = (await web.get(path(tenant), headers=mia(tenant))).json()
    assert [(c["connector"], c["state"]) for c in listed] == [("gmail", "connected")]


async def test_a_personal_connection_is_hidden_from_other_users(
    web: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    started = await web.post(path(tenant), headers=mia(tenant), json={"connector": "gmail"})
    connection_id = started.json()["connection"]["id"]
    other = bearer(await add_user(engine, tenant, workspace_member=True))
    assert (await web.get(path(tenant), headers=other)).json() == []
    for method, url in (
        ("POST", f"/connections/{connection_id}/check"),
        ("POST", f"/connections/{connection_id}/disable"),
        ("DELETE", f"/connections/{connection_id}"),
    ):
        response = await web.request(method, url, headers=other)
        assert response.status_code == 404, url
    outsider = bearer(await add_user(engine, tenant, workspace_member=False))
    assert (await web.get(path(tenant), headers=outsider)).status_code == 404


async def test_only_a_workspace_admin_connects_for_the_workspace(
    web: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    body = {"connector": "gmail", "scope": "workspace"}
    assert (await web.post(path(tenant), headers=mia(tenant), json=body)).status_code == 403
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership)
            .where(
                Membership.user_id == tenant.ids["mia"],
                Membership.workspace_id == tenant.ids["ws"],
            )
            .values(role="admin")
        )
    started = await web.post(path(tenant), headers=mia(tenant), json=body)
    assert started.status_code == 201
    connection_id = started.json()["connection"]["id"]
    # A member sees the workspace connection but cannot change it.
    member = bearer(await add_user(engine, tenant, workspace_member=True))
    assert len((await web.get(path(tenant), headers=member)).json()) == 1
    response = await web.post(f"/connections/{connection_id}/disable", headers=member)
    assert response.status_code == 403
    assert (
        await web.delete(f"/connections/{connection_id}", headers=mia(tenant))
    ).status_code == 204


async def test_a_form_secret_is_never_in_an_answer(
    web: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    started = await web.post(path(tenant), headers=mia(tenant), json={"connector": "notion"})
    connection = started.json()["connection"]
    assert started.json()["redirect_url"] is None
    saved = await web.put(
        f"/connections/{connection['id']}/form",
        headers=mia(tenant),
        json={"values": {"token": NOTION_TOKEN}},
    )
    assert saved.status_code == 200
    assert saved.json()["state"] == "connected"
    assert NOTION_TOKEN not in saved.text
    listed = await web.get(path(tenant), headers=mia(tenant))
    assert NOTION_TOKEN not in listed.text
    async with engine.connect() as db:
        row = (await db.execute(select(Connection).where(Connection.id == connection["id"]))).one()
    assert row.secret_ref == f"connection:{connection['id']}"
    too_many = {"values": {f"f{i}": "x" for i in range(30)}}
    response = await web.put(
        f"/connections/{connection['id']}/form", headers=mia(tenant), json=too_many
    )
    assert response.status_code == 422
    disabled = await web.post(f"/connections/{connection['id']}/disable", headers=mia(tenant))
    assert disabled.json()["state"] == "disabled"


async def test_bad_input_is_refused(web: httpx.AsyncClient, tenant: Tenant) -> None:
    for body, status in (
        ({"connector": "nope"}, 404),
        ({"connector": "gmail", "scope": "everyone"}, 422),
    ):
        response = await web.post(path(tenant), headers=mia(tenant), json=body)
        assert response.status_code == status, body
