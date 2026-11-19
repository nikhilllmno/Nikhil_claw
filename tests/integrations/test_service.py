"""Tests of connects and disconnects that run at the same time (docs/architecture.md,
Section 11.6). Each test holds one Telegram call open, starts a second change, and checks that
the second change gets 409 and does not change the first one."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db import create_engine
from sama.db.models import Integration
from sama.gateway.telegram_client import TelegramClient
from sama.integrations.service import IntegrationError, Integrations, secret_hash
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import Vault
from tests.integrations.support import TOKEN, FakeTelegram
from tests.orchestrator.conftest import Tenant


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


@asynccontextmanager
async def held[T](
    telegram: HeldTelegram, change: Coroutine[Any, Any, T]
) -> AsyncIterator[asyncio.Task[T]]:
    """Start ``change``, wait until it holds the Telegram call, and always release it.

    If the test fails, the held change must not keep its lock transaction open, because the
    clean-up of the test tables then waits for ever.
    """
    task = asyncio.create_task(change)
    try:
        await asyncio.wait_for(telegram.reached.wait(), 5)
        yield task
    finally:
        telegram.release.set()
        if not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), 10)
            except BaseException:
                task.cancel()


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


async def test_a_second_connect_during_a_connect_gets_409(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    telegram = HeldTelegram("setWebhook")
    change = service(engine, telegram).connect_telegram(
        tenant.org, tenant.ids["mia"], SecretStr(TOKEN)
    )
    async with held(telegram, change) as first:
        with pytest.raises(IntegrationError) as busy:
            await service(engine, telegram).connect_telegram(
                tenant.org, tenant.ids["mia"], SecretStr(TOKEN)
            )
        assert busy.value.status_code == 409
        assert telegram.started.count("setWebhook") == 1
        telegram.release.set()
        assert (await asyncio.wait_for(first, 10)).status == "active"
    again = await service(engine, telegram).connect_telegram(
        tenant.org, tenant.ids["mia"], SecretStr(TOKEN)
    )
    assert again.status == "active"
    last = telegram.method_calls("setWebhook")[-1]["secret_token"]
    row = await stored(engine, tenant)
    assert row is not None
    assert (row.status, row.secret_hash) == ("active", secret_hash(last))


async def test_a_connect_during_a_disconnect_gets_409(engine: AsyncEngine, tenant: Tenant) -> None:
    telegram = HeldTelegram("deleteWebhook")
    await service(engine, telegram).connect_telegram(
        tenant.org, tenant.ids["mia"], SecretStr(TOKEN)
    )
    change = service(engine, telegram).disconnect(tenant.org, "telegram", tenant.ids["mia"])
    async with held(telegram, change) as gone:
        with pytest.raises(IntegrationError) as busy:
            await service(engine, telegram).connect_telegram(
                tenant.org, tenant.ids["mia"], SecretStr(TOKEN)
            )
        assert busy.value.status_code == 409
        telegram.release.set()
        assert await asyncio.wait_for(gone, 10) is True
    assert (
        await service(engine, telegram).connect_telegram(
            tenant.org, tenant.ids["mia"], SecretStr(TOKEN)
        )
    ).status == "active"
    methods = [name for name, _ in telegram.calls if name in {"setWebhook", "deleteWebhook"}]
    assert methods == ["setWebhook", "deleteWebhook", "setWebhook"]


async def test_changes_that_wait_do_not_use_up_a_small_pool(
    queue_database_url: str, tenant: Tenant
) -> None:
    """The change with the locks needs a second connection. The other changes must not keep
    the pool while the first one waits for Telegram."""
    small = create_engine(queue_database_url, pool_size=2, max_overflow=0, pool_timeout=2)
    try:
        telegram = HeldTelegram("setWebhook")
        change = service(small, telegram).connect_telegram(
            tenant.org, tenant.ids["mia"], SecretStr(TOKEN)
        )
        async with held(telegram, change) as first:
            others = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        service(small, telegram).connect_telegram(
                            tenant.org, tenant.ids["mia"], SecretStr(TOKEN)
                        )
                        for _ in range(5)
                    ),
                    return_exceptions=True,
                ),
                10,
            )
            assert all(isinstance(e, IntegrationError) and e.status_code == 409 for e in others)
            telegram.release.set()
            assert (await asyncio.wait_for(first, 10)).status == "active"
    finally:
        await small.dispose()
