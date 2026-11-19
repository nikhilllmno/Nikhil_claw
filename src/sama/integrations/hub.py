"""The webhooks of the Telegram integrations (docs/architecture.md, Section 11.6).

Telegram sends the updates of each bot to ``POST /telegram/<integration id>/webhook`` with the
webhook secret of that bot in ``X-Telegram-Bot-Api-Secret-Token``. The hub:

1. reads the integration row and compares the SHA-256 digest of the given secret with the
   stored digest, in constant time. A wrong secret or an unknown id gets ``401``;
2. finds the ``TelegramGateway`` of the bot. It builds the gateway on the first update, with
   the token from the vault, and keeps it while the stored digest stays the same. A new
   connect makes a new secret, so every process builds a new gateway after a change;
3. gives the update to the gateway, which handles it in the background.

All gateways share one pairing lockout and one follower limit.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Final

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Integration
from sama.gate.service import GateService
from sama.gateway.pairing import Lockout
from sama.gateway.telegram import TelegramBot, TelegramGateway, TelegramSettings
from sama.gateway.telegram_client import TelegramClient, bot_id
from sama.gateway.transcribe import OpenAITranscriber, Transcriber
from sama.integrations.service import TelegramClientFactory, secret_hash
from sama.orchestrator.queue import RunQueue
from sama.vault.crypto import VaultError
from sama.vault.store import OrgKeys, SecretName, Vault

log: Final = logging.getLogger("sama.integrations.hub")

TranscriberFactory = Callable[[SecretStr], Transcriber]
UNKNOWN: Final = "unknown webhook or wrong secret"


@dataclass(slots=True)
class _Bot:
    gateway: TelegramGateway
    client: TelegramClient
    transcriber: Transcriber | None
    secret_hash: str

    async def aclose(self) -> None:
        await self.gateway.aclose()
        await self.client.aclose()
        if isinstance(self.transcriber, OpenAITranscriber):
            await self.transcriber.aclose()


class TelegramHub:
    def __init__(
        self,
        engine: AsyncEngine,
        vault: Vault,
        *,
        keys: OrgKeys | None = None,
        settings: TelegramSettings | None = None,
        telegram_client: TelegramClientFactory = TelegramClient,
        transcriber: TranscriberFactory = OpenAITranscriber,
        queue: RunQueue | None = None,
        gate: GateService | None = None,
    ) -> None:
        """``keys`` gives the OpenAI key of an org for voice notes: the vault entry of the
        org, then the default key of the process. Without a key voice notes are off."""
        self._engine = engine
        self._vault = vault
        self._keys = keys
        self._settings = settings or TelegramSettings()
        self._telegram_client = telegram_client
        self._transcriber = transcriber
        self._queue = queue or RunQueue(engine)
        self._gate = gate or GateService(engine)
        self._lockout = Lockout()
        self._followers: set[object] = set()
        self._bots: dict[uuid.UUID, _Bot] = {}
        self._lock = asyncio.Lock()
        self._retiring: set[asyncio.Task[None]] = set()

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.post("/telegram/{integration_id}/webhook", include_in_schema=False)
        async def webhook(
            integration_id: str,
            request: Request,
            x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
        ) -> dict[str, str]:
            gateway = await self.gateway(integration_id, x_telegram_bot_api_secret_token or "")
            try:
                update = await request.json()
            except ValueError:
                raise HTTPException(status_code=400, detail="the body is not JSON") from None
            if not isinstance(update, dict):
                raise HTTPException(status_code=400, detail="the body is not an update")
            gateway.accept(update)
            return {}

        return router

    async def gateway(self, integration_id: str, given_secret: str) -> TelegramGateway:
        """The gateway of the integration when ``given_secret`` is its webhook secret.
        Raises ``HTTPException``."""
        try:
            key = uuid.UUID(integration_id)
        except ValueError:
            raise HTTPException(status_code=401, detail=UNKNOWN) from None
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(
                        Integration.id,
                        Integration.org_id,
                        Integration.status,
                        Integration.name,
                        Integration.external_id,
                        Integration.secret_hash,
                    ).where(Integration.id == key, Integration.kind == "telegram")
                )
            ).one_or_none()
        stored = "" if row is None else (row.secret_hash or "")
        # Compare even for an unknown id, so that the time does not show which ids exist.
        matches = hmac.compare_digest(secret_hash(given_secret), stored or "0" * 64)
        if row is None or not stored or not matches or row.status == "failed":
            raise HTTPException(status_code=401, detail=UNKNOWN)
        bot = self._bots.get(key)
        if bot is not None and bot.secret_hash == stored:
            return bot.gateway
        async with self._lock:
            bot = self._bots.get(key)
            if bot is None or bot.secret_hash != stored:
                self._retire(key)
                bot = await self._build(row, stored)
                self._bots[key] = bot
        return bot.gateway

    async def _build(self, row: Any, stored: str) -> _Bot:
        try:
            token = await self._vault.get(row.org_id, SecretName.TELEGRAM_BOT_TOKEN)
            openai = (
                None
                if self._keys is None
                else await self._keys.get(row.org_id, SecretName.OPENAI_API_KEY)
            )
        except VaultError:
            log.exception("a Telegram integration secret does not decrypt id=%s", row.id)
            raise HTTPException(status_code=503, detail="the bot is not available") from None
        if token is None:
            log.error("a Telegram integration has no token id=%s", row.id)
            raise HTTPException(status_code=503, detail="the bot is not available")
        client = self._telegram_client(token)
        transcriber = None if openai is None else self._transcriber(openai)
        gateway = TelegramGateway(
            self._engine,
            client,
            bot=TelegramBot(
                integration_id=row.id,
                org_id=row.org_id,
                username=str(row.name or ""),
                user_id=bot_id(token),
            ),
            settings=self._settings,
            queue=self._queue,
            gate=self._gate,
            transcriber=transcriber,
            lockout=self._lockout,
            followers=self._followers,
        )
        return _Bot(gateway, client, transcriber, stored)

    def forget(self, integration_id: uuid.UUID) -> None:
        """Drops the gateway of an integration that changed or went. Its open work ends in
        the background."""
        self._retire(integration_id)

    def _retire(self, integration_id: uuid.UUID) -> None:
        bot = self._bots.pop(integration_id, None)
        if bot is None:
            return
        task = asyncio.create_task(self._close_later(bot))
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    async def _close_later(self, bot: _Bot) -> None:
        """Lets the runs that the old gateway follows end, then closes it."""
        try:
            await bot.gateway.drain()
        finally:
            await bot.aclose()

    async def drain(self) -> None:
        """Waits for the background work of all gateways (for tests)."""
        for bot in list(self._bots.values()):
            await bot.gateway.drain()
        while self._retiring:
            await asyncio.gather(*list(self._retiring), return_exceptions=True)

    async def aclose(self) -> None:
        for task in list(self._retiring):
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*list(self._retiring), return_exceptions=True)
        bots, self._bots = list(self._bots.values()), {}
        for bot in bots:
            await bot.aclose()
