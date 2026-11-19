"""Tests of the Slack setup, the Slack connect, and the Slack pairing routes (Sections 11.3 and
11.6)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import JwtVerifier, SamaApi
from sama.db.models import AuditEntry, ChannelIdentity, Integration, PairingCode
from sama.gateway.pairing import redeem
from sama.integrations.service import IntegrationError, Integrations, secret_hash
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import SecretName, Vault
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.api.test_app import add_user, mia
from tests.api.test_telegram_routes import new_code
from tests.integrations.support import APP_ID, SIGNING, SLACK_TOKEN, TEAM, FakeSlack
from tests.orchestrator.conftest import Tenant, add_tenant

pytestmark = pytest.mark.usefixtures("tenant")

SECOND_TOKEN = "xoxb-" + "2" * 12 + "-" + "c" * 24


@pytest.fixture
def slack() -> FakeSlack:
    fake = FakeSlack()
    fake.teams[SECOND_TOKEN] = {
        "team_id": "T0SECOND",
        "team": "Second",
        "user_id": "UBOT2",
        "bot_id": "B2",
    }
    return fake


@pytest.fixture
def vault(engine: AsyncEngine) -> Vault:
    return Vault(engine, VaultKey(b"j" * KEY_BYTES))


@pytest.fixture
def integrations(engine: AsyncEngine, vault: Vault, slack: FakeSlack) -> Integrations:
    return Integrations(engine, vault, public_url="https://sama.test/", slack_client=slack.client)


@pytest.fixture
async def http(engine: AsyncEngine, integrations: Integrations) -> AsyncIterator[httpx.AsyncClient]:
    api = SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        integrations=integrations,
    )
    async with engine.begin() as connection:
        await connection.execute(delete(ChannelIdentity))
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        yield client


def connect_body(token: str = SLACK_TOKEN, signing: str = SIGNING) -> dict[str, str]:
    return {"bot_token": f" {token} ", "signing_secret": signing}


async def test_an_admin_sets_up_connects_and_disconnects_slack(
    http: httpx.AsyncClient,
    engine: AsyncEngine,
    tenant: Tenant,
    slack: FakeSlack,
    vault: Vault,
) -> None:
    admin = bearer(await add_user(engine, tenant, workspace_member=True, role="admin"))
    url = f"/orgs/{tenant.org}/integrations/slack"
    early = await http.post(url, headers=admin, json=connect_body())
    assert early.status_code == 409, "the app needs the URLs of the setup first"

    setup = (await http.post(f"{url}/setup", headers=admin)).json()
    row_id = setup["integration"]["id"]
    assert setup["integration"]["status"] == "pending"
    assert setup["events_url"] == f"https://sama.test/slack/{row_id}/events"
    assert setup["commands_url"] == f"https://sama.test/slack/{row_id}/commands"
    assert setup["manifest_link"].startswith("https://api.slack.com/apps?new_app=1&manifest_json=")
    again = (await http.post(f"{url}/setup", headers=admin)).json()
    assert again["integration"]["id"] == row_id, "the setup keeps its row"
    me = (await http.get("/me", headers=mia(tenant))).json()["orgs"][0]
    assert me["integrations"] == [{"kind": "slack", "status": "pending", "name": None}]
    pairing = await http.post(
        "/channels/slack/pairing-codes", headers=mia(tenant), json=new_code(tenant)
    )
    assert pairing.status_code == 409, "a pending app cannot pair"

    response = await http.post(url, headers=admin, json=connect_body())
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["id"], body["status"], body["name"]) == (row_id, "active", "Sama HQ")
    assert SLACK_TOKEN not in response.text
    async with engine.connect() as connection:
        row = (
            await connection.execute(select(Integration).where(Integration.id == uuid.UUID(row_id)))
        ).one()
    assert (row.external_id, row.secret_hash) == (TEAM, secret_hash(SIGNING))
    assert row.settings["app_id"] == APP_ID
    token = await vault.get(tenant.org, SecretName.SLACK_BOT_TOKEN)
    assert token is not None
    assert token.get_secret_value() == SLACK_TOKEN
    signing = await vault.get(tenant.org, SecretName.SLACK_SIGNING_SECRET)
    assert signing is not None
    assert signing.get_secret_value() == SIGNING

    code = (
        await http.post("/channels/slack/pairing-codes", headers=mia(tenant), json=new_code(tenant))
    ).json()
    assert code["link"] == f"https://slack.com/app_redirect?app={APP_ID}&team={TEAM}"
    assert code["command"] == f"/sama pair {code['code']}"
    async with engine.begin() as connection:
        linked = await redeem(
            connection,
            integration_id=uuid.UUID(row_id),
            code=code["code"],
            kind="user",
            external_id="UMIA0001",
        )
    assert linked is not None
    listed = (await http.get("/channels/slack/identities", headers=mia(tenant))).json()
    assert len(listed) == 1
    assert (await http.get("/channels/telegram/identities", headers=mia(tenant))).json() == []
    wrong_channel = await http.delete(
        f"/channels/telegram/identities/{listed[0]['id']}", headers=mia(tenant)
    )
    assert wrong_channel.status_code == 404

    assert (await http.delete(url, headers=admin)).status_code == 204
    assert len(slack.method_calls("auth.revoke")) == 1, "the token is revoked"
    assert await vault.get(tenant.org, SecretName.SLACK_BOT_TOKEN) is None
    assert await vault.get(tenant.org, SecretName.SLACK_SIGNING_SECRET) is None
    assert (await http.get("/channels/slack/identities", headers=mia(tenant))).json() == []
    async with engine.connect() as connection:
        actions = (
            await connection.execute(
                select(AuditEntry.action)
                .where(AuditEntry.org_id == tenant.org, AuditEntry.target_type == "integration")
                .order_by(AuditEntry.id)
            )
        ).scalars()
        assert list(actions) == [
            "integration.created",
            "integration.connected",
            "integration.disconnected",
        ]
    assert (await http.delete(url, headers=admin)).status_code == 404


async def test_a_pending_setup_can_be_removed(
    http: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant, slack: FakeSlack
) -> None:
    admin = bearer(await add_user(engine, tenant, workspace_member=True, role="admin"))
    url = f"/orgs/{tenant.org}/integrations/slack"
    assert (await http.post(f"{url}/setup", headers=admin)).status_code == 200
    assert (await http.delete(url, headers=admin)).status_code == 204
    assert slack.calls == [], "a pending app has no token to revoke"


async def test_wrong_secrets_are_refused(
    http: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant, slack: FakeSlack
) -> None:
    admin = bearer(await add_user(engine, tenant, workspace_member=True, role="admin"))
    url = f"/orgs/{tenant.org}/integrations/slack"
    await http.post(f"{url}/setup", headers=admin)
    for body in (
        connect_body(token="xoxp-" + "1" * 30),
        connect_body(signing="not-a-secret"),
    ):
        response = await http.post(url, headers=admin, json=body)
        assert response.status_code == 400, response.text
    assert slack.calls == [], "a wrong format does not reach Slack"
    unknown = "xoxb-" + "9" * 30
    response = await http.post(url, headers=admin, json=connect_body(token=unknown))
    assert response.status_code == 400
    assert "did not accept the token" in response.json()["detail"]
    assert unknown not in response.text


async def test_only_an_admin_sets_up_slack(
    http: httpx.AsyncClient, tenant: Tenant, slack: FakeSlack
) -> None:
    url = f"/orgs/{tenant.org}/integrations/slack"
    assert (await http.post(f"{url}/setup", headers=mia(tenant))).status_code == 403
    assert (await http.post(url, headers=mia(tenant), json=connect_body())).status_code == 403
    assert (await http.post(f"{url}/setup", headers=bearer(uuid.uuid4()))).status_code == 404
    assert slack.calls == []


async def test_a_workspace_belongs_to_one_org(
    integrations: Integrations, engine: AsyncEngine, tenant: Tenant
) -> None:
    other = await add_tenant(engine)
    await integrations.setup_slack(tenant.org, tenant.ids["mia"])
    await integrations.connect_slack(
        tenant.org, tenant.ids["mia"], SecretStr(SLACK_TOKEN), SecretStr(SIGNING)
    )
    await integrations.setup_slack(other.org, other.ids["mia"])
    with pytest.raises(IntegrationError) as caught:
        await integrations.connect_slack(
            other.org, other.ids["mia"], SecretStr(SLACK_TOKEN), SecretStr(SIGNING)
        )
    assert caught.value.status_code == 409


async def test_another_workspace_drops_the_links_of_the_old_one(
    integrations: Integrations, engine: AsyncEngine, tenant: Tenant
) -> None:
    user = tenant.ids["mia"]
    setup = await integrations.setup_slack(tenant.org, user)
    first = await integrations.connect_slack(
        tenant.org, user, SecretStr(SLACK_TOKEN), SecretStr(SIGNING)
    )
    async with engine.begin() as connection:
        await connection.execute(
            insert(ChannelIdentity).values(
                org_id=tenant.org,
                integration_id=first.id,
                channel="slack",
                external_id="UMIA0001",
                user_id=user,
                workspace_id=tenant.ids["ws"],
                agent_profile_id=tenant.ids["profile"],
            )
        )

    async def links() -> int:
        async with engine.connect() as connection:
            return int(
                await connection.scalar(
                    select(func.count())
                    .select_from(ChannelIdentity)
                    .where(ChannelIdentity.integration_id == first.id)
                )
                or 0
            )

    # The same workspace keeps the links (for example a new signing secret).
    await integrations.connect_slack(tenant.org, user, SecretStr(SLACK_TOKEN), SecretStr("8" * 32))
    assert await links() == 1
    second = await integrations.connect_slack(
        tenant.org, user, SecretStr(SECOND_TOKEN), SecretStr(SIGNING)
    )
    assert (second.id, second.name) == (setup.integration.id, "Second")
    assert await links() == 0
    async with engine.connect() as connection:
        codes = await connection.scalar(
            select(func.count()).select_from(PairingCode).where(PairingCode.org_id == tenant.org)
        )
    assert codes == 0
