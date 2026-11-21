"""Tests of the connect of WhatsApp and email and of the WhatsApp webhook (docs/architecture.md,
Section 11.9). The providers are the real classes with a fake HTTP transport."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AuditEntry, ChannelIdentity, Integration, VaultSecret
from sama.gateway.links import media_signature
from sama.gateway.sendgrid_client import SendGridClient
from sama.gateway.sessions import FollowSettings
from sama.gateway.twilio_client import signature
from sama.gateway.whatsapp_providers import WhatsAppCredentials, build_provider
from sama.integrations.service import IntegrationError, Integrations, secret_hash
from sama.integrations.whatsapp_hub import WhatsAppHub
from sama.orchestrator.queue import RunQueue
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import SecretName, Vault
from tests.gateway.mail_support import key_pair
from tests.gateway.media_support import RunRef, add_media
from tests.media.support import PNG
from tests.orchestrator.conftest import Tenant, add_tenant

PUBLIC = "https://sama.test"
NUMBER = "+15550001111"
SID = "AC" + "1" * 32
AUTH = uuid.uuid4().hex  # random: vault values join the log redactor of the process
APP_SECRET = uuid.uuid4().hex
APP_ID = str(uuid.UUID(int=99))
NOW = 1_800_000_000


class Provider:
    """The APIs of Meta, Twilio, Gupshup, and SendGrid in one fake transport."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.refuse: set[str] = set()
        self.subscribe_fails = False
        self.scopes = ["mail.send"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if host in self.refuse:
            return httpx.Response(401, json={"message": "no", "error": {"message": "no"}})
        if host == "api.sendgrid.com":
            return httpx.Response(200, json={"scopes": self.scopes})
        if host == "graph.facebook.com":
            return httpx.Response(200, json={"display_phone_number": "1 555 000 1111"})
        if host == "api.gupshup.io":
            if request.url.path.endswith("/subscription") and request.method == "POST":
                if self.subscribe_fails:
                    return httpx.Response(400, json={"status": "error", "message": "bad url"})
                return httpx.Response(200, json={"status": "success", "subscription": {"id": "s9"}})
            if request.method == "DELETE":
                return httpx.Response(200, json={"status": "success"})
            if request.url.path.endswith("/msg"):
                return httpx.Response(200, json={"status": "submitted", "messageId": "g1"})
            return httpx.Response(200, json={"status": "success", "app": {"name": "samaapp"}})
        # Twilio
        return httpx.Response(200, json={"sid": "SM1", "status": "active"})

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def factory(self, credentials: WhatsAppCredentials) -> Any:
        return build_provider(credentials, http=self.http())

    def sendgrid(self, key: SecretStr) -> SendGridClient:
        return SendGridClient(key, http=self.http())


@pytest.fixture
def fake() -> Provider:
    return Provider()


@pytest.fixture
def vault(engine: AsyncEngine) -> Vault:
    return Vault(engine, VaultKey(b"w" * KEY_BYTES))


@pytest.fixture
def integrations(engine: AsyncEngine, vault: Vault, fake: Provider) -> Integrations:
    return Integrations(
        engine,
        vault,
        public_url=PUBLIC,
        whatsapp_provider=fake.factory,
        sendgrid_client=fake.sendgrid,
    )


def twilio() -> WhatsAppCredentials:
    return WhatsAppCredentials("twilio", NUMBER, SecretStr(AUTH), account_sid=SID)


def meta() -> WhatsAppCredentials:
    return WhatsAppCredentials(
        "meta",
        NUMBER,
        SecretStr("EAA" + "t" * 40),
        app_secret=SecretStr(APP_SECRET),
        phone_number_id="7777777",
    )


def gupshup() -> WhatsAppCredentials:
    return WhatsAppCredentials(
        "gupshup", NUMBER, SecretStr(uuid.uuid4().hex), app_id=APP_ID, app_name="samaapp"
    )


async def names(engine: AsyncEngine, org_id: uuid.UUID) -> set[str]:
    async with engine.connect() as connection:
        rows = await connection.scalars(
            select(VaultSecret.name).where(VaultSecret.org_id == org_id)
        )
        return set(rows.all())


async def test_twilio_connects_and_gives_the_webhook_url(
    engine: AsyncEngine, tenant: Tenant, integrations: Integrations, fake: Provider
) -> None:
    setup = await integrations.connect_whatsapp(tenant.org, tenant.ids["mia"], twilio())
    view = setup.integration
    assert (view.kind, view.status, view.name) == ("whatsapp", "active", NUMBER)
    assert view.external_id == f"twilio:{SID}:{NUMBER}", "unique only with the proven account"
    token = setup.webhook_url.rsplit("/", 1)[1]
    assert setup.webhook_url == f"{PUBLIC}/whatsapp/{view.id}/webhook/{token}"
    assert setup.verify_token is None
    assert any("When a message comes in" in step for step in setup.steps)
    async with engine.connect() as connection:
        row = (await connection.execute(select(Integration).where(Integration.id == view.id))).one()
        audit = await connection.scalar(
            select(AuditEntry.details).where(AuditEntry.action == "integration.connected")
        )
    assert row.secret_hash == secret_hash(token)
    assert row.settings == {"provider": "twilio", "account_sid": SID}
    assert audit == {"kind": "whatsapp", "provider": "twilio"}
    assert await names(engine, tenant.org) == {"whatsapp_api_token", "whatsapp_link_secret"}
    assert fake.requests[0].url.path == f"/2010-04-01/Accounts/{SID}.json"
    assert AUTH not in json.dumps(setup.public())


async def test_meta_connects_with_a_verify_token(
    engine: AsyncEngine, tenant: Tenant, integrations: Integrations
) -> None:
    setup = await integrations.connect_whatsapp(tenant.org, tenant.ids["mia"], meta())
    assert setup.verify_token == setup.webhook_url.rsplit("/", 1)[1]
    assert setup.public()["verify_token"] == setup.verify_token
    assert "whatsapp_app_secret" in await names(engine, tenant.org)
    # A connect again with Twilio removes the app secret of Meta.
    await integrations.connect_whatsapp(tenant.org, tenant.ids["mia"], twilio())
    assert "whatsapp_app_secret" not in await names(engine, tenant.org)


async def test_gupshup_connects_and_subscribes_with_a_secret_header(
    engine: AsyncEngine, tenant: Tenant, integrations: Integrations, fake: Provider, vault: Vault
) -> None:
    setup = await integrations.connect_whatsapp(tenant.org, tenant.ids["mia"], gupshup())
    assert setup.integration.status == "active"
    subscribe = next(r for r in fake.requests if r.url.path.endswith("/subscription"))
    form = parse_qs(subscribe.content.decode())
    assert form["url"] == [setup.webhook_url]
    header = await vault.get(tenant.org, SecretName.WHATSAPP_APP_SECRET, fresh=True)
    assert header is not None
    assert json.loads(form["meta"][0]) == {"X-Sama-Webhook-Secret": header.get_secret_value()}
    async with engine.connect() as connection:
        settings = await connection.scalar(select(Integration.settings))
    assert settings is not None
    assert settings["subscription_id"] == "s9"

    # A disconnect deletes the subscription and the secrets.
    assert await integrations.disconnect(tenant.org, "whatsapp", tenant.ids["mia"])
    assert fake.requests[-1].method == "DELETE"
    assert fake.requests[-1].url.path.endswith("/subscription/s9")
    assert await names(engine, tenant.org) == set()


async def test_a_failed_gupshup_subscription_marks_the_number_failed(
    tenant: Tenant, integrations: Integrations, fake: Provider
) -> None:
    fake.subscribe_fails = True
    setup = await integrations.connect_whatsapp(tenant.org, tenant.ids["mia"], gupshup())
    assert setup.integration.status == "failed"
    assert "bad url" in str(setup.integration.last_error)


async def test_wrong_whatsapp_credentials_are_refused(
    engine: AsyncEngine, tenant: Tenant, integrations: Integrations, fake: Provider
) -> None:
    with pytest.raises(IntegrationError, match="account SID"):
        await integrations.connect_whatsapp(
            tenant.org,
            tenant.ids["mia"],
            WhatsAppCredentials("twilio", NUMBER, SecretStr(AUTH), account_sid="x"),
        )
    fake.refuse.add("api.twilio.com")
    with pytest.raises(IntegrationError, match="Twilio did not accept"):
        await integrations.connect_whatsapp(tenant.org, tenant.ids["mia"], twilio())
    async with engine.connect() as connection:
        assert await connection.scalar(select(Integration.id)) is None


async def test_a_number_of_another_org_is_refused_only_with_the_same_account(
    engine: AsyncEngine, tenant: Tenant, integrations: Integrations
) -> None:
    await integrations.connect_whatsapp(tenant.org, tenant.ids["mia"], twilio())
    other = await add_tenant(engine)
    with pytest.raises(IntegrationError) as error:
        await integrations.connect_whatsapp(other.org, other.ids["mia"], twilio())
    assert error.value.status_code == 409
    # Another Twilio account cannot block the number: the number alone is not proven.
    third = await add_tenant(engine)
    another = WhatsAppCredentials("twilio", NUMBER, SecretStr(AUTH), account_sid="AC" + "3" * 32)
    setup = await integrations.connect_whatsapp(third.org, third.ids["mia"], another)
    assert setup.integration.status == "active"


async def test_meta_refuses_a_phone_number_id_of_another_number(
    tenant: Tenant, integrations: Integrations
) -> None:
    other = WhatsAppCredentials(
        "meta",
        "+15559998888",
        SecretStr("EAA" + "t" * 40),
        app_secret=SecretStr(APP_SECRET),
        phone_number_id="7777777",
    )
    with pytest.raises(IntegrationError, match="did not accept"):
        await integrations.connect_whatsapp(tenant.org, tenant.ids["mia"], other)


async def test_email_connects_with_a_sendgrid_key_and_a_public_key(
    engine: AsyncEngine, tenant: Tenant, integrations: Integrations, fake: Provider
) -> None:
    _, public = key_pair()
    key = SecretStr("SG." + "a" * 22 + "." + "b" * 43)
    setup = await integrations.connect_email(
        tenant.org,
        tenant.ids["mia"],
        address="Sama@Mail.Sama.Test",
        api_key=key,
        parse_public_key=public,
    )
    assert setup.integration.name == "sama@mail.sama.test"
    assert str(setup.integration.external_id).endswith(":sama@mail.sama.test")
    assert setup.webhook_url == f"{PUBLIC}/email/{setup.integration.id}/inbound"
    assert any("mx.sendgrid.net" in step for step in setup.steps)
    assert await names(engine, tenant.org) == {"sendgrid_api_key", "email_link_secret"}
    for address, api_key, public_key, problem in (
        ("sama+x@mail.sama.test", key, public, "without +"),
        ("sama@mail.sama.test", SecretStr("nope"), public, "SG."),
        ("sama@mail.sama.test", key, "bad", "public key"),
    ):
        with pytest.raises(IntegrationError, match=problem):
            await integrations.connect_email(
                tenant.org,
                tenant.ids["mia"],
                address=address,
                api_key=api_key,
                parse_public_key=public_key,
            )
    fake.scopes = ["user.profile.read"]
    with pytest.raises(IntegrationError, match="Mail Send"):
        await integrations.connect_email(
            tenant.org,
            tenant.ids["mia"],
            address="sama@mail.sama.test",
            api_key=key,
            parse_public_key=public,
        )
    assert await integrations.disconnect(tenant.org, "email", tenant.ids["mia"])
    assert await names(engine, tenant.org) == set()


# -- the WhatsApp webhook -----------------------------------------------------------------------


@pytest.fixture
async def hub(engine: AsyncEngine, vault: Vault, fake: Provider) -> AsyncIterator[WhatsAppHub]:
    async with engine.begin() as connection:
        await connection.execute(delete(ChannelIdentity))
    hub = WhatsAppHub(
        engine,
        vault,
        public_url=PUBLIC,
        whatsapp_provider=fake.factory,
        settings=FollowSettings(poll_s=0.02, paused_poll_s=0.02),
        clock=lambda: NOW,
    )
    yield hub
    await hub.aclose()


@pytest.fixture
async def http(hub: WhatsAppHub) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(hub.router())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as client:
        yield client


async def test_a_signed_twilio_message_reaches_the_gateway(
    tenant: Tenant,
    integrations: Integrations,
    hub: WhatsAppHub,
    http: httpx.AsyncClient,
    fake: Provider,
) -> None:
    setup = await integrations.connect_whatsapp(tenant.org, tenant.ids["mia"], twilio())
    path = setup.webhook_url.removeprefix(PUBLIC)
    form = {
        "From": "whatsapp:+15557654321",
        "To": f"whatsapp:{NUMBER}",
        "MessageSid": "SM42",
        "Body": "hello",
    }
    body = urlencode(form)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Twilio-Signature": signature(AUTH, setup.webhook_url, form.items()),
    }
    answer = await http.post(path, content=body, headers=headers)
    assert answer.status_code == 200
    assert "<Response>" in answer.text
    await hub.drain()
    sent = [
        parse_qs(r.content.decode()) for r in fake.requests if r.url.path.endswith("/Messages.json")
    ]
    assert "is not linked to sama" in sent[-1]["Body"][0]
    assert sent[-1]["To"] == ["whatsapp:+15557654321"]

    bad = {**headers, "X-Twilio-Signature": "AAAA"}
    assert (await http.post(path, content=body, headers=bad)).status_code == 401
    wrong_token = path.rsplit("/", 1)[0] + "/wrong"
    assert (await http.post(wrong_token, content=body, headers=headers)).status_code == 401


async def test_meta_verifies_the_webhook_and_needs_the_hmac(
    tenant: Tenant,
    integrations: Integrations,
    hub: WhatsAppHub,
    http: httpx.AsyncClient,
) -> None:
    setup = await integrations.connect_whatsapp(tenant.org, tenant.ids["mia"], meta())
    path = setup.webhook_url.removeprefix(PUBLIC)
    query = {"hub.mode": "subscribe", "hub.verify_token": setup.verify_token, "hub.challenge": "42"}
    answer = await http.get(path, params=query)
    assert (answer.status_code, answer.text) == (200, "42")
    assert (await http.get(path, params={**query, "hub.verify_token": "no"})).status_code == 401
    body = json.dumps({"object": "whatsapp_business_account", "entry": []}).encode()
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    good = await http.post(path, content=body, headers={"X-Hub-Signature-256": f"sha256={digest}"})
    assert good.status_code == 200
    assert (await http.post(path, content=body)).status_code == 401


async def test_a_media_link_works_only_with_its_signature(
    engine: AsyncEngine,
    tenant: Tenant,
    integrations: Integrations,
    vault: Vault,
    http: httpx.AsyncClient,
) -> None:
    setup = await integrations.connect_whatsapp(tenant.org, tenant.ids["mia"], twilio())
    run_id = await RunQueue(engine).enqueue(
        org_id=tenant.org, session_id=tenant.ids["mine"], text="draw", user_id=tenant.ids["mia"]
    )
    media_id = await add_media(engine, RunRef(tenant.org, tenant.ids["mine"], run_id), "image", PNG)
    secret = await vault.get(tenant.org, SecretName.WHATSAPP_LINK_SECRET, fresh=True)
    assert secret is not None
    expires = NOW + 60
    sig = media_signature(secret.get_secret_value(), setup.integration.id, media_id, expires)
    path = f"/whatsapp/{setup.integration.id}/media/{media_id}"
    answer = await http.get(path, params={"e": expires, "s": sig})
    assert (answer.status_code, answer.content) == (200, PNG)
    assert answer.headers["content-type"] == "image/png"
    assert (await http.get(path, params={"e": expires, "s": sig + "x"})).status_code == 404
    assert (await http.get(path, params={"e": expires + 1, "s": sig})).status_code == 404


async def test_a_gupshup_number_without_its_header_secret_is_not_available(
    engine: AsyncEngine,
    tenant: Tenant,
    integrations: Integrations,
    vault: Vault,
    http: httpx.AsyncClient,
) -> None:
    setup = await integrations.connect_whatsapp(tenant.org, tenant.ids["mia"], gupshup())
    path = setup.webhook_url.removeprefix(PUBLIC)
    header = await vault.get(tenant.org, SecretName.WHATSAPP_APP_SECRET, fresh=True)
    assert header is not None
    body = b'{"type": "message", "app": "samaapp", "payload": {}}'
    good = await http.post(
        path, content=body, headers={"X-Sama-Webhook-Secret": header.get_secret_value()}
    )
    assert good.status_code == 200
    assert (await http.post(path, content=body)).status_code == 401
    await vault.delete(tenant.org, SecretName.WHATSAPP_APP_SECRET)
    async with integrations._engine.begin() as connection:
        await connection.execute(
            update(Integration).values(updated_at=func.now() + text("interval '1 second'"))
        )
    assert (await http.post(path, content=body)).status_code == 503
