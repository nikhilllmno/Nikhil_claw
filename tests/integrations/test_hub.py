"""Tests of the Telegram webhooks of the integrations (docs/architecture.md, Section 11.6)."""

from __future__ import annotations

import itertools
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Integration
from sama.gateway.telegram import PAIR_PROMPT
from sama.integrations.hub import TelegramHub
from sama.integrations.service import secret_hash
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import SecretName, Vault
from tests.integrations.support import TOKEN, FakeTelegram, add_bot
from tests.orchestrator.conftest import Tenant

SECRET = "webhook-secret-0123456789"
HEADER = "X-Telegram-Bot-Api-Secret-Token"
_ids = itertools.count(1)


def start(chat: int = 1001) -> dict[str, Any]:
    return {
        "update_id": next(_ids),
        "message": {
            "message_id": next(_ids),
            "chat": {"id": chat, "type": "private"},
            "from": {"id": chat, "is_bot": False, "first_name": "Mia"},
            "text": "/start",
        },
    }


@pytest.fixture
def telegram() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def vault(engine: AsyncEngine) -> Vault:
    return Vault(engine, VaultKey(b"h" * KEY_BYTES))


@pytest.fixture
async def hub(
    engine: AsyncEngine, tenant: Tenant, telegram: FakeTelegram, vault: Vault
) -> AsyncIterator[TelegramHub]:
    await vault.put(tenant.org, SecretName.TELEGRAM_BOT_TOKEN, TOKEN)
    await add_bot(engine, tenant, secret_hash=secret_hash(SECRET))
    hub = TelegramHub(engine, vault, telegram_client=telegram.client)
    yield hub
    await hub.aclose()


@pytest.fixture
async def http(hub: TelegramHub) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(hub.router())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw.test") as client:
        yield client


async def test_the_webhook_checks_the_secret_and_drops_repeats(
    http: httpx.AsyncClient, hub: TelegramHub, tenant: Tenant, telegram: FakeTelegram
) -> None:
    url = f"/telegram/{tenant.ids['bot']}/webhook"
    update_ = start()
    assert (await http.post(url, json=update_, headers={HEADER: "x"})).status_code == 401
    assert (await http.post(url, json=update_)).status_code == 401
    assert (await http.post(url, content=b"{", headers={HEADER: SECRET})).status_code == 400
    assert (await http.post(url, json=[1], headers={HEADER: SECRET})).status_code == 400
    for _ in range(2):
        assert (await http.post(url, json=update_, headers={HEADER: SECRET})).status_code == 200
    await hub.drain()
    assert telegram.texts(1001) == [PAIR_PROMPT], "a repeated update runs once"
    assert {token for token, _ in telegram.tokens} == {TOKEN}, "the bot of the org answers"


@pytest.mark.parametrize("path", ["not-a-uuid", str(uuid.uuid4())])
async def test_an_unknown_webhook_is_refused(
    http: httpx.AsyncClient, telegram: FakeTelegram, path: str
) -> None:
    response = await http.post(f"/telegram/{path}/webhook", json=start(), headers={HEADER: SECRET})
    assert response.status_code == 401
    assert telegram.calls == []


async def test_a_failed_integration_takes_no_updates(
    http: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Integration).where(Integration.id == tenant.ids["bot"]).values(status="failed")
        )
    url = f"/telegram/{tenant.ids['bot']}/webhook"
    assert (await http.post(url, json=start(), headers={HEADER: SECRET})).status_code == 401


async def test_a_new_secret_builds_a_new_gateway(
    hub: TelegramHub, engine: AsyncEngine, tenant: Tenant
) -> None:
    key = str(tenant.ids["bot"])
    first = await hub.gateway(key, SECRET)
    assert await hub.gateway(key, SECRET) is first, "the gateway is kept"
    async with engine.begin() as connection:
        await connection.execute(
            update(Integration)
            .where(Integration.id == tenant.ids["bot"])
            .values(secret_hash=secret_hash("new-secret-0123456789"))
        )
    with pytest.raises(HTTPException) as refused:
        await hub.gateway(key, SECRET)
    assert refused.value.status_code == 401
    second = await hub.gateway(key, "new-secret-0123456789")
    assert second is not first
    assert second.bot.integration_id == tenant.ids["bot"]
    assert second.bot.username == "sama_bot"
    hub.forget(tenant.ids["bot"])
    assert await hub.gateway(key, "new-secret-0123456789") is not second
    await hub.drain()


async def test_a_bot_without_a_token_is_not_available(
    vault: Vault, tenant: Tenant, http: httpx.AsyncClient
) -> None:
    await vault.delete(tenant.org, SecretName.TELEGRAM_BOT_TOKEN)
    url = f"/telegram/{tenant.ids['bot']}/webhook"
    assert (await http.post(url, json=start(), headers={HEADER: SECRET})).status_code == 503
