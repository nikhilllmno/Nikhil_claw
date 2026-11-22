"""Connect from the chat with Postgres (docs/architecture.md, Section 10.3.4, M8.1).

The fixtures are those of the gate tests: ``mia`` (a member of ``ws``) starts the run ``run``
in the shared session ``shared``, and the run ``secret-run`` in her private session ``secret``.
``admin`` is an org admin.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.connectors.chat import cards, connection_escalation
from sama.connectors.composio import ComposioClient
from sama.connectors.service import ConnectionService, ConnectionView, ConnectorError
from sama.db.models import AuditEntry, Connection, Membership
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import OrgKeys, Vault
from tests.connectors.fake_mcp import URL, FakeMcp, driver
from tests.connectors.support import FakeComposio, FakeForm
from tests.connectors.support import service as make_service
from tests.db.test_connections import member, private
from tests.db.test_tool_gate import Org1
from tests.db.test_tool_gate import org as gate_org

org = gate_org

RETURN = "https://api.sama.test"


class Resumed:
    """``on_connected``: records each continued session."""

    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, ConnectionView, dict[str, str]]] = []

    async def __call__(
        self, org_id: uuid.UUID, view: ConnectionView, origin: Mapping[str, str]
    ) -> None:
        self.calls.append((org_id, view, dict(origin)))


World = tuple[ConnectionService, FakeComposio, FakeMcp, Resumed]


@pytest.fixture
async def world(engine: AsyncEngine, org: Org1) -> AsyncIterator[World]:
    fake = FakeComposio()
    mcp = FakeMcp("oauth")
    mcp.tokens = set()
    vault = Vault(engine, VaultKey(b"q" * KEY_BYTES))
    service = make_service(engine, vault, fake, FakeForm(), mcp=driver(mcp, vault))
    service.return_base = RETURN
    resumed = Resumed()
    service.on_connected = resumed
    yield service, fake, mcp, resumed
    await service.aclose()


async def row_of(engine: AsyncEngine, connection_id: Any) -> Any:
    async with engine.connect() as connection:
        return (
            await connection.execute(select(Connection).where(Connection.id == connection_id))
        ).one()


async def actions(engine: AsyncEngine, org: Org1) -> list[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(AuditEntry.action).where(AuditEntry.org_id == org.ids["org"])
        )
        return list(rows.scalars())


async def make_admin(engine: AsyncEngine, org: Org1, name: str) -> None:
    """Gives ``name`` the role ``admin`` in the org and in the workspace."""
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership)
            .where(Membership.org_id == org.ids["org"], Membership.user_id == org.ids[name])
            .values(role="admin")
        )


async def test_connect_my_gmail_gives_a_sign_in_card_and_continues_the_session(
    engine: AsyncEngine, org: Org1, world: World
) -> None:
    service, fake, _, resumed = world
    claims = private(org)
    answer = await service.request_for_agent(claims, "gmail", None)
    assert "https://connect.composio.dev/link/1" in answer
    assert "Do not ask the user to tell you when they are done" in answer
    [found] = cards(answer)
    assert found["title"] == "Gmail"
    assert found["url"] == "https://connect.composio.dev/link/1"
    assert found["state"] == "initiated"
    connection_id = uuid.UUID(found["connection_id"])
    row = await row_of(engine, connection_id)
    assert row.state == "initiated"
    assert row.config["via"] == "chat"
    assert row.config["origin"]["session_id"] == str(org.ids["secret"])
    assert row.config["origin"]["user_id"] == str(org.ids["mia"])
    # The return page needs the token of this start.
    with pytest.raises(ConnectorError) as caught:
        await service.finish_return(connection_id, "not-the-token-at-all")
    assert caught.value.status_code == 403
    still, via = await service.finish_return(connection_id, row.config["return_token"])
    assert (still.state, via) == ("initiated", "chat")
    assert resumed.calls == []
    fake.status = "ACTIVE"
    done, _ = await service.finish_return(connection_id, row.config["return_token"])
    assert done.state == "connected"
    assert len(resumed.calls) == 1
    org_id, view, origin = resumed.calls[0]
    assert org_id == org.ids["org"]
    assert view.connector == "gmail"
    assert origin["session_id"] == str(org.ids["secret"])
    # The origin is used once: a second check does not continue the session again.
    await service.check(org.ids["org"], connection_id, actor=None)
    assert len(resumed.calls) == 1
    assert "origin" not in (await row_of(engine, connection_id)).config
    # Already connected: the agent uses the tools at once.
    again = await service.request_for_agent(claims, "gmail", None)
    assert "already connected" in again


async def test_the_composio_callback_is_the_return_page_of_the_control_plane(
    org: Org1, world: World
) -> None:
    service, fake, _, _ = world
    calls: list[str] = []
    original = fake.__call__

    def spy(request: Any) -> Any:
        if request.url.path.endswith("/connected_accounts/link"):
            calls.append(request.content.decode())
        return original(request)

    service._composio = ComposioClient(transport=httpx.MockTransport(spy))
    await service.request_for_agent(private(org), "googlecalendar", None)
    assert f"{RETURN}/connections/return?connection=" in calls[0].replace("\\/", "/")


async def test_words_of_the_user_map_to_connectors(org: Org1, world: World) -> None:
    service, _, _, _ = world
    claims = private(org)
    email = await service.request_for_agent(claims, "my email", None)
    assert "Gmail (gmail)" in email
    assert "Outlook (outlook)" in email
    assert "Ask the user which one" in email
    calendar = await service.request_for_agent(claims, "calendar", None)
    assert cards(calendar)[0]["connector"] == "googlecalendar"
    # "slack" is the Slack account of the user, not the chat app.
    assert await service.channel_for_agent(claims, "slack") is None
    slack = await service.request_for_agent(claims, "slack", None)
    assert cards(slack)[0]["connector"] == "slack"
    with pytest.raises(ConnectorError, match="connection_add_mcp"):
        await service.request_for_agent(claims, "composio-thing", None)


async def test_a_member_gets_the_admins_to_ask_and_a_request_in_the_audit_log(
    engine: AsyncEngine, org: Org1, world: World
) -> None:
    service, _, _, _ = world
    shared = org.claims()
    # A workspace connection must say so, so the gate asks a person.
    first = await service.request_for_agent(shared, "gmail", None)
    assert "scope 'workspace'" in first
    assert connection_escalation("connection_request", {"scope": "workspace"}) is not None
    assert connection_escalation("mcp__sama__connection_request", {"scope": "workspace"})
    assert connection_escalation("connection_request", {"scope": "personal"}) is None
    answer = await service.request_for_agent(shared, "gmail", "workspace")
    assert "cannot connect Gmail for the whole workspace" in answer
    assert "ask" in answer
    guide = await service.channel_for_agent(private(org), "slack_app")
    assert guide is not None
    assert "only an org owner or admin can" in guide
    assert "1. " not in guide
    assert (await actions(engine, org)).count("connection.requested") == 2
    listed = await service.list_for_agent(private(org))
    assert "The user is the org member." in listed
    assert "via Composio" in listed


async def test_an_admin_gets_the_steps_of_a_channel_and_a_workspace_sign_in(
    engine: AsyncEngine, org: Org1, world: World
) -> None:
    service, _, _, _ = world
    await make_admin(engine, org, "mia")
    guide = await service.channel_for_agent(private(org), "telegram")
    assert guide is not None
    assert "1. " in guide
    answer = await service.request_for_agent(org.claims(), "gmail", "workspace")
    [found] = cards(answer)
    assert found["scope"] == "workspace"
    assert found["url"] is not None


async def test_a_missing_composio_key_shows_in_plain_words_and_writes_no_row(
    engine: AsyncEngine, org: Org1, world: World
) -> None:
    service, _, _, _ = world
    service._keys = OrgKeys(None, {})
    listed = await service.list_for_agent(private(org))
    assert "gmail (Gmail, via Composio): not available: Gmail signs in through Composio" in listed
    answer = await service.request_for_agent(private(org), "gmail", None)
    assert "cannot connect now" in answer
    assert "Composio is not set up" in answer
    with pytest.raises(ConnectorError) as caught:
        await service.start(org.ids["org"], org.ids["ws"], member(org, "mia"), "gmail", "personal")
    assert caught.value.status_code == 409
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(Connection.id).where(Connection.org_id == org.ids["org"])
            )
        ).all()
    assert rows == []


async def test_a_stuck_composio_row_without_an_account_checks_as_failed(
    engine: AsyncEngine, org: Org1, world: World
) -> None:
    service, _, _, _ = world
    started = await service.start(
        org.ids["org"], org.ids["ws"], member(org, "mia"), "gmail", "personal"
    )
    async with engine.begin() as connection:
        await connection.execute(
            update(Connection)
            .where(Connection.id == started.connection.id)
            .values(state="pending", config={})
        )
    checked = await service.check(org.ids["org"], started.connection.id, actor=None)
    assert checked.state == "failed"
    assert checked.last_error is not None


async def test_the_poll_connects_an_open_chat_sign_in(
    engine: AsyncEngine, org: Org1, world: World
) -> None:
    service, fake, _, resumed = world
    answer = await service.request_for_agent(private(org), "github", None)
    connection_id = uuid.UUID(cards(answer)[0]["connection_id"])
    assert await service.poll_signins() == 1
    assert resumed.calls == []
    fake.status = "ACTIVE"
    assert await service.poll_signins() == 1
    assert (await row_of(engine, connection_id)).state == "connected"
    assert len(resumed.calls) == 1
    # A connected row is not polled again.
    assert await service.poll_signins() == 0


async def test_a_late_sign_in_does_not_continue_the_session(
    engine: AsyncEngine, org: Org1, world: World
) -> None:
    service, fake, _, resumed = world
    answer = await service.request_for_agent(private(org), "linear", None)
    connection_id = uuid.UUID(cards(answer)[0]["connection_id"])
    row = await row_of(engine, connection_id)
    old = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    async with engine.begin() as connection:
        await connection.execute(
            update(Connection)
            .where(Connection.id == connection_id)
            .values(config={**row.config, "origin": {**row.config["origin"], "at": old}})
        )
    fake.status = "ACTIVE"
    done = await service.check(org.ids["org"], connection_id, actor=None)
    assert done.state == "connected"
    assert resumed.calls == []


async def test_a_server_by_url_from_the_chat_signs_in_on_the_return_page(
    engine: AsyncEngine, org: Org1, world: World
) -> None:
    service, _, mcp, resumed = world
    refused = await service.add_mcp_for_agent(
        private(org), url=URL, name=None, title=None, auth="oauth", scope=None
    )
    assert "Only a workspace admin or the org owner can add an MCP server" in refused
    await make_admin(engine, org, "mia")
    answer = await service.add_mcp_for_agent(
        private(org), url=URL, name="tracker", title="Tracker", auth="oauth", scope=None
    )
    [found] = cards(answer)
    assert found["title"] == "Tracker"
    url = found["url"]
    assert url is not None
    query = parse_qs(urlsplit(url).query)
    assert query["redirect_uri"] == [f"{RETURN}/connections/oauth/return"]
    code, state = mcp.approve(url)
    with pytest.raises(ConnectorError):
        await service.finish_oauth_return(state=state[:-2] + "zz", code=code, error=None)
    done, via = await service.finish_oauth_return(state=state, code=code, error=None)
    assert (done.state, via) == ("connected", "chat")
    assert len(resumed.calls) == 1
    assert len(await service.mcp_tools(private(org))) == 3
    # The same return again changes nothing.
    with pytest.raises(ConnectorError):
        await service.finish_oauth_return(state=state, code=code, error=None)
    assert len(resumed.calls) == 1
