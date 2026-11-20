"""Remote MCP connections with Postgres (docs/architecture.md, Section 10.3, M3.5).

The fixtures are those of the gate tests: ``mia`` starts the run ``run`` in the shared session
``shared``, and the run ``secret-run`` in her private session ``secret``. ``FakeMcp`` is the
remote server for every host, so the catalog servers (``neon-mcp``, ``deepwiki``) answer too.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.connectors.service import ConnectionService, ConnectorError
from sama.db.models import AuditEntry, Connection
from sama.gate.service import GateService
from sama.tools.deferred import DeferredCatalog
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import Vault
from tests.connectors.fake_mcp import GOOD_TOKEN, URL, FakeMcp, driver, tool
from tests.connectors.support import FakeComposio, FakeForm
from tests.connectors.support import service as make_service
from tests.db.test_connections import member, private
from tests.db.test_deferred_gate import grant, wrapped
from tests.db.test_tool_gate import Org1
from tests.db.test_tool_gate import org as gate_org

org = gate_org

World = tuple[ConnectionService, FakeMcp]


@pytest.fixture
async def fake() -> FakeMcp:
    return FakeMcp("bearer")


@pytest.fixture
async def world(engine: AsyncEngine, org: Org1, fake: FakeMcp) -> AsyncIterator[World]:
    vault = Vault(engine, VaultKey(b"m" * KEY_BYTES))
    service = make_service(engine, vault, FakeComposio(), FakeForm(), mcp=driver(fake, vault))
    yield service, fake
    await service.aclose()


async def connect_neon(service: ConnectionService, org: Org1, scope: Any = "personal") -> Any:
    who = member(org, "mia", admin=scope == "workspace")
    started = await service.start(org.ids["org"], org.ids["ws"], who, "neon-mcp", scope)
    assert started.redirect_url is None
    assert started.connection.state == "pending"
    return await service.submit_form(
        org.ids["org"], started.connection.id, {"token": GOOD_TOKEN}, actor=who.user_id
    )


async def row_of(engine: AsyncEngine, connection_id: Any) -> Any:
    async with engine.connect() as connection:
        return (
            await connection.execute(select(Connection).where(Connection.id == connection_id))
        ).one()


async def test_a_catalog_server_connects_with_a_token_and_its_tools_are_usable(
    engine: AsyncEngine, org: Org1, world: World
) -> None:
    service, fake = world
    view = await connect_neon(service, org)
    assert view.state == "connected"
    row = await row_of(engine, view.id)
    assert len(row.tool_cache["tools"]) == 3
    assert GOOD_TOKEN not in json.dumps(row.config) + json.dumps(row.tool_cache)
    assert row.secret_ref == f"connection:{view.id}"
    public = view.public()["mcp"]
    assert [t["name"] for t in public["tools"]] == [
        "mcp_neon_mcp_list_items",
        "mcp_neon_mcp_create_item",
        "mcp_neon_mcp_drop_item",
    ]
    assert [t["access"] for t in public["tools"]] == ["read", "write", "destructive"]
    # The private session of mia sees the tools of her personal connection.
    tools = {t.name: t for t in await service.mcp_tools(private(org))}
    assert set(tools) == {
        "mcp_neon_mcp_list_items",
        "mcp_neon_mcp_create_item",
        "mcp_neon_mcp_drop_item",
    }
    assert tools["mcp_neon_mcp_list_items"].access == "read"
    assert tools["mcp_neon_mcp_list_items"].toolset == "integrations"
    assert "(remote MCP server)" in tools["mcp_neon_mcp_list_items"].description
    answer = await tools["mcp_neon_mcp_list_items"].handler(private(org), {"q": "a"})
    assert "connector:neon-mcp" in answer
    assert 'ran list_items {"q": "a"}' in answer
    assert fake.calls[-1] == ("list_items", {"q": "a"})
    listed = await service.list_for_agent(private(org))
    assert "- neon-mcp (Neon MCP, remote MCP server): connected (personal)." in listed
    assert "Tools: 3 (1 read only)." in listed
    # The shared session does not use a personal connection.
    assert await service.mcp_tools(org.claims()) == []
    with pytest.raises(ConnectorError, match="no connected MCP server"):
        await service.call_mcp(org.claims(), "mcp_neon_mcp_list_items", {})


async def test_a_wrong_token_fails_and_a_refused_call_asks_to_connect_again(
    org: Org1, world: World
) -> None:
    service, fake = world
    who = member(org, "mia")
    started = await service.start(org.ids["org"], org.ids["ws"], who, "neon-mcp", "personal")
    failed = await service.submit_form(
        org.ids["org"], started.connection.id, {"token": "wrong"}, actor=who.user_id
    )
    assert failed.state == "failed"
    assert "refused the credential" in (failed.last_error or "")
    view = await connect_neon(service, org)
    fake.tokens = set()
    with pytest.raises(ConnectorError, match="connection_request"):
        await service.call_mcp(private(org), "mcp_neon_mcp_list_items", {})
    after = await service.get(org.ids["org"], view.id)
    assert after is not None
    assert after.state == "failed"
    assert await service.mcp_tools(private(org)) == []


async def test_a_server_that_does_not_answer_keeps_the_state(org: Org1, world: World) -> None:
    service, fake = world
    view = await connect_neon(service, org)
    fake.down = True
    checked = await service.check(org.ids["org"], view.id, actor=None)
    assert checked.state == "connected"
    assert checked.last_error
    # The cached tools stay usable.
    assert len(await service.mcp_tools(private(org))) == 3


async def test_a_server_without_auth_connects_at_once_with_the_manifest_access(
    org: Org1, world: World, fake: FakeMcp
) -> None:
    service, _ = world
    fake.auth = "none"
    admin = member(org, "mia", admin=True)
    started = await service.start(org.ids["org"], org.ids["ws"], admin, "deepwiki", "workspace")
    assert started.connection.state == "connected"
    # The manifest says that all deepwiki tools only read.
    for claims in (org.claims(), private(org)):
        tools = await service.mcp_tools(claims)
        assert {t.access for t in tools} == {"read"}
        assert len(tools) == 3


async def test_new_tools_and_filters_apply_without_a_new_session(
    engine: AsyncEngine, org: Org1, world: World
) -> None:
    service, fake = world
    view = await connect_neon(service, org)
    claims = private(org)
    fake.tools.append(tool("search_docs", read_only=True))
    await service.check(org.ids["org"], view.id, actor=None)
    assert "mcp_neon_mcp_search_docs" in {t.name for t in await service.mcp_tools(claims)}
    mia = member(org, "mia")
    saved = await service.set_filters(
        org.ids["org"], view.id, include=(), exclude=("drop_*",), actor=mia.user_id
    )
    assert saved.public()["mcp"]["exclude"] == ["drop_*"]
    enabled = {t["name"]: t["enabled"] for t in saved.public()["mcp"]["tools"]}
    assert enabled["mcp_neon_mcp_drop_item"] is False
    names = {t.name for t in await service.mcp_tools(claims)}
    assert "mcp_neon_mcp_drop_item" not in names
    assert "mcp_neon_mcp_list_items" in names
    with pytest.raises(ConnectorError, match="no connected MCP server"):
        await service.call_mcp(claims, "mcp_neon_mcp_drop_item", {})
    await service.set_filters(
        org.ids["org"], view.id, include=("list_*",), exclude=(), actor=mia.user_id
    )
    assert {t.name for t in await service.mcp_tools(claims)} == {"mcp_neon_mcp_list_items"}
    with pytest.raises(ConnectorError) as caught:
        await service.set_filters(
            org.ids["org"], view.id, include=("bad pattern",), exclude=(), actor=mia.user_id
        )
    assert caught.value.status_code == 422
    async with engine.connect() as connection:
        actions = set(
            (
                await connection.execute(
                    select(AuditEntry.action).where(AuditEntry.org_id == org.ids["org"])
                )
            ).scalars()
        )
    assert "connection.filters_set" in actions


async def test_a_stale_tool_list_is_checked_in_the_background(
    engine: AsyncEngine, org: Org1, world: World
) -> None:
    service, fake = world
    view = await connect_neon(service, org)
    fake.tools.append(tool("new_tool", read_only=True))
    async with engine.begin() as connection:
        await connection.execute(
            update(Connection)
            .where(Connection.id == view.id)
            .values(last_health_at=datetime.now(UTC) - timedelta(hours=7))
        )
    before = {t.name for t in await service.mcp_tools(private(org))}
    assert "mcp_neon_mcp_new_tool" not in before
    await asyncio.gather(*list(service._background))
    after = {t.name for t in await service.mcp_tools(private(org))}
    assert "mcp_neon_mcp_new_tool" in after


async def test_an_admin_adds_a_server_by_url(engine: AsyncEngine, org: Org1, world: World) -> None:
    service, fake = world
    fake.auth = "header"
    args: dict[str, Any] = {
        "name": "Acme",
        "url": URL,
        "auth": "header",
        "header": "X-Api-Key",
        "oauth_scope": None,
        "title": "Acme tools",
        "scope": "workspace",
    }
    with pytest.raises(ConnectorError) as caught:
        await service.add_mcp(org.ids["org"], org.ids["ws"], member(org, "max"), **args)
    assert caught.value.status_code == 403
    admin = member(org, "mia", admin=True)
    for change, status in (
        ({"name": "gmail"}, 409),
        ({"name": "Bad Name"}, 422),
        ({"url": "http://mcp.remote.test/mcp"}, 422),
        ({"url": "https://private.remote.test/mcp"}, 422),
        ({"auth": "header", "header": None}, 422),
    ):
        with pytest.raises(ConnectorError) as caught:
            await service.add_mcp(org.ids["org"], org.ids["ws"], admin, **{**args, **change})
        assert caught.value.status_code == status, change
    started = await service.add_mcp(org.ids["org"], org.ids["ws"], admin, **args)
    view = started.connection
    assert (view.connector, view.state, view.scope) == ("acme", "pending", "workspace")
    custom = view.public()["mcp"]["custom"]
    assert custom == {"title": "Acme tools", "url": URL, "auth": "header", "header": "X-Api-Key"}
    done = await service.submit_form(
        org.ids["org"], view.id, {"token": GOOD_TOKEN}, actor=admin.user_id
    )
    assert done.state == "connected"
    tools = {t.name for t in await service.mcp_tools(org.claims())}
    assert tools == {"mcp_acme_list_items", "mcp_acme_create_item", "mcp_acme_drop_item"}
    listed = await service.list_for_agent(org.claims())
    assert "- acme (Acme tools, remote MCP server added by URL): connected (workspace)" in listed
    # A restart by name keeps the custom server and its filters.
    await service.set_filters(
        org.ids["org"], view.id, include=(), exclude=("drop_*",), actor=admin.user_id
    )
    again = await service.start(org.ids["org"], org.ids["ws"], admin, "acme", "workspace")
    assert again.connection.id == view.id
    assert again.connection.custom_mcp() is not None
    assert again.connection.filters == ((), ("drop_*",))
    # A new URL for the server: the old credential does not go to it.
    moved = await service.add_mcp(
        org.ids["org"], org.ids["ws"], admin, **{**args, "url": "https://other.remote.test/mcp"}
    )
    assert moved.connection.id == view.id
    checked = await service.check(org.ids["org"], view.id, actor=admin.user_id)
    assert checked.state == "pending"
    assert all(r.headers["host"] != "other.remote.test" for r in fake.seen)
    assert (await row_of(engine, view.id)).secret_ref is None
    assert await service.delete(org.ids["org"], view.id, actor=admin.user_id)
    assert await service.mcp_tools(org.claims()) == []


async def test_the_oauth_sign_in_connects_and_a_wrong_state_changes_nothing(
    engine: AsyncEngine, org: Org1, world: World
) -> None:
    service, fake = world
    fake.auth = "oauth"
    fake.tokens = set()
    mia = member(org, "mia", admin=True)
    started = await service.add_mcp(
        org.ids["org"],
        org.ids["ws"],
        mia,
        name="tracker",
        url=URL,
        auth="oauth",
        header=None,
        oauth_scope="read",
        title=None,
        scope="personal",
    )
    view = started.connection
    assert view.state == "initiated"
    assert started.redirect_url is not None
    assert "redirect_uri=https%3A%2F%2Fapp.sama.test%2Fconnections" in started.redirect_url
    assert "scope=read" in started.redirect_url
    code, state = fake.approve(started.redirect_url)
    with pytest.raises(ConnectorError) as caught:
        await service.finish_oauth(
            org.ids["org"],
            view.id,
            state=state[:-1] + "x",
            code=code,
            error=None,
            actor=mia.user_id,
        )
    assert caught.value.status_code == 400
    still = await service.get(org.ids["org"], view.id)
    assert still is not None
    assert still.state == "initiated"
    done = await service.finish_oauth(
        org.ids["org"], view.id, state=state, code=code, error=None, actor=mia.user_id
    )
    assert done.state == "connected"
    row = await row_of(engine, view.id)
    assert "access-1" not in json.dumps(row.config)
    assert row.config["oauth"]["client_id"] == "client-1"
    assert len(await service.mcp_tools(private(org))) == 3
    # The same state again: the sign-in is not open now.
    with pytest.raises(ConnectorError) as caught:
        await service.finish_oauth(
            org.ids["org"], view.id, state=state, code=code, error=None, actor=mia.user_id
        )
    assert caught.value.status_code == 409
    # The server forgets the tokens: the call refreshes once, then the connection expires.
    fake.tokens = set()
    fake.refresh_tokens = set()
    with pytest.raises(ConnectorError, match="connection_request"):
        await service.call_mcp(private(org), "mcp_tracker_list_items", {})
    expired = await service.get(org.ids["org"], view.id)
    assert expired is not None
    assert expired.state == "expired"
    # A new sign-in that the user refuses.
    again = await service.start(org.ids["org"], org.ids["ws"], mia, "tracker", "personal")
    assert again.redirect_url is not None
    _, state2 = fake.approve(again.redirect_url)
    refused = await service.finish_oauth(
        org.ids["org"],
        view.id,
        state=state2,
        code=None,
        error="access_denied",
        actor=mia.user_id,
    )
    assert refused.state == "failed"
    assert "access_denied" in (refused.last_error or "")


async def test_the_gate_knows_the_remote_tools_and_their_access(
    engine: AsyncEngine, org: Org1, world: World
) -> None:
    service, _ = world
    await connect_neon(service, org, "workspace")
    gate = GateService(engine, extra_toolsets=DeferredCatalog([]), run_tools=service)
    await grant(engine, org, "tool_search", "integrations")
    read = await gate.check(org.claims(), "c1", "tool_call", wrapped("mcp_neon_mcp_list_items"))
    assert read.action == "allow"
    drop = await gate.check(org.claims(), "c2", "tool_call", wrapped("mcp_neon_mcp_drop_item"))
    assert drop.action == "ask"
    unknown = await gate.check(org.claims(), "c3", "tool_call", wrapped("mcp_neon_mcp_nothing"))
    assert unknown.action == "deny"
    # Without the toolset, a remote tool is denied.
    await grant(engine, org, "tool_search")
    denied = await gate.check(org.claims(), "c4", "tool_call", wrapped("mcp_neon_mcp_list_items"))
    assert denied.action == "deny"


async def test_a_tool_name_that_two_connections_give_is_hidden(
    org: Org1, world: World, fake: FakeMcp
) -> None:
    service, _ = world
    fake.auth = "none"
    admin = member(org, "mia", admin=True)
    args: dict[str, Any] = {
        "url": URL,
        "auth": "none",
        "header": None,
        "oauth_scope": None,
        "title": None,
        "scope": "workspace",
    }
    first = await service.add_mcp(org.ids["org"], org.ids["ws"], admin, name="acme-x", **args)
    assert first.connection.state == "connected"
    names = {t.name for t in await service.mcp_tools(org.claims())}
    assert "mcp_acme_x_list_items" in names
    # ``acme_x`` gives the same sama names: no connection gets them.
    second = await service.add_mcp(org.ids["org"], org.ids["ws"], admin, name="acme_x", **args)
    assert second.connection.state == "connected"
    assert await service.mcp_tools(org.claims()) == []
    with pytest.raises(ConnectorError):
        await service.call_mcp(org.claims(), "mcp_acme_x_list_items", {})
    assert not any("tools/call" in r.content.decode() for r in fake.seen)


async def test_two_callbacks_at_once_connect_once(org: Org1, world: World, fake: FakeMcp) -> None:
    service, _ = world
    fake.auth = "oauth"
    fake.tokens = set()
    mia = member(org, "mia", admin=True)
    started = await service.add_mcp(
        org.ids["org"],
        org.ids["ws"],
        mia,
        name="tracker",
        url=URL,
        auth="oauth",
        header=None,
        oauth_scope=None,
        title=None,
        scope="personal",
    )
    assert started.redirect_url is not None
    code, state = fake.approve(started.redirect_url)
    view = started.connection

    async def callback() -> Any:
        return await service.finish_oauth(
            org.ids["org"], view.id, state=state, code=code, error=None, actor=mia.user_id
        )

    results = await asyncio.gather(callback(), callback(), return_exceptions=True)
    done = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, ConnectorError)]
    assert len(done) == 1
    assert done[0].state == "connected"
    assert len(refused) == 1
    assert refused[0].status_code in (400, 409)
    exchanges = [r for r in fake.token_requests if r.get("grant_type") == "authorization_code"]
    assert len(exchanges) == 1
    final = await service.get(org.ids["org"], view.id)
    assert final is not None
    assert final.state == "connected"


async def test_a_check_finishes_an_oauth_sign_in_after_a_server_outage(
    org: Org1, world: World, fake: FakeMcp
) -> None:
    service, _ = world
    fake.auth = "oauth"
    fake.tokens = set()
    mia = member(org, "mia", admin=True)
    started = await service.add_mcp(
        org.ids["org"],
        org.ids["ws"],
        mia,
        name="tracker",
        url=URL,
        auth="oauth",
        header=None,
        oauth_scope=None,
        title=None,
        scope="personal",
    )
    assert started.redirect_url is not None
    code, state = fake.approve(started.redirect_url)
    view = started.connection
    fake.mcp_down = True
    waiting = await service.finish_oauth(
        org.ids["org"], view.id, state=state, code=code, error=None, actor=mia.user_id
    )
    assert waiting.state == "pending"
    assert waiting.last_error
    fake.mcp_down = False
    # A check uses the stored tokens: no second sign-in and no second code exchange.
    done = await service.check(org.ids["org"], view.id, actor=mia.user_id)
    assert done.state == "connected"
    exchanges = [r for r in fake.token_requests if r.get("grant_type") == "authorization_code"]
    assert len(exchanges) == 1
