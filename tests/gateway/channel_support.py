"""Fakes for the tests of the WhatsApp and email channels (docs/architecture.md, Section 11.9)."""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Integration
from sama.gateway.pairing import create_code
from sama.gateway.sendgrid_client import Mail, SendGridError
from sama.gateway.whatsapp_providers import (
    Button,
    Inbound,
    MediaRef,
    WhatsAppError,
    WhatsAppProvider,
)
from sama.media.generate import StoredMedia
from tests.orchestrator.conftest import Tenant

NUMBER = "+15550001111"
MIA = "+15557654321"


class FakeWhatsApp(WhatsAppProvider):
    """A provider that records what sama sends. ``buttons`` picks the card style."""

    name = "meta"

    def __init__(self, *, buttons: bool = True, max_text: int = 4096) -> None:
        self.buttons = buttons
        self.max_text = max_text
        self.sent: list[tuple[str, str, str]] = []
        """``(kind, to, text)`` of each send. ``kind`` is text, buttons, or media."""
        self.cards: list[tuple[str, list[Button]]] = []
        self.media: list[tuple[str, StoredMedia, str]] = []
        self.files: dict[str, bytes] = {}
        self.failing = False
        self._ids = itertools.count(1)

    def verify(self, url: str, headers: Mapping[str, str], body: bytes) -> bool:
        return True

    def parse(self, body: bytes, content_type: str) -> list[Inbound]:
        return []

    async def check(self) -> None:
        if self.failing:
            raise WhatsAppError("refused")

    async def send_text(self, to: str, text: str) -> str:
        if self.failing:
            raise WhatsAppError("refused")
        self.sent.append(("text", to, text))
        return f"m{next(self._ids)}"

    async def send_buttons(self, to: str, text: str, buttons: Sequence[Button]) -> str:
        self.sent.append(("buttons", to, text))
        self.cards.append((text, list(buttons)))
        return f"m{next(self._ids)}"

    async def send_media(self, to: str, media: StoredMedia, url: str) -> str:
        self.sent.append(("media", to, url))
        self.media.append((to, media, url))
        return f"m{next(self._ids)}"

    async def download(self, media: MediaRef, *, max_bytes: int) -> bytes:
        data = self.files.get(media.ref)
        if data is None:
            raise WhatsAppError("download failed: 404")
        if len(data) > max_bytes:
            raise WhatsAppError("download failed: the file is too large")
        return data

    async def aclose(self) -> None:
        return None

    def texts(self, to: str = MIA) -> list[str]:
        return [text for kind, who, text in self.sent if who == to and kind != "media"]


class FakeSendGrid:
    """A ``SendGridClient`` that records the mails."""

    def __init__(self) -> None:
        self.mails: list[Mail] = []
        self.scopes_answer: list[str] = ["mail.send"]
        self.failing = False

    async def scopes(self) -> list[str]:
        if self.failing:
            raise SendGridError("SendGrid refused the key: 401")
        return self.scopes_answer

    async def send(self, mail: Mail) -> str:
        if self.failing:
            raise SendGridError("SendGrid refused the email: 401")
        self.mails.append(mail)
        return f"sg{len(self.mails)}"

    async def aclose(self) -> None:
        return None

    def texts(self, to: str) -> list[str]:
        return [mail.text for mail in self.mails if mail.to == to]


async def add_channel(
    engine: AsyncEngine,
    tenant: Tenant,
    kind: str,
    external_id: str,
    *,
    settings: dict[str, Any] | None = None,
    secret_hash: str | None = None,
) -> uuid.UUID:
    integration_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(Integration).values(
                id=integration_id,
                org_id=tenant.org,
                kind=kind,
                status="active",
                external_id=external_id,
                name=external_id,
                settings=settings or {},
                secret_hash=secret_hash,
            )
        )
    tenant.ids[kind] = integration_id
    return integration_id


async def code_for(engine: AsyncEngine, tenant: Tenant, channel: str) -> str:
    async with engine.begin() as connection:
        code, _ = await create_code(
            connection,
            org_id=tenant.org,
            user_id=tenant.ids["mia"],
            integration_id=tenant.ids[channel],
            channel=channel,
            kind="user",
            workspace_id=tenant.ids["ws"],
            agent_profile_id=tenant.ids["profile"],
        )
    return code
