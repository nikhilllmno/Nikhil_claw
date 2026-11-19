"""Tests of connects and disconnects that run at the same time (docs/architecture.md,
Section 11.6). Each test holds one Telegram call open, starts a second change, and checks that
the second change waits for the first one."""

from __future__ import annotations

import asyncio

import httpx
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Integration
from sama.gateway.telegram_client import TelegramClient
from sama.integrations.service import Integrations, secret_hash
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import Vault
from tests.integrations.support import TOKEN, FakeTelegram
from tests.orchestrator.conftest import Tenant

WAIT_S = 0.3


class HeldTelegram(FakeTelegram):
    """A Bot API where the first call of ``held`` waits until ``release`` is set."""

    def __init__(self, held: str) -> None:
        super().__init__()
        self.held = held
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.started: list[str] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.started.append(request.url.path.rsplit("/", 1)[-1])
        if request.url.path.endswith(f"/{self.held}") and not self.reached.is_set():
            self.reached.set()
            await self.release.wait()
        return self(request)

    def client(self, token: SecretStr) -> TelegramClient:
        return TelegramClient(
            token,
            base_url="https://tg.test",
            http=httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
        )


def service(engine: AsyncEngine, telegram: FakeTelegram) -> Integrations:
    """One service for each call, as in two processes: each has its own vault cache."""
    vault = Vault(engine, VaultKey(b"j" * KEY_BYTES))
    return Integrations(
        engine, vault, public_url="https://sama.test", telegram_client=telegram.client
    )


async def stored(engine: AsyncEngine, tenant: Tenant) -> Integration | None:
    async with engine.connect() as connection:
        return (
            await connection.execute(
                select(Integration.status, Integration.secret_hash).where(
                    Integration.org_id == tenant.org
                )
            )
        ).one_or_none()  # type: ignore[return-value]


async def test_two_connects_keep_the_secret_that_telegram_has(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    telegram = HeldTelegram("setWebhook")
    first = asyncio.create_task(
        service(engine, telegram).connect_telegram(tenant.org, tenant.ids["mia"], SecretStr(TOKEN))
    )
    await asyncio.wait_for(telegram.reached.wait(), 5)
    second = asyncio.create_task(
        service(engine, telegram).connect_telegram(tenant.org, tenant.ids["mia"], SecretStr(TOKEN))
    )
    await asyncio.sleep(WAIT_S)
    assert telegram.started.count("setWebhook") == 1, "the second connect waits"
    telegram.release.set()
    views = await asyncio.gather(first, second)
    assert [view.status for view in views] == ["active", "active"]
    last = telegram.method_calls("setWebhook")[-1]["secret_token"]
    row = await stored(engine, tenant)
    assert row is not None
    assert (row.status, row.secret_hash) == ("active", secret_hash(last))


async def test_a_disconnect_does_not_delete_the_webhook_of_a_later_connect(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    telegram = HeldTelegram("deleteWebhook")
    await service(engine, telegram).connect_telegram(
        tenant.org, tenant.ids["mia"], SecretStr(TOKEN)
    )
    gone = asyncio.create_task(
        service(engine, telegram).disconnect(tenant.org, "telegram", tenant.ids["mia"])
    )
    await asyncio.wait_for(telegram.reached.wait(), 5)
    again = asyncio.create_task(
        service(engine, telegram).connect_telegram(tenant.org, tenant.ids["mia"], SecretStr(TOKEN))
    )
    await asyncio.sleep(WAIT_S)
    assert telegram.started.count("setWebhook") == 1, "the connect waits for the disconnect"
    telegram.release.set()
    assert await gone is True
    view = await again
    assert view.status == "active"
    methods = [name for name, _ in telegram.calls if name in {"setWebhook", "deleteWebhook"}]
    assert methods == ["setWebhook", "deleteWebhook", "setWebhook"]
