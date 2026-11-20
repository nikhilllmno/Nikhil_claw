"""The connection service with Postgres (docs/architecture.md, Section 10.3).

The fixtures are those of the gate tests: ``mia`` starts the run ``run`` in the shared session
``shared``, and the run ``secret-run`` in her private session ``secret``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.connectors.service import ConnectionService, ConnectorError, entity_id
from sama.db.models import AuditEntry, Connection, VaultSecret
from sama.gate.decide import policy_verdict
from sama.tenancy.access import Grants
from sama.tools.deferred import DeferredCatalog
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import Vault, connection_secret
from tests.connectors.support import NOTION_TOKEN, WEB, FakeComposio, FakeForm
from tests.connectors.support import service as make_service
from tests.db.test_tool_gate import Org1
from tests.db.test_tool_gate import org as gate_org

org = gate_org


@pytest.fixture
async def world(
    engine: AsyncEngine, org: Org1
) -> AsyncIterator[tuple[ConnectionService, FakeComposio, FakeForm]]:
    fake = FakeComposio()
    form = FakeForm()
    vault = Vault(engine, VaultKey(b"c" * KEY_BYTES))
    service = make_service(engine, vault, fake, form)
    yield service, fake, form
    await service.aclose()


def member(org: Org1, name: str, *, admin: bool = False) -> Grants:
    role = "admin" if admin else "member"
    return Grants(org.ids[name], ((None, "member"), (org.ids["ws"], role)))


def private(org: Org1) -> Any:
    return org.claims(run="secret-run", session_id=str(org.ids["secret"]))


async def connect_gmail(
    service: ConnectionService, fake: FakeComposio, org: Org1, scope: Any, who: Grants
) -> uuid.UUID:
    started = await service.start(org.ids["org"], org.ids["ws"], who, "gmail", scope)
    fake.status = "ACTIVE"
    view = await service.check(org.ids["org"], started.connection.id, actor=who.user_id)
    assert view.state == "connected"
    return view.id


async def test_a_composio_connection_is_connected_only_by_a_check(
    engine: AsyncEngine, org: Org1, world: Any
) -> None:
    service, fake, _ = world
    mia = member(org, "mia")
    started = await service.start(org.ids["org"], org.ids["ws"], mia, "gmail", "personal")
    assert started.redirect_url == "https://connect.composio.dev/link/1"
    view = started.connection
    assert (view.state, view.scope, view.owner_user_id) == ("initiated", "personal", mia.user_id)
    # One entity for each user.
    async with engine.connect() as connection:
        config = (
            await connection.execute(select(Connection.config).where(Connection.id == view.id))
        ).scalar_one()
    assert config["entity"] == entity_id(org.ids["org"], "personal", mia.user_id, org.ids["ws"])
    assert config["entity"].endswith(f"-u-{mia.user_id}")
    # The page comes back, but Composio still waits for the user.
    checked = await service.check(org.ids["org"], view.id, actor=mia.user_id)
    assert checked.state == "initiated"
    fake.status = "ACTIVE"
    checked = await service.check(org.ids["org"], view.id, actor=mia.user_id)
    assert checked.state == "connected"
    assert checked.last_health_at is not None
    async with engine.connect() as connection:
        actions = list(
            (
                await connection.execute(
                    select(AuditEntry.action).where(AuditEntry.org_id == org.ids["org"])
                )
            ).scalars()
        )
    assert "connection.started" in actions
    assert "connection.connected" in actions


async def test_a_workspace_connection_has_one_entity_and_needs_an_admin(
    org: Org1, world: Any
) -> None:
    service, _, _ = world
    with pytest.raises(ConnectorError) as caught:
        await service.start(org.ids["org"], org.ids["ws"], member(org, "max"), "gmail", "workspace")
    assert caught.value.status_code == 403
    started = await service.start(
        org.ids["org"], org.ids["ws"], member(org, "max", admin=True), "gmail", "workspace"
    )
    assert started.connection.owner_user_id is None
    assert f"-w-{org.ids['ws']}" in entity_id(org.ids["org"], "workspace", None, org.ids["ws"])


async def test_a_new_link_forgets_the_old_composio_account(org: Org1, world: Any) -> None:
    service, fake, _ = world
    mia = member(org, "mia")
    first = await service.start(org.ids["org"], org.ids["ws"], mia, "gmail", "personal")
    second = await service.start(org.ids["org"], org.ids["ws"], mia, "gmail", "personal")
    assert first.connection.id == second.connection.id
    assert fake.deleted == ["ca_1"]
    assert await service.delete(org.ids["org"], second.connection.id, actor=mia.user_id)
    assert fake.deleted == ["ca_1", "ca_2"]


async def test_an_old_sign_in_expires_after_30_minutes(
    engine: AsyncEngine, org: Org1, world: Any
) -> None:
    service, _, _ = world
    mia = member(org, "mia")
    started = await service.start(org.ids["org"], org.ids["ws"], mia, "gmail", "personal")
    async with engine.begin() as connection:
        await connection.execute(
            update(Connection)
            .where(Connection.id == started.connection.id)
            .values(updated_at=text("now() - interval '31 minutes'"))
        )
    views = await service.list_connections(org.ids["org"], org.ids["ws"], mia.user_id)
    assert [(v.state, v.last_error is not None) for v in views] == [("expired", True)]


async def test_a_check_keeps_the_state_when_composio_does_not_answer(org: Org1, world: Any) -> None:
    service, fake, _ = world
    mia = member(org, "mia")
    connection_id = await connect_gmail(service, fake, org, "personal", mia)
    fake.status = "DOWN"
    view = await service.check(org.ids["org"], connection_id, actor=None)
    assert view.state == "connected"
    assert view.last_error
    fake.status = "GONE"
    view = await service.check(org.ids["org"], connection_id, actor=None)
    assert view.state == "expired"


async def test_a_check_does_not_start_an_expired_sign_in_again(
    engine: AsyncEngine, org: Org1, world: Any
) -> None:
    service, fake, _ = world
    mia = member(org, "mia")
    started = await service.start(org.ids["org"], org.ids["ws"], mia, "gmail", "personal")
    async with engine.begin() as connection:
        await connection.execute(
            update(Connection)
            .where(Connection.id == started.connection.id)
            .values(updated_at=text("now() - interval '31 minutes'"))
        )
    # Composio still waits for the user, but the link is too old.
    view = await service.check(org.ids["org"], started.connection.id, actor=mia.user_id)
    assert view.state == "expired"
    assert view.last_error
    view = await service.check(org.ids["org"], started.connection.id, actor=mia.user_id)
    assert view.state == "expired"
    # A late sign-in that Composio accepted still connects.
    fake.status = "ACTIVE"
    view = await service.check(org.ids["org"], started.connection.id, actor=mia.user_id)
    assert view.state == "connected"


async def test_a_network_error_keeps_a_connected_form_connection(org: Org1, world: Any) -> None:
    service, _, form = world
    mia = member(org, "mia")
    started = await service.start(org.ids["org"], org.ids["ws"], mia, "notion", "personal")
    view = await service.submit_form(
        org.ids["org"], started.connection.id, {"token": NOTION_TOKEN}, actor=mia.user_id
    )
    assert view.state == "connected"
    form.down = True
    view = await service.check(org.ids["org"], view.id, actor=None)
    assert view.state == "connected"
    assert view.last_error
    assert "cannot be reached" in view.last_error


async def test_a_disable_during_a_check_wins(org: Org1, world: Any) -> None:
    service, _, form = world
    admin = member(org, "max", admin=True)
    started = await service.start(org.ids["org"], org.ids["ws"], admin, "notion", "workspace")
    view = await service.submit_form(
        org.ids["org"], started.connection.id, {"token": NOTION_TOKEN}, actor=admin.user_id
    )
    assert view.state == "connected"

    async def disable() -> None:
        form.during_test = None
        await service.disable(org.ids["org"], view.id, actor=admin.user_id)

    # A member checks; the admin disables the connection before the API answers.
    form.during_test = disable
    checked = await service.check(org.ids["org"], view.id, actor=None)
    assert checked.state == "disabled"
    current = await service.get(org.ids["org"], view.id)
    assert current is not None
    assert current.state == "disabled"


async def test_a_form_secret_goes_only_to_the_vault(
    engine: AsyncEngine, org: Org1, world: Any
) -> None:
    service, _, _ = world
    mia = member(org, "mia")
    started = await service.start(org.ids["org"], org.ids["ws"], mia, "notion", "personal")
    assert (started.connection.state, started.redirect_url) == ("pending", None)
    wrong = await service.submit_form(
        org.ids["org"], started.connection.id, {"token": "wrong"}, actor=mia.user_id
    )
    assert wrong.state == "failed"
    view = await service.submit_form(
        org.ids["org"], started.connection.id, {"token": NOTION_TOKEN}, actor=mia.user_id
    )
    assert view.state == "connected"
    assert NOTION_TOKEN not in json.dumps(view.public())
    async with engine.connect() as connection:
        row = (await connection.execute(select(Connection).where(Connection.id == view.id))).one()
        names = (
            await connection.execute(
                select(VaultSecret.name).where(VaultSecret.org_id == org.ids["org"])
            )
        ).scalars()
        assert list(names) == [str(connection_secret(view.id))]
        dumped = (
            await connection.execute(
                text("SELECT row_to_json(c)::text FROM connections c WHERE id = :id"),
                {"id": view.id},
            )
        ).scalar_one()
    assert row.secret_ref == f"connection:{view.id}"
    assert NOTION_TOKEN not in dumped
    with pytest.raises(ConnectorError, match="unknown fields"):
        await service.submit_form(
            org.ids["org"], view.id, {"token": "x", "other": "y"}, actor=mia.user_id
        )
    # A delete removes the vault entry too.
    assert await service.delete(org.ids["org"], view.id, actor=mia.user_id)
    async with engine.connect() as connection:
        left = (
            await connection.execute(
                select(VaultSecret.name).where(VaultSecret.org_id == org.ids["org"])
            )
        ).scalars()
        assert list(left) == []


async def test_a_personal_connection_is_not_usable_in_a_shared_session(
    org: Org1, world: Any
) -> None:
    service, fake, _ = world
    await connect_gmail(service, fake, org, "personal", member(org, "mia"))
    shared, secret = org.claims(), private(org)
    read = "gmail_fetch_emails"
    # The private session of mia uses her personal connection.
    assert read in await service.usable_tools(secret, [read, "todo_write"])
    assert "connected (personal)" in await service.list_for_agent(secret)
    answer = await service.call(secret, read, {"query": "is:unread"})
    assert "connector:gmail" in answer
    assert fake.executed[-1]["user_id"].endswith(f"-u-{org.ids['mia']}")
    # The shared session, where mia starts the run, does not see it.
    assert await service.usable_tools(shared, [read, "todo_write"]) == {"todo_write"}
    listed = await service.list_for_agent(shared)
    assert "gmail (Gmail): not connected" in listed
    with pytest.raises(ConnectorError, match="no workspace connection"):
        await service.call(shared, read, {})
    with pytest.raises(ConnectorError, match="shared session cannot use a personal"):
        await service.request_for_agent(shared, "gmail", "personal")
    assert "not connected" in await service.test_for_agent(shared, "gmail")


async def test_a_workspace_connection_is_usable_in_both_sessions(org: Org1, world: Any) -> None:
    service, fake, _ = world
    await connect_gmail(service, fake, org, "workspace", member(org, "mia", admin=True))
    read = "gmail_fetch_emails"
    for claims in (org.claims(), private(org)):
        assert read in await service.usable_tools(claims, [read])
        await service.call(claims, read, {})
        assert fake.executed[-1]["user_id"].endswith(f"-w-{org.ids['ws']}")


async def test_a_private_session_uses_the_personal_connection_first(org: Org1, world: Any) -> None:
    service, fake, _ = world
    await connect_gmail(service, fake, org, "workspace", member(org, "mia", admin=True))
    await connect_gmail(service, fake, org, "personal", member(org, "mia"))
    await service.call(private(org), "gmail_fetch_emails", {})
    assert fake.executed[-1]["user_id"].endswith(f"-u-{org.ids['mia']}")
    await service.call(org.claims(), "gmail_fetch_emails", {})
    assert fake.executed[-1]["user_id"].endswith(f"-w-{org.ids['ws']}")


async def test_a_disabled_connection_has_no_tools(org: Org1, world: Any) -> None:
    service, fake, _ = world
    mia = member(org, "mia")
    connection_id = await connect_gmail(service, fake, org, "personal", mia)
    await service.disable(org.ids["org"], connection_id, actor=mia.user_id)
    # A check does not turn on a disabled connection.
    assert (await service.check(org.ids["org"], connection_id, actor=None)).state == "disabled"
    assert await service.usable_tools(private(org), ["gmail_fetch_emails"]) == frozenset()


async def test_the_agent_gets_a_link_and_never_a_secret(org: Org1, world: Any) -> None:
    service, _, _ = world
    answer = await service.request_for_agent(private(org), "notion", None)
    assert f"{WEB}/connections?workspace={org.ids['ws']}&connect=notion&scope=personal" in answer
    shared = await service.request_for_agent(org.claims(), "gmail", None)
    assert "scope=workspace" in shared
    assert "workspace admin" in shared
    with pytest.raises(ConnectorError, match="no connector"):
        await service.request_for_agent(org.claims(), "nope", None)


def test_connect_reply_gives_a_link_and_a_warning(world: Any) -> None:
    service, _, _ = world
    ws = uuid.uuid4()
    reply = service.connect_reply(ws, " Gmail ", shared=False, admin=False)
    assert f"{WEB}/connections?workspace={ws}&connect=gmail&scope=personal" in reply
    assert "Do not" in reply
    shared = service.connect_reply(ws, "gmail", shared=True, admin=False)
    assert "scope=workspace" in shared
    assert "workspace admin" in shared
    assert "Connectors: " in service.connect_reply(ws, "", shared=False, admin=False)
    assert "no connector" in service.connect_reply(ws, "nope", shared=False, admin=False)


def test_the_tools_are_deferred_with_their_access(world: Any) -> None:
    service, _, _ = world
    catalog = DeferredCatalog([])
    service.register_tools(catalog)
    access = catalog.access()
    assert access["gmail_fetch_emails"] == "read"
    assert access["gmail_send_email"] == "write"
    names = set(service.catalog.tool_names())
    assert names == set(access)
    assert {"read", "write", "destructive"} <= set(access.values())
    for name in names:
        tool = catalog.get(name)
        assert tool is not None
        assert tool.toolset == "integrations"
        verdict = policy_verdict([], name, access=access)
        assert verdict.action == ("allow" if access[name] == "read" else "ask"), name
        assert policy_verdict([], name, shared=True, access=access).action == verdict.action
