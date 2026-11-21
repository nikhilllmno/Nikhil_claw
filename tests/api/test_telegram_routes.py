"""Tests of the Telegram pairing routes and the integration routes of the HTTP API (Sections
11.3 and 11.6)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import JwtVerifier, SamaApi
from sama.db.models import AuditEntry, ChannelIdentity, Integration, PairingCode
from sama.gateway.pairing import code_hash, redeem
from sama.integrations.service import IntegrationError, Integrations
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import SecretName, Vault
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.api.test_app import add_user, mia
from tests.integrations.support import TOKEN, FakeTelegram, add_bot
from tests.orchestrator.conftest import Tenant, add_tenant

pytestmark = pytest.mark.usefixtures("tenant")


@pytest.fixture
def telegram() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def vault(engine: AsyncEngine) -> Vault:
    return Vault(engine, VaultKey(b"i" * KEY_BYTES))


@pytest.fixture
async def tg(
    engine: AsyncEngine, tenant: Tenant, telegram: FakeTelegram, vault: Vault
) -> AsyncIterator[httpx.AsyncClient]:
    """The API with integrations, and an org with an active Telegram bot."""
    await add_bot(engine, tenant)
    api = SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        integrations=Integrations(
            engine, vault, public_url="https://sama.test", telegram_client=telegram.client
        ),
    )
    async with engine.begin() as connection:
        await connection.execute(delete(ChannelIdentity))
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        yield client


def new_code(tenant: Tenant, kind: str = "user") -> dict[str, str]:
    return {
        "workspace_id": str(tenant.ids["ws"]),
        "agent_profile_id": str(tenant.ids["profile"]),
        "kind": kind,
    }


async def test_pairing_is_off_without_a_bot(client: httpx.AsyncClient, tenant: Tenant) -> None:
    response = await client.post(
        "/channels/telegram/pairing-codes", headers=mia(tenant), json=new_code(tenant)
    )
    assert response.status_code == 409


async def test_a_user_makes_a_code_and_links_an_account(
    tg: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    assert (await tg.get("/config")).json()["integrations"] is True
    response = await tg.post(
        "/channels/telegram/pairing-codes", headers=mia(tenant), json=new_code(tenant)
    )
    assert response.status_code == 201
    body = response.json()
    assert (body["kind"], body["bot_username"]) == ("user", "sama_bot")
    code = body["code"]
    assert body["link"] == f"https://t.me/sama_bot?start={code}"
    async with engine.begin() as connection:
        stored = (
            await connection.execute(
                select(PairingCode.code_hash).where(PairingCode.org_id == tenant.org)
            )
        ).scalars()
        assert list(stored) == [code_hash(code)], "only the digest is stored"
        linked = await redeem(
            connection,
            integration_id=tenant.ids["bot"],
            code=code,
            kind="user",
            external_id="555",
        )
    assert linked is not None
    assert linked.user_id == tenant.ids["mia"]

    listed = (await tg.get("/channels/telegram/identities", headers=mia(tenant))).json()
    assert [(i["org_id"], i["workspace_id"]) for i in listed] == [
        (str(tenant.org), str(tenant.ids["ws"]))
    ]
    other = await add_user(engine, tenant, workspace_member=True)
    assert (await tg.get("/channels/telegram/identities", headers=bearer(other))).json() == []
    stolen = await tg.delete(
        f"/channels/telegram/identities/{listed[0]['id']}", headers=bearer(other)
    )
    assert stolen.status_code == 404

    gone = await tg.delete(f"/channels/telegram/identities/{listed[0]['id']}", headers=mia(tenant))
    assert gone.status_code == 204
    assert (await tg.get("/channels/telegram/identities", headers=mia(tenant))).json() == []
    async with engine.connect() as connection:
        actions = (
            await connection.execute(
                select(AuditEntry.action).where(
                    AuditEntry.org_id == tenant.org, AuditEntry.target_id == "telegram:555"
                )
            )
        ).scalars()
        assert sorted(actions) == ["channel_identity.linked", "channel_identity.unlinked"]


async def test_a_code_needs_access_to_the_workspace(
    tg: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    outsider = await add_user(engine, tenant, workspace_member=False)
    response = await tg.post(
        "/channels/telegram/pairing-codes", headers=bearer(outsider), json=new_code(tenant)
    )
    assert response.status_code == 404
    other = await add_tenant(engine)
    wrong_profile = new_code(tenant) | {"agent_profile_id": str(other.ids["profile"])}
    response = await tg.post(
        "/channels/telegram/pairing-codes", headers=mia(tenant), json=wrong_profile
    )
    assert response.status_code == 404
    bad_kind = new_code(tenant, kind="admin")
    response = await tg.post("/channels/telegram/pairing-codes", headers=mia(tenant), json=bad_kind)
    assert response.status_code == 422
    missing = await tg.delete(f"/channels/telegram/identities/{uuid.uuid4()}", headers=mia(tenant))
    assert missing.status_code == 404


async def test_codes_made_at_the_same_time_keep_the_cap(
    tg: httpx.AsyncClient, tenant: Tenant
) -> None:
    responses = await asyncio.gather(
        *(
            tg.post("/channels/telegram/pairing-codes", headers=mia(tenant), json=new_code(tenant))
            for _ in range(8)
        )
    )
    assert sorted(response.status_code for response in responses) == [201] * 5 + [429] * 3


async def test_a_user_has_at_most_five_open_codes(tg: httpx.AsyncClient, tenant: Tenant) -> None:
    for kind in ("user", "group", "user", "user", "group"):
        response = await tg.post(
            "/channels/telegram/pairing-codes", headers=mia(tenant), json=new_code(tenant, kind)
        )
        assert response.status_code == 201
    response = await tg.post(
        "/channels/telegram/pairing-codes", headers=mia(tenant), json=new_code(tenant)
    )
    assert response.status_code == 429


# -- integrations -------------------------------------------------------------------------------


async def test_an_admin_connects_and_disconnects_the_bot_of_the_org(
    tg: httpx.AsyncClient,
    engine: AsyncEngine,
    tenant: Tenant,
    telegram: FakeTelegram,
    vault: Vault,
) -> None:
    admin = bearer(await add_user(engine, tenant, workspace_member=True, role="admin"))
    url = f"/orgs/{tenant.org}/integrations"
    old = tenant.ids["bot"]
    response = await tg.post(f"{url}/telegram", headers=admin, json={"bot_token": f" {TOKEN} "})
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["kind"], body["status"], body["name"]) == ("telegram", "active", "sama_bot")
    assert TOKEN not in response.text
    # A new bot replaces the old bot and its links.
    assert body["id"] != str(old)
    (hook,) = telegram.method_calls("setWebhook")
    assert hook["url"] == f"https://sama.test/telegram/{body['id']}/webhook"
    async with engine.connect() as connection:
        stored = (
            await connection.execute(
                select(Integration.secret_hash).where(Integration.id == uuid.UUID(body["id"]))
            )
        ).scalar_one()
    assert stored is not None
    assert hook["secret_token"] not in stored, "only the digest of the secret is stored"
    token = await vault.get(tenant.org, SecretName.TELEGRAM_BOT_TOKEN)
    assert token is not None
    assert token.get_secret_value() == TOKEN

    listed = (await tg.get(url, headers=mia(tenant))).json()
    assert [(i["id"], i["status"]) for i in listed] == [(body["id"], "active")]
    me = (await tg.get("/me", headers=mia(tenant))).json()["orgs"][0]
    assert me["integrations"] == [{"kind": "telegram", "status": "active", "name": "sama_bot"}]
    group = await tg.post(
        "/channels/telegram/pairing-codes", headers=mia(tenant), json=new_code(tenant, "group")
    )
    assert group.json()["link"] == f"https://t.me/sama_bot?startgroup={group.json()['code']}"

    assert (await tg.delete(f"{url}/telegram", headers=admin)).status_code == 204
    assert telegram.method_calls("deleteWebhook") == [{"drop_pending_updates": True}]
    assert await vault.get(tenant.org, SecretName.TELEGRAM_BOT_TOKEN) is None
    assert (await tg.get(url, headers=mia(tenant))).json() == []
    async with engine.connect() as connection:
        codes = await connection.scalar(
            select(func.count()).select_from(PairingCode).where(PairingCode.org_id == tenant.org)
        )
        actions = (
            await connection.execute(
                select(AuditEntry.action)
                .where(AuditEntry.org_id == tenant.org, AuditEntry.target_type == "integration")
                .order_by(AuditEntry.id)
            )
        ).scalars()
        assert list(actions) == ["integration.connected", "integration.disconnected"]
    assert codes == 0, "the codes of the bot go with it"
    assert (await tg.delete(f"{url}/telegram", headers=admin)).status_code == 404
    off = await tg.post(
        "/channels/telegram/pairing-codes", headers=mia(tenant), json=new_code(tenant)
    )
    assert off.status_code == 409


async def test_only_an_admin_of_the_org_changes_integrations(
    tg: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant, telegram: FakeTelegram
) -> None:
    url = f"/orgs/{tenant.org}/integrations"
    token = {"bot_token": TOKEN}
    assert (await tg.post(f"{url}/telegram", headers=mia(tenant), json=token)).status_code == 403
    assert (await tg.delete(f"{url}/telegram", headers=mia(tenant))).status_code == 403
    stranger = bearer(uuid.uuid4())
    assert (await tg.get(url, headers=stranger)).status_code == 404
    assert (await tg.post(f"{url}/telegram", headers=stranger, json=token)).status_code == 404
    other = await add_tenant(engine)
    assert (await tg.get(f"/orgs/{other.org}/integrations", headers=mia(tenant))).status_code == 404
    assert telegram.calls == []


async def test_a_wrong_token_or_a_failed_webhook_is_shown(
    tg: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant, telegram: FakeTelegram
) -> None:
    admin = bearer(await add_user(engine, tenant, workspace_member=True, role="admin"))
    url = f"/orgs/{tenant.org}/integrations/telegram"
    shape = await tg.post(url, headers=admin, json={"bot_token": "not-a-token"})
    assert (shape.status_code, telegram.calls) == (400, [])
    unknown = await tg.post(url, headers=admin, json={"bot_token": "1:" + "b" * 35})
    assert unknown.status_code == 400
    assert "did not accept" in unknown.json()["detail"]
    telegram.failing.add("setWebhook")
    failed = await tg.post(url, headers=admin, json={"bot_token": TOKEN})
    assert failed.status_code == 201
    assert failed.json()["status"] == "failed"
    assert "Bad Request" in failed.json()["last_error"]
    # A bot that is not active gives no pairing codes.
    code = await tg.post(
        "/channels/telegram/pairing-codes", headers=mia(tenant), json=new_code(tenant)
    )
    assert code.status_code == 409
    telegram.failing.clear()
    again = await tg.post(url, headers=admin, json={"bot_token": TOKEN})
    assert (again.json()["id"], again.json()["status"]) == (failed.json()["id"], "active")


async def test_a_bot_belongs_to_one_org(
    tg: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    admin = bearer(await add_user(engine, tenant, workspace_member=True, role="owner"))
    assert (
        await tg.post(
            f"/orgs/{tenant.org}/integrations/telegram", headers=admin, json={"bot_token": TOKEN}
        )
    ).status_code == 201
    other = await add_tenant(engine)
    other_admin = bearer(await add_user(engine, other, workspace_member=True, role="admin"))
    taken = await tg.post(
        f"/orgs/{other.org}/integrations/telegram", headers=other_admin, json={"bot_token": TOKEN}
    )
    assert taken.status_code == 409
    assert "another org" in taken.json()["detail"]


async def test_integrations_are_off_without_the_service(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    admin = bearer(await add_user(engine, tenant, workspace_member=True, role="admin"))
    response = await client.get(f"/orgs/{tenant.org}/integrations", headers=admin)
    assert response.status_code == 409


async def test_integrations_need_the_vault(engine: AsyncEngine, tenant: Tenant) -> None:
    service = Integrations(engine, None, public_url="https://sama.test")
    assert not service.enabled
    with pytest.raises(IntegrationError, match="SAMA_VAULT_KEY") as error:
        await service.connect_telegram(tenant.org, tenant.ids["mia"], SecretStr(TOKEN))
    assert error.value.status_code == 409


async def test_an_admin_checks_the_telegram_bot(
    tg: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant, telegram: FakeTelegram
) -> None:
    admin = bearer(await add_user(engine, tenant, workspace_member=True, role="admin"))
    base = f"/orgs/{tenant.org}/integrations/telegram"
    assert (await tg.post(base, headers=admin, json={"bot_token": TOKEN})).status_code == 201

    ok = await tg.post(f"{base}/check", headers=admin)

    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert (body["ok"], body["detail"]) == (True, None)
    assert (body["integration"]["kind"], body["integration"]["status"]) == ("telegram", "active")
    assert body["integration"]["last_error"] is None
    assert TOKEN not in ok.text

    telegram.failing.add("getMe")
    failed = await tg.post(f"{base}/check", headers=admin)
    assert failed.status_code == 200
    assert failed.json()["ok"] is False
    assert failed.json()["detail"].startswith("Telegram did not accept the token")
    assert failed.json()["integration"]["last_error"] == failed.json()["detail"]
    assert failed.json()["integration"]["status"] == "active"
    assert TOKEN not in failed.text

    telegram.failing.clear()
    healed = await tg.post(f"{base}/check", headers=admin)
    assert healed.json()["ok"] is True
    assert healed.json()["integration"]["last_error"] is None
    async with engine.connect() as connection:
        entries = (
            await connection.execute(
                select(AuditEntry.details)
                .where(AuditEntry.action == "integration.checked")
                .order_by(AuditEntry.id)
            )
        ).all()
    assert [e.details for e in entries] == [
        {"kind": "telegram", "ok": True},
        {"kind": "telegram", "ok": False},
        {"kind": "telegram", "ok": True},
    ]


async def test_the_check_needs_an_admin_and_an_integration(
    tg: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    base = f"/orgs/{tenant.org}/integrations"
    assert (await tg.post(f"{base}/telegram/check", headers=mia(tenant))).status_code == 403
    admin = bearer(await add_user(engine, tenant, workspace_member=True, role="admin"))
    assert (await tg.post(f"{base}/slack/check", headers=admin)).status_code == 404
    assert (await tg.post(f"{base}/discord/check", headers=admin)).status_code == 422
    other = bearer("33333333-3333-3333-3333-333333333333")
    assert (await tg.post(f"{base}/telegram/check", headers=other)).status_code == 404
