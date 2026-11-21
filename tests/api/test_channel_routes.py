"""Tests of the WhatsApp and email connect routes and their pairing codes (Section 11.9)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import JwtVerifier, SamaApi
from sama.db.models import ChannelIdentity
from sama.gateway.sendgrid_client import SendGridClient
from sama.gateway.whatsapp_providers import WhatsAppCredentials, build_provider
from sama.integrations.service import Integrations
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import Vault
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.api.test_app import add_user, mia
from tests.api.test_telegram_routes import new_code
from tests.gateway.mail_support import key_pair
from tests.orchestrator.conftest import Tenant

pytestmark = pytest.mark.usefixtures("tenant")

AUTH = uuid.uuid4().hex  # random: vault values join the log redactor of the process
SID = "AC" + "2" * 32


def answer(request: httpx.Request) -> httpx.Response:
    if request.url.host == "api.sendgrid.com":
        return httpx.Response(200, json={"scopes": ["mail.send"]})
    return httpx.Response(200, json={"sid": SID})


def provider(credentials: WhatsAppCredentials) -> Any:
    return build_provider(
        credentials, http=httpx.AsyncClient(transport=httpx.MockTransport(answer))
    )


def sendgrid(key: SecretStr) -> SendGridClient:
    return SendGridClient(key, http=httpx.AsyncClient(transport=httpx.MockTransport(answer)))


@pytest.fixture
async def http(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    integrations = Integrations(
        engine,
        Vault(engine, VaultKey(b"r" * KEY_BYTES)),
        public_url="https://sama.test",
        whatsapp_provider=provider,
        sendgrid_client=sendgrid,
    )
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


def whatsapp_body() -> dict[str, str]:
    return {"provider": "twilio", "number": "+15550003333", "api_token": AUTH, "account_sid": SID}


async def test_an_admin_connects_whatsapp_and_a_member_pairs(
    http: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    url = f"/orgs/{tenant.org}/integrations/whatsapp"
    refused = await http.post(url, headers=mia(tenant), json=whatsapp_body())
    assert refused.status_code == 403
    admin = bearer(await add_user(engine, tenant, workspace_member=True, role="admin"))
    early = await http.post(
        "/channels/whatsapp/pairing-codes", headers=mia(tenant), json=new_code(tenant)
    )
    assert early.status_code == 409
    assert "WhatsApp number" in early.json()["detail"]
    response = await http.post(url, headers=admin, json=whatsapp_body())
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "active"
    assert body["webhook_url"].startswith(f"https://sama.test/whatsapp/{body['id']}/webhook/")
    assert AUTH not in response.text

    code = (
        await http.post(
            "/channels/whatsapp/pairing-codes", headers=mia(tenant), json=new_code(tenant)
        )
    ).json()
    assert code["link"] == f"https://wa.me/15550003333?text={code['code']}"
    assert code["command"] == code["code"]
    group = await http.post(
        "/channels/whatsapp/pairing-codes",
        headers=mia(tenant),
        json={**new_code(tenant), "kind": "group"},
    )
    assert group.status_code == 400

    gone = await http.delete(url, headers=admin)
    assert gone.status_code == 204


async def test_a_wrong_provider_body_is_refused(
    http: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    admin = bearer(await add_user(engine, tenant, workspace_member=True, role="admin"))
    url = f"/orgs/{tenant.org}/integrations/whatsapp"
    other = await http.post(url, headers=admin, json={**whatsapp_body(), "provider": "other"})
    assert other.status_code == 422
    short = await http.post(url, headers=admin, json={**whatsapp_body(), "account_sid": "AC1"})
    assert short.status_code == 400
    assert "account SID" in short.json()["detail"]


async def test_an_admin_connects_email_and_a_member_pairs(
    http: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    admin = bearer(await add_user(engine, tenant, workspace_member=True, role="admin"))
    _, public = key_pair()
    response = await http.post(
        f"/orgs/{tenant.org}/integrations/email",
        headers=admin,
        json={
            "address": "sama@mail.sama.test",
            "api_key": "SG." + "a" * 22 + "." + "b" * 43,
            "public_key": public,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["webhook_url"] == f"https://sama.test/email/{body['id']}/inbound"
    code = (
        await http.post("/channels/email/pairing-codes", headers=mia(tenant), json=new_code(tenant))
    ).json()
    assert code["link"] == f"mailto:sama@mail.sama.test?subject={code['code']}"
    listed = (await http.get("/channels/email/identities", headers=mia(tenant))).json()
    assert listed == []
