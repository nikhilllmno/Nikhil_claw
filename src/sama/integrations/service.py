"""Connect and disconnect the chat apps of an org (docs/architecture.md, Section 11.6).

An org admin connects the Telegram bot of the org with its BotFather token. ``Integrations``:

1. asks Telegram for the bot of the token (``getMe``);
2. stores the token in the vault and the bot in ``integrations``, in one transaction, with a
   new random webhook secret. Only the SHA-256 digest of the secret is stored;
3. registers ``<public_url>/telegram/<integration id>/webhook`` with the secret at Telegram.

A disconnect deletes the webhook at Telegram, the row (with its linked chats and pairing codes),
and the vault entry. Each change writes an audit entry without secrets.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from pydantic import SecretStr
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import AuditEntry, Integration
from sama.gateway.telegram_client import TelegramClient, TelegramError
from sama.vault.store import SecretName, Vault

log: Final = logging.getLogger("sama.integrations")

TELEGRAM_TOKEN: Final = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{30,100}")
MAX_ERROR_CHARS: Final = 500

TelegramClientFactory = Callable[[SecretStr], TelegramClient]


class IntegrationError(Exception):
    """A connect or a disconnect that cannot happen. ``status_code`` is the HTTP answer."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class IntegrationView:
    """An integration without secrets."""

    id: uuid.UUID
    org_id: uuid.UUID
    kind: str
    status: str
    external_id: str | None
    name: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    def public(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "kind": self.kind,
            "status": self.status,
            "external_id": self.external_id,
            "name": self.name,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


_VIEW: Final = (
    Integration.id,
    Integration.org_id,
    Integration.kind,
    Integration.status,
    Integration.external_id,
    Integration.name,
    Integration.last_error,
    Integration.created_at,
    Integration.updated_at,
)


def secret_hash(secret: str) -> str:
    """The digest that ``integrations.secret_hash`` stores."""
    return hashlib.sha256(secret.encode()).hexdigest()


def telegram_webhook_path(integration_id: uuid.UUID) -> str:
    return f"/telegram/{integration_id}/webhook"


class Integrations:
    def __init__(
        self,
        engine: AsyncEngine,
        vault: Vault | None,
        *,
        public_url: str | None,
        telegram_client: TelegramClientFactory = TelegramClient,
        on_change: Callable[[uuid.UUID], None] | None = None,
    ) -> None:
        """``on_change`` gets the id of each integration that changed or went, so that a
        cache of this process forgets it at once."""
        self._engine = engine
        self._vault = vault
        self._public_url = (public_url or "").rstrip("/") or None
        self._telegram_client = telegram_client
        self._on_change = on_change

    @property
    def enabled(self) -> bool:
        """Integrations need the vault for their secrets and a public address for webhooks."""
        return self._vault is not None and self._public_url is not None

    async def list(self, org_id: uuid.UUID) -> list[IntegrationView]:
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                select(*_VIEW).where(Integration.org_id == org_id).order_by(Integration.kind)
            )
            return [IntegrationView(*row) for row in rows]

    async def get(self, org_id: uuid.UUID, kind: str) -> IntegrationView | None:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(*_VIEW).where(Integration.org_id == org_id, Integration.kind == kind)
                )
            ).one_or_none()
        return None if row is None else IntegrationView(*row)

    # -- Telegram ---------------------------------------------------------------------------

    async def connect_telegram(
        self, org_id: uuid.UUID, user_id: uuid.UUID, token: SecretStr
    ) -> IntegrationView:
        """Connects the bot of ``token``, or replaces the bot of the org. Raises
        ``IntegrationError``. A failed webhook registration gives the status ``failed``."""
        vault, public_url = self._require()
        value = token.get_secret_value().strip()
        if TELEGRAM_TOKEN.fullmatch(value) is None:
            raise IntegrationError("this is not a Telegram bot token. Copy it from @BotFather.")
        token = SecretStr(value)
        client = self._telegram_client(token)
        try:
            try:
                me = await client.get_me()
            except TelegramError:
                raise IntegrationError(
                    "Telegram did not accept the token. Check it in @BotFather."
                ) from None
            bot_id, username = str(me["id"]), str(me.get("username") or "")
            if not username:
                raise IntegrationError("the bot has no username. Set one in @BotFather.")
            webhook_secret = secrets.token_urlsafe(32)
            integration_id = await self._store_telegram(
                vault, org_id, user_id, token, bot_id, username, webhook_secret
            )
            self._changed(integration_id)
            url = f"{public_url}{telegram_webhook_path(integration_id)}"
            error: str | None = None
            try:
                await client.set_webhook(url, SecretStr(webhook_secret))
            except TelegramError as failure:
                error = str(failure)[:MAX_ERROR_CHARS]
                log.warning("a Telegram webhook registration failed org=%s: %s", org_id, error)
        finally:
            await client.aclose()
        async with self._engine.begin() as connection:
            await connection.execute(
                update(Integration)
                .where(Integration.org_id == org_id, Integration.id == integration_id)
                .values(
                    status="failed" if error else "active",
                    last_error=error,
                    updated_at=func.now(),
                )
            )
        view = await self.get(org_id, "telegram")
        if view is None or view.id != integration_id:
            raise IntegrationError(
                "the integration changed at the same time. Try again.", status_code=409
            )
        return view

    async def _store_telegram(
        self,
        vault: Vault,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        token: SecretStr,
        bot_id: str,
        username: str,
        webhook_secret: str,
    ) -> uuid.UUID:
        try:
            async with self._engine.begin() as connection:
                elsewhere = await connection.scalar(
                    select(Integration.id).where(
                        Integration.kind == "telegram",
                        Integration.external_id == bot_id,
                        Integration.org_id != org_id,
                    )
                )
                if elsewhere is not None:
                    raise IntegrationError(
                        "this bot is connected to another org. Make a new bot in @BotFather.",
                        status_code=409,
                    )
                current = (
                    await connection.execute(
                        select(Integration.id, Integration.external_id)
                        .where(Integration.org_id == org_id, Integration.kind == "telegram")
                        .with_for_update()
                    )
                ).one_or_none()
                if current is not None and current.external_id != bot_id:
                    # A new bot: the links of the old bot do not work with it.
                    await connection.execute(
                        delete(Integration).where(
                            Integration.org_id == org_id, Integration.id == current.id
                        )
                    )
                    self._changed(current.id)
                    current = None
                values = {
                    "status": "pending",
                    "external_id": bot_id,
                    "name": username,
                    "secret_hash": secret_hash(webhook_secret),
                    "last_error": None,
                }
                if current is None:
                    integration_id = uuid.uuid4()
                    await connection.execute(
                        insert(Integration).values(
                            id=integration_id,
                            org_id=org_id,
                            kind="telegram",
                            created_by_user_id=user_id,
                            **values,
                        )
                    )
                else:
                    integration_id = current.id
                    await connection.execute(
                        update(Integration)
                        .where(Integration.org_id == org_id, Integration.id == integration_id)
                        .values(**values, updated_at=func.now())
                    )
                await vault.put(
                    org_id,
                    SecretName.TELEGRAM_BOT_TOKEN,
                    token.get_secret_value(),
                    actor_user_id=user_id,
                    connection=connection,
                )
                await _audit(
                    connection,
                    org_id,
                    user_id,
                    "integration.connected",
                    integration_id,
                    {"kind": "telegram", "name": username},
                )
        except IntegrityError:
            # Another connect of the same bot or the same org won the race.
            raise IntegrationError(
                "another connect is in progress. Try again.", status_code=409
            ) from None
        return integration_id

    # -- all kinds --------------------------------------------------------------------------

    async def disconnect(self, org_id: uuid.UUID, kind: str, user_id: uuid.UUID) -> bool:
        """Removes the integration of ``kind``. False when the org has none."""
        vault, _ = self._require()
        names = _SECRETS[kind]
        token = None
        if kind == "telegram":
            token = await vault.get(org_id, SecretName.TELEGRAM_BOT_TOKEN)
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    delete(Integration)
                    .where(Integration.org_id == org_id, Integration.kind == kind)
                    .returning(Integration.id, Integration.name)
                )
            ).one_or_none()
            if row is None:
                return False
            for name in names:
                await vault.delete(org_id, name, actor_user_id=user_id, connection=connection)
            await _audit(
                connection,
                org_id,
                user_id,
                "integration.disconnected",
                row.id,
                {"kind": kind, "name": row.name},
            )
        self._changed(row.id)
        if token is not None:
            client = self._telegram_client(token)
            try:
                await client.delete_webhook()
            except TelegramError as error:
                log.warning("a Telegram webhook delete failed org=%s: %s", org_id, error)
            finally:
                await client.aclose()
        return True

    def _require(self) -> tuple[Vault, str]:
        if self._vault is None:
            raise IntegrationError(
                "integrations need the vault. The operator must set SAMA_VAULT_KEY.",
                status_code=409,
            )
        if self._public_url is None:
            raise IntegrationError(
                "integrations need SAMA_PUBLIC_URL for the webhooks.", status_code=409
            )
        return self._vault, self._public_url

    def _changed(self, integration_id: uuid.UUID) -> None:
        if self._on_change is not None:
            self._on_change(integration_id)


_SECRETS: Final = {
    "telegram": (SecretName.TELEGRAM_BOT_TOKEN,),
    "slack": (SecretName.SLACK_BOT_TOKEN, SecretName.SLACK_SIGNING_SECRET),
}


async def _audit(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    action: str,
    integration_id: uuid.UUID,
    details: dict[str, Any],
) -> None:
    await connection.execute(
        insert(AuditEntry).values(
            org_id=org_id,
            actor_kind="user",
            actor_user_id=user_id,
            action=action,
            target_type="integration",
            target_id=str(integration_id),
            details=details,
        )
    )
