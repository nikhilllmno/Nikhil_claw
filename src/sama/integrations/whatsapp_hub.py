"""The webhooks of the WhatsApp integrations (docs/architecture.md, Section 11.9).

Each provider sends the messages of the number of an org to
``POST /whatsapp/<integration id>/webhook/<token>``. The hub:

1. reads the integration row and compares the SHA-256 digest of the token in the URL with
   ``secret_hash``, in constant time, also for an unknown id. A wrong token gets ``401``;
2. builds the ``WhatsAppGateway`` of the number on the first request, with the secrets from the
   vault, and keeps it while ``updated_at`` of the row stays the same;
3. checks the signature of the provider (``WhatsAppProvider.verify``). A wrong signature gets
   ``401``;
4. gives each message to the gateway, which handles it in the background.

``GET`` on the same URL answers the verification request of Meta (``hub.challenge``) when
``hub.verify_token`` is the token. ``GET /whatsapp/<integration id>/media/<media id>`` gives a
file that a run made to the provider, with a signed link that expires (``sama.gateway.links``).
"""

from __future__ import annotations

import hmac
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Final

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import GeneratedMedia, Integration
from sama.gate.service import GateService
from sama.gateway.links import MEDIA_LINK_S, media_signature, media_valid
from sama.gateway.pairing import Lockout
from sama.gateway.sessions import FollowSettings
from sama.gateway.transcribe import OpenAITranscriber, Transcriber
from sama.gateway.whatsapp import WhatsAppGateway
from sama.gateway.whatsapp_providers import (
    PROVIDERS,
    WhatsAppCredentials,
    WhatsAppProvider,
    build_provider,
)
from sama.integrations.hub import GatewayCache, TranscriberFactory
from sama.integrations.request_body import read_body
from sama.integrations.service import WhatsAppFactory, secret_hash
from sama.orchestrator.queue import RunQueue
from sama.review.apply import ReviewApplier
from sama.skills.service import SkillLibrary
from sama.vault.crypto import VaultError
from sama.vault.store import OrgKeys, SecretName, Vault

log: Final = logging.getLogger("sama.integrations.whatsapp_hub")

UNKNOWN: Final = "unknown webhook or wrong signature"
MAX_BODY_BYTES: Final = 1024 * 1024
_EMPTY_TWIML: Final = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


@dataclass(slots=True)
class _Number:
    gateway: WhatsAppGateway
    provider: WhatsAppProvider
    transcriber: Transcriber | None
    link_secret: SecretStr
    updated_at: Any

    async def aclose(self) -> None:
        await self.gateway.aclose()
        await self.provider.aclose()
        if isinstance(self.transcriber, OpenAITranscriber):
            await self.transcriber.aclose()


class WhatsAppHub(GatewayCache[_Number]):
    def __init__(
        self,
        engine: AsyncEngine,
        vault: Vault,
        *,
        public_url: str,
        keys: OrgKeys | None = None,
        settings: FollowSettings | None = None,
        whatsapp_provider: WhatsAppFactory = build_provider,
        transcriber: TranscriberFactory = OpenAITranscriber,
        queue: RunQueue | None = None,
        gate: GateService | None = None,
        skills: SkillLibrary | None = None,
        reviews: ReviewApplier | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__()
        self._engine = engine
        self._vault = vault
        self._public_url = public_url.rstrip("/")
        self._keys = keys
        self._settings = settings or FollowSettings()
        self._whatsapp_provider = whatsapp_provider
        self._transcriber = transcriber
        self._queue = queue or RunQueue(engine)
        self._gate = gate or GateService(engine)
        self._skills = skills
        self._reviews = reviews
        self._clock = clock
        self._lockout = Lockout()
        self._followers: set[object] = set()

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/whatsapp/{integration_id}/webhook/{token}", include_in_schema=False)
        async def verify(
            integration_id: str,
            token: str,
            mode: Annotated[str, Query(alias="hub.mode")] = "",
            verify_token: Annotated[str, Query(alias="hub.verify_token")] = "",
            challenge: Annotated[str, Query(alias="hub.challenge", max_length=200)] = "",
        ) -> Response:
            await self._row(integration_id, token)
            if mode != "subscribe" or not hmac.compare_digest(verify_token, token):
                raise HTTPException(status_code=401, detail=UNKNOWN)
            return Response(content=challenge, media_type="text/plain")

        @router.post("/whatsapp/{integration_id}/webhook/{token}", include_in_schema=False)
        async def webhook(integration_id: str, token: str, request: Request) -> Response:
            body = await read_body(request, MAX_BODY_BYTES)
            row = await self._row(integration_id, token)
            number = await self._cached(row)
            url = f"{self._public_url}/whatsapp/{row.id}/webhook/{token}"
            headers = {name.lower(): value for name, value in request.headers.items()}
            if not number.provider.verify(url, headers, body):
                raise HTTPException(status_code=401, detail=UNKNOWN)
            for message in number.provider.parse(body, headers.get("content-type", "")):
                number.gateway.accept(message)
            if number.provider.name == "twilio":
                return Response(content=_EMPTY_TWIML, media_type="text/xml")
            return Response(content="{}", media_type="application/json")

        @router.get("/whatsapp/{integration_id}/media/{media_id}", include_in_schema=False)
        async def media(
            integration_id: uuid.UUID,
            media_id: uuid.UUID,
            e: Annotated[int, Query()] = 0,
            s: Annotated[str, Query(max_length=100)] = "",
        ) -> Response:
            return await self._media(integration_id, media_id, e, s)

        return router

    async def _row(self, integration_id: str, token: str) -> Any:
        """The ``active`` row of the integration when ``token`` is its webhook token."""
        try:
            key = uuid.UUID(integration_id)
        except ValueError:
            raise HTTPException(status_code=401, detail=UNKNOWN) from None
        async with self._engine.connect() as connection:
            row = (await connection.execute(_select().where(Integration.id == key))).one_or_none()
        stored = "" if row is None else (row.secret_hash or "")
        # Compare even for an unknown id, so that the time does not show which ids exist.
        matches = hmac.compare_digest(secret_hash(token), stored or "0" * 64)
        if row is None or not stored or not matches or row.status != "active":
            raise HTTPException(status_code=401, detail=UNKNOWN)
        return row

    async def _cached(self, row: Any) -> _Number:
        number = self._bots.get(row.id)
        if number is None or number.updated_at != row.updated_at:
            async with self._lock:
                number = self._bots.get(row.id)
                if number is None or number.updated_at != row.updated_at:
                    self.forget(row.id)
                    number = await self._build(row)
                    self._bots[row.id] = number
        return number

    async def _build(self, row: Any) -> _Number:
        settings = row.settings or {}
        provider_name = settings.get("provider")
        try:
            # Not from the cache: another process can have replaced the number a moment ago.
            token = await self._vault.get(row.org_id, SecretName.WHATSAPP_API_TOKEN, fresh=True)
            app_secret = await self._vault.get(
                row.org_id, SecretName.WHATSAPP_APP_SECRET, fresh=True
            )
            link = await self._vault.get(row.org_id, SecretName.WHATSAPP_LINK_SECRET, fresh=True)
            openai = (
                None
                if self._keys is None
                else await self._keys.get(row.org_id, SecretName.OPENAI_API_KEY)
            )
        except VaultError:
            log.exception("a WhatsApp integration secret does not decrypt id=%s", row.id)
            raise HTTPException(status_code=503, detail="the number is not available") from None
        if (
            token is None
            or link is None
            or provider_name not in PROVIDERS
            or (provider_name == "meta" and app_secret is None)
        ):
            log.error("a WhatsApp integration has no secrets id=%s", row.id)
            raise HTTPException(status_code=503, detail="the number is not available")
        provider = self._whatsapp_provider(
            WhatsAppCredentials(
                provider=provider_name,
                number=str(row.external_id),
                api_token=token,
                app_secret=app_secret,
                phone_number_id=settings.get("phone_number_id"),
                account_sid=settings.get("account_sid"),
                app_id=settings.get("app_id"),
                app_name=settings.get("app_name"),
            )
        )
        transcriber = None if openai is None else self._transcriber(openai)
        integration_id = row.id

        def media_link(media: Any) -> str:
            expires = int(self._clock()) + MEDIA_LINK_S
            signature = media_signature(link.get_secret_value(), integration_id, media.id, expires)
            return (
                f"{self._public_url}/whatsapp/{integration_id}/media/{media.id}"
                f"?e={expires}&s={signature}"
            )

        gateway = WhatsAppGateway(
            self._engine,
            provider,
            integration_id=row.id,
            org_id=row.org_id,
            media_link=media_link,
            settings=self._settings,
            queue=self._queue,
            gate=self._gate,
            skills=self._skills,
            reviews=self._reviews,
            transcriber=transcriber,
            lockout=self._lockout,
            followers=self._followers,
        )
        return _Number(gateway, provider, transcriber, link, row.updated_at)

    async def _media(
        self, integration_id: uuid.UUID, media_id: uuid.UUID, expires: int, signature: str
    ) -> Response:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(_select().where(Integration.id == integration_id))
            ).one_or_none()
        if row is None or row.status != "active":
            raise HTTPException(status_code=404, detail="not found")
        number = await self._cached(row)
        if not media_valid(
            number.link_secret.get_secret_value(),
            integration_id,
            media_id,
            expires,
            signature,
            now=self._clock(),
        ):
            raise HTTPException(status_code=404, detail="not found")
        async with self._engine.connect() as connection:
            file = (
                await connection.execute(
                    select(GeneratedMedia.mime_type, GeneratedMedia.data).where(
                        GeneratedMedia.org_id == row.org_id, GeneratedMedia.id == media_id
                    )
                )
            ).one_or_none()
        if file is None:
            raise HTTPException(status_code=404, detail="not found")
        return Response(
            content=bytes(file.data),
            media_type=file.mime_type,
            headers={"Cache-Control": "no-store"},
        )


def _select() -> Any:
    return select(
        Integration.id,
        Integration.org_id,
        Integration.status,
        Integration.external_id,
        Integration.secret_hash,
        Integration.settings,
        Integration.updated_at,
    ).where(Integration.kind == "whatsapp")
