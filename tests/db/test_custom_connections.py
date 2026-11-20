"""Hosted stdio MCP connections and custom HTTP API connections with Postgres
(docs/architecture.md, Sections 10.3.2 and 10.3.3, M4.11 and M4.12).

The fixtures are those of the gate tests. The API is ``FakeApi`` at ``https://api.remote.test``.
The connector hosts are local folders (``LocalHosts``), and the real bridge runs the fake stdio
server.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.connectors.api import ApiDriver
from sama.connectors.host import ConnectorHosts
from sama.connectors.service import ConnectionService, ConnectorError
from sama.db.models import Connection
from sama.gate.service import GateService
from sama.tenancy.access import Grants
from sama.tools.deferred import DeferredCatalog
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import Vault
from tests.connectors.fake_mcp import FakeMcp, driver
from tests.connectors.support import FakeComposio, FakeForm
from tests.connectors.support import service as make_service
from tests.connectors.test_api_driver import HOST, FakeApi
from tests.connectors.test_host import SERVER, LocalHosts
from tests.db.test_connections import member
from tests.db.test_deferred_gate import grant, wrapped
from tests.db.test_tool_gate import Org1
from tests.db.test_tool_gate import org as gate_org
from tests.support import wire_transport

org = gate_org

TOKEN = "api-bearer-token-777"
HOSTED_SECRET = "hosted-secret-value-99"
DOCUMENT = {
    "openapi": "3.0.0",
    "servers": [{"url": f"https://{HOST}/v1"}],
    "paths": {
        "/items": {
            "get": {"operationId": "listItems", "summary": "List the items."},
            "post": {
                "operationId": "createItem",
                "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
            },
        },
        "/items/{id}": {
            "parameters": [{"name": "id", "in": "path", "required": True}],
            "delete": {"operationId": "dropItem"},
        },
    },
}


class World:
    def __init__(self, service: ConnectionService, api: FakeApi, backend: LocalHosts) -> None:
        self.service = service
        self.api = api
        self.backend = backend


@pytest.fixture
async def world(engine: AsyncEngine, tmp_path: Path) -> AsyncIterator[World]:
    from tests.connectors.fake_mcp import public

    vault = Vault(engine, VaultKey(b"c" * KEY_BYTES))
    fake_api = FakeApi()
    root = tmp_path / "hosts"
    root.mkdir()
    backend = LocalHosts(root)
    service = make_service(
        engine,
        vault,
        FakeComposio(),
        FakeForm(),
        mcp=driver(FakeMcp("bearer"), vault),
        hosts=ConnectorHosts(backend, reaper_every_s=3600),
        api=ApiDriver(transport=wire_transport(fake_api), resolver=public),
    )
    yield World(service, fake_api, backend)
    await service.aclose()


def owner(org: Org1) -> Grants:
    return Grants(org.ids["mia"], ((None, "owner"), (org.ids["ws"], "admin")))


async def row_of(engine: AsyncEngine, connection_id: Any) -> Any:
    async with engine.connect() as connection:
        return (
            await connection.execute(select(Connection).where(Connection.id == connection_id))
        ).one()


async def add_demo_api(world: World, org: Org1, **changes: Any) -> Any:
    who = member(org, "mia", admin=True)
    arguments: dict[str, Any] = {
        "name": "demo",
        "title": "Demo API",
        "scope": "workspace",
        "auth": {"type": "bearer"},
        "openapi": json.dumps(DOCUMENT),
        **changes,
    }
    return await world.service.add_api(org.ids["org"], org.ids["ws"], who, **arguments)


async def test_an_openapi_import_gives_api_tools_that_the_gate_knows(
    engine: AsyncEngine, org: Org1, world: World
) -> None:
    started = await add_demo_api(world, org)
    view = started.connection
    assert view.kind == "http_api"
    assert view.state == "pending"
    public = view.public()["mcp"]
    assert [(t["name"], t["access"], t["method"], t["path"]) for t in public["tools"]] == [
        ("api_demo_list_items", "read", "GET", "/items"),
        ("api_demo_create_item", "write", "POST", "/items"),
        ("api_demo_drop_item", "destructive", "DELETE", "/items/{id}"),
    ]
    assert public["custom"]["base_url"] == f"https://{HOST}/v1"
    connected = await world.service.submit_form(
        org.ids["org"], view.id, {"token": TOKEN}, actor=org.ids["mia"]
    )
    assert connected.state == "connected"
    # The health check called the first GET without arguments, with the token.
    assert world.api.requests[-1].url.path == "/v1/items"
    assert world.api.requests[-1].headers["authorization"] == f"Bearer {TOKEN}"
    row = await row_of(engine, view.id)
    assert TOKEN not in json.dumps(row.config) + json.dumps(row.tool_cache)
    tools = {t.name: t for t in await world.service.mcp_tools(org.claims())}
    assert set(tools) == {"api_demo_list_items", "api_demo_create_item", "api_demo_drop_item"}
    assert "(HTTP API)" in tools["api_demo_list_items"].description
    answer = await tools["api_demo_create_item"].handler(org.claims(), {"body": {"n": 1}})
    assert '"ok":true' in answer
    assert world.api.requests[-1].method == "POST"
    assert json.loads(world.api.requests[-1].content) == {"n": 1}
    # The gate: a GET tool reads, so it is allowed. The other methods ask.
    gate = GateService(engine, extra_toolsets=DeferredCatalog([]), run_tools=world.service)
    await grant(engine, org, "tool_search", "integrations")
    claims = org.claims()
    assert (await gate.check(claims, "c1", "tool_call", wrapped("api_demo_list_items"))).action == (
        "allow"
    )
    assert (
        await gate.check(claims, "c2", "tool_call", wrapped("api_demo_create_item"))
    ).action == ("ask")
    assert (await gate.check(claims, "c3", "tool_call", wrapped("api_demo_drop_item"))).action == (
        "ask"
    )
    listed = await world.service.list_for_agent(claims)
    assert "- demo (Demo API, HTTP API added by an admin): connected (workspace)." in listed
    assert "Tools: 3 (1 read only)." in listed


async def test_a_list_of_endpoints_without_auth_connects_at_once(org: Org1, world: World) -> None:
    started = await world.service.add_api(
        org.ids["org"],
        org.ids["ws"],
        member(org, "mia", admin=True),
        name="plain",
        title=None,
        scope="workspace",
        auth={"type": "none"},
        base_url=f"https://{HOST}/v2",
        endpoints=[
            {"name": "get_item", "method": "GET", "path": "/items/{id}"},
            {"name": "ping", "method": "GET", "path": "/ping"},
        ],
        test="ping",
    )
    assert started.connection.state == "connected"
    assert world.api.requests[-1].url.path == "/v2/ping"
    tools = {t.name: t for t in await world.service.mcp_tools(org.claims())}
    await tools["api_plain_get_item"].handler(org.claims(), {"id": "a/b"})
    assert world.api.requests[-1].url.raw_path == b"/v2/items/a%2Fb"


@pytest.mark.parametrize(
    ("changes", "status", "message"),
    [
        ({"openapi_url": f"https://{HOST}/doc"}, 422, "one of"),
        ({"openapi": "{}"}, 422, "OpenAPI 3"),
        ({"openapi": json.dumps({"openapi": "3.0.0", "paths": {}})}, 422, "no operations"),
        (
            {"openapi": json.dumps({**DOCUMENT, "servers": []})},
            422,
            "give the base URL",
        ),
        ({"base_url": "https://private.remote.test"}, 422, "cannot use this URL"),
        ({"test": "nothing"}, 422, "no operation nothing"),
        ({"auth": {"type": "api_key"}}, 422, "name"),
        ({"name": "neon"}, 409, "catalog connector"),
        ({"name": "Bad Name"}, 422, "the name has"),
    ],
)
async def test_a_bad_api_is_refused(
    org: Org1, world: World, changes: dict[str, Any], status: int, message: str
) -> None:
    with pytest.raises(ConnectorError, match=message) as refused:
        await add_demo_api(world, org, **changes)
    assert refused.value.status_code == status


async def test_only_a_workspace_admin_adds_an_api(org: Org1, world: World) -> None:
    with pytest.raises(ConnectorError) as refused:
        await world.service.add_api(
            org.ids["org"],
            org.ids["ws"],
            member(org, "mia"),
            name="demo",
            title=None,
            scope="workspace",
            auth={"type": "none"},
            openapi=json.dumps(DOCUMENT),
        )
    assert refused.value.status_code == 403


async def test_a_new_base_url_drops_the_credential(
    engine: AsyncEngine, org: Org1, world: World
) -> None:
    view = (await add_demo_api(world, org)).connection
    await world.service.submit_form(org.ids["org"], view.id, {"token": TOKEN}, actor=org.ids["mia"])
    again = (await add_demo_api(world, org)).connection
    assert again.id == view.id
    assert again.state == "connected"
    moved = (await add_demo_api(world, org, base_url=f"https://{HOST}/v9")).connection
    assert moved.id == view.id
    assert moved.state == "pending"
    row = await row_of(engine, view.id)
    assert row.secret_ref is None


async def add_hosted(world: World, org: Org1, who: Grants, **changes: Any) -> Any:
    arguments: dict[str, Any] = {
        "name": "fake",
        "title": "Fake server",
        "scope": "workspace",
        "package": None,
        "command": ["/usr/bin/env", "python3", str(SERVER)],
        "env": ["DEMO_TOKEN"],
        "egress": ["api.demo.test"],
        **changes,
    }
    return await world.service.add_stdio(org.ids["org"], org.ids["ws"], who, **arguments)


async def test_the_owner_adds_a_stdio_server_with_a_command(
    engine: AsyncEngine, org: Org1, world: World
) -> None:
    started = await add_hosted(world, org, owner(org))
    view = started.connection
    assert view.kind == "mcp_hosted"
    assert view.state == "pending"
    # No host before the first use.
    assert world.backend.created == []
    connected = await world.service.submit_form(
        org.ids["org"], view.id, {"demo_token": HOSTED_SECRET}, actor=org.ids["mia"]
    )
    assert connected.state == "connected", connected.last_error
    assert len(world.backend.created) == 1
    spec = world.backend.created[0]
    assert spec.allow_out == ("api.demo.test",)
    row = await row_of(engine, view.id)
    stored = json.dumps(row.config) + json.dumps(row.tool_cache)
    assert HOSTED_SECRET not in stored
    assert HOSTED_SECRET not in json.dumps(spec.labels())
    tools = {t.name: t for t in await world.service.mcp_tools(org.claims())}
    assert {"mcp_fake_echo", "mcp_fake_has_secret"} <= set(tools)
    assert tools["mcp_fake_echo"].access == "read"
    assert "(hosted MCP server)" in tools["mcp_fake_echo"].description
    answer = await tools["mcp_fake_has_secret"].handler(org.claims(), {})
    assert f"set=True length={len(HOSTED_SECRET)}" in answer
    assert HOSTED_SECRET not in answer
    gate = GateService(engine, extra_toolsets=DeferredCatalog([]), run_tools=world.service)
    await grant(engine, org, "tool_search", "integrations")
    check = await gate.check(org.claims(), "c1", "tool_call", wrapped("mcp_fake_echo"))
    assert check.action == "allow"
    await world.service.delete(org.ids["org"], view.id, actor=org.ids["mia"])
    assert list(world.backend.root.iterdir()) == []


async def test_a_command_needs_the_owner_and_a_package_needs_an_admin(
    org: Org1, world: World
) -> None:
    admin = member(org, "mia", admin=True)
    with pytest.raises(ConnectorError, match="only the org owner") as refused:
        await add_hosted(world, org, admin)
    assert refused.value.status_code == 403
    with pytest.raises(ConnectorError) as not_admin:
        await add_hosted(world, org, member(org, "mia"), command=None, package=PACKAGE)
    assert not_admin.value.status_code == 403
    started = await add_hosted(world, org, admin, command=None, package=PACKAGE)
    assert started.connection.state == "pending"
    public = started.connection.public()["mcp"]["custom"]
    assert public["package"]["version"] == "2025.9.25"
    assert world.backend.created == []


PACKAGE = {
    "registry": "npm",
    "name": "@modelcontextprotocol/server-everything",
    "version": "2025.9.25",
}


async def test_a_custom_name_cannot_change_its_kind(org: Org1, world: World) -> None:
    await add_demo_api(world, org, name="fake")
    with pytest.raises(ConnectorError) as clash:
        await add_hosted(world, org, owner(org))
    assert clash.value.status_code == 409


async def test_without_e2b_a_stdio_server_cannot_be_added(engine: AsyncEngine, org: Org1) -> None:
    vault = Vault(engine, VaultKey(b"d" * KEY_BYTES))
    service = make_service(engine, vault, FakeComposio(), FakeForm())
    with pytest.raises(ConnectorError, match="E2B") as refused:
        await add_hosted(World(service, FakeApi(), LocalHosts(Path("."))), org, owner(org))
    assert refused.value.status_code == 409
    await service.aclose()
