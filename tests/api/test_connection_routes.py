"""Tests of the connection routes of the HTTP API (docs/architecture.md, Section 10.3)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import JwtVerifier, SamaApi
from sama.connectors.api import ApiDriver
from sama.db.models import Connection, Membership
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import Vault
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.api.test_app import add_user, mia
from tests.connectors.fake_mcp import URL, FakeMcp, driver
from tests.connectors.support import NOTION_TOKEN, FakeComposio, FakeForm, service
from tests.connectors.test_api_driver import HOST as API_HOST
from tests.connectors.test_api_driver import FakeApi
from tests.orchestrator.conftest import Tenant
from tests.support import wire_transport

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
    # A refused body does not come back in the answer: it can hold a secret.
    too_many = {"values": {"token": NOTION_TOKEN} | {f"f{i}": "x" for i in range(30)}}
    for refused in (
        too_many,
        {"values": {"token": NOTION_TOKEN, "n": 5}},
        {"values": {"token": NOTION_TOKEN}, "extra": NOTION_TOKEN},
    ):
        response = await web.put(
            f"/connections/{connection['id']}/form", headers=mia(tenant), json=refused
        )
        assert response.status_code == 422
        assert NOTION_TOKEN not in response.text
        assert response.json()["detail"][0]["loc"]
    disabled = await web.post(f"/connections/{connection['id']}/disable", headers=mia(tenant))
    assert disabled.json()["state"] == "disabled"


async def test_bad_input_is_refused(web: httpx.AsyncClient, tenant: Tenant) -> None:
    for body, status in (
        ({"connector": "nope"}, 404),
        ({"connector": "gmail", "scope": "everyone"}, 422),
    ):
        response = await web.post(path(tenant), headers=mia(tenant), json=body)
        assert response.status_code == status, body


# -- remote MCP servers (M3.5) -----------------------------------------------------------------


@pytest.fixture
def remote() -> FakeMcp:
    fake = FakeMcp("oauth")
    fake.tokens = set()
    return fake


@pytest.fixture
async def mcp_web(
    engine: AsyncEngine, tenant: Tenant, remote: FakeMcp
) -> AsyncIterator[httpx.AsyncClient]:
    vault = Vault(engine, VaultKey(b"w" * KEY_BYTES))
    connections = service(engine, vault, FakeComposio(), FakeForm(), mcp=driver(remote, vault))
    api = SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        connections=connections,
    )
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        yield client
    await connections.aclose()


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


async def test_an_admin_adds_a_server_by_url_and_signs_in(
    mcp_web: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant, remote: FakeMcp
) -> None:
    body = {"name": "tracker", "url": URL, "auth": "oauth", "scope": "personal"}
    url = path(tenant) + "/mcp"
    # Only a workspace admin adds a server by URL.
    assert (await mcp_web.post(url, headers=mia(tenant), json=body)).status_code == 403
    await make_admin(engine, tenant)
    for bad in (
        {**body, "url": "http://mcp.remote.test/mcp"},
        {**body, "name": "Bad Name"},
        {**body, "auth": "basic"},
    ):
        assert (await mcp_web.post(url, headers=mia(tenant), json=bad)).status_code == 422, bad
    started = await mcp_web.post(url, headers=mia(tenant), json=body)
    assert started.status_code == 201
    redirect = started.json()["redirect_url"]
    connection = started.json()["connection"]
    assert connection["state"] == "initiated"
    assert connection["mcp"]["custom"]["url"] == URL
    code, state = remote.approve(redirect)
    callback = "/connections/oauth/callback"
    for broken, status in (("not-a-uuid.x", 400), (state[:-1] + "x", 400)):
        answer = await mcp_web.post(
            callback, headers=mia(tenant), json={"state": broken, "code": code}
        )
        assert answer.status_code == status, broken
    # Another member cannot finish the sign-in of mia.
    other = bearer(await add_user(engine, tenant, workspace_member=True))
    refused = await mcp_web.post(callback, headers=other, json={"state": state, "code": code})
    assert refused.status_code == 404
    done = await mcp_web.post(callback, headers=mia(tenant), json={"state": state, "code": code})
    assert done.status_code == 200
    assert done.json()["state"] == "connected"
    assert "access-1" not in done.text
    names = [t["name"] for t in done.json()["mcp"]["tools"]]
    assert names == ["mcp_tracker_list_items", "mcp_tracker_create_item", "mcp_tracker_drop_item"]
    # The tool filters.
    tools = f"/connections/{connection['id']}/tools"
    saved = await mcp_web.put(
        tools, headers=mia(tenant), json={"include": ["*_item*"], "exclude": ["drop_*"]}
    )
    assert saved.status_code == 200
    enabled = {t["remote_name"]: t["enabled"] for t in saved.json()["mcp"]["tools"]}
    assert enabled == {"list_items": True, "create_item": True, "drop_item": False}
    assert saved.json()["mcp"]["exclude"] == ["drop_*"]
    refused_pattern = await mcp_web.put(tools, headers=mia(tenant), json={"include": ["a b"]})
    assert refused_pattern.status_code == 422
    assert (await mcp_web.put(tools, headers=other, json={})).status_code == 404


@pytest.fixture
async def api_web(engine: AsyncEngine, tenant: Tenant) -> AsyncIterator[httpx.AsyncClient]:
    from tests.connectors.fake_mcp import public

    vault = Vault(engine, VaultKey(b"w" * KEY_BYTES))
    api_driver = ApiDriver(transport=wire_transport(FakeApi()), resolver=public)
    connections = service(engine, vault, FakeComposio(), FakeForm(), api=api_driver)
    api = SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        connections=connections,
    )
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        yield client
    await connections.aclose()


async def test_an_admin_adds_an_http_api_from_a_list_of_endpoints(
    api_web: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    url = path(tenant) + "/api"
    body = {
        "name": "plain",
        "base_url": f"https://{API_HOST}/v2",
        "auth": {"type": "none"},
        "endpoints": [
            {"name": "get_item", "method": "GET", "path": "/items/{id}"},
            {"name": "drop_item", "method": "DELETE", "path": "/items/{id}"},
        ],
    }
    assert (await api_web.post(url, headers=mia(tenant), json=body)).status_code == 403
    await make_admin(engine, tenant)
    for bad in (
        {**body, "auth": {"type": "digest"}},
        {**body, "endpoints": [{"method": "TRACE", "path": "/x"}]},
        {**body, "unknown": 1},
    ):
        assert (await api_web.post(url, headers=mia(tenant), json=bad)).status_code == 422, bad
    started = await api_web.post(url, headers=mia(tenant), json=body)
    assert started.status_code == 201, started.text
    connection = started.json()["connection"]
    assert connection["state"] == "connected"
    assert connection["mcp"]["custom"]["base_url"] == f"https://{API_HOST}/v2"
    tools = {t["name"]: t for t in connection["mcp"]["tools"]}
    assert set(tools) == {"api_plain_get_item", "api_plain_drop_item"}
    assert (tools["api_plain_get_item"]["method"], tools["api_plain_get_item"]["path"]) == (
        "GET",
        "/items/{id}",
    )


async def test_a_stdio_server_needs_e2b_and_a_valid_body(
    api_web: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    url = path(tenant) + "/stdio"
    body = {"name": "everything", "package": {"registry": "npm", "name": "x", "version": "1.0.0"}}
    bad = {**body, "package": {"registry": "cargo", "name": "x", "version": "1.0.0"}}
    assert (await api_web.post(url, headers=mia(tenant), json=bad)).status_code == 422
    await make_admin(engine, tenant)
    refused = await api_web.post(url, headers=mia(tenant), json=body)
    assert refused.status_code == 409
    assert "E2B" in refused.text
