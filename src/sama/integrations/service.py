"""Connect and disconnect the chat apps of an org (docs/architecture.md, Section 11.6).

An org admin connects the Telegram bot of the org with its BotFather token. ``Integrations``:

1. asks Telegram for the bot of the token (``getMe``);
2. stores the token in the vault and the bot in ``integrations``, in one transaction, with a
   new random webhook secret. Only the SHA-256 digest of the secret is stored;
3. registers ``<public_url>/telegram/<integration id>/webhook`` with the secret at Telegram.

An org admin connects a Slack app in two steps:

1. ``setup_slack`` makes a ``pending`` row and returns the request URLs of the row and an app
   manifest. The admin makes the Slack app from the manifest and installs it;
2. ``connect_slack`` takes the bot token and the signing secret of the app. It asks Slack for
   the workspace and the bot of the token (``auth.test``), stores both secrets in the vault and
   the SHA-256 digest of the signing secret in the row, and sets the row ``active``. The row
   keeps its id, because the URLs in the Slack app hold it.

A disconnect deletes the row (with its linked chats and pairing codes) and the vault entries.
For Telegram it deletes the webhook, and for Slack it revokes the bot token. Each change
writes an audit entry without secrets.

A connect or a disconnect holds Postgres advisory locks for the org (and the kind) and for the
bot from the start of its database work to the end of its Telegram calls. Thus two changes of
one org or one bot, also in two processes, cannot interleave their webhook registrations. A
change that does not get a lock at once gets ``409``: it does not wait, so it does not keep a
pooled connection that the change with the lock needs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from urllib.parse import quote

from pydantic import SecretStr
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import AuditEntry, ChannelGroup, ChannelIdentity, Integration, PairingCode
from sama.gateway.slack_client import BOT_TOKEN, SIGNING_SECRET, SlackClient, SlackError
from sama.gateway.telegram_client import TelegramClient, TelegramError
from sama.vault.crypto import VaultError
from sama.vault.store import SecretName, Vault

log: Final = logging.getLogger("sama.integrations")

TELEGRAM_TOKEN: Final = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{30,100}")
MAX_ERROR_CHARS: Final = 500

TelegramClientFactory = Callable[[SecretStr], TelegramClient]
SlackClientFactory = Callable[[SecretStr], SlackClient]

SLACK_SCOPES: Final = (
    "app_mentions:read",
    "chat:write",
    "commands",
    "im:history",
    "im:read",
    "im:write",
    "users:read",
)


class IntegrationError(Exception):
    """A connect or a disconnect that cannot happen. ``status_code`` is the HTTP answer."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class SlackSetup:
    """What an admin needs to make the Slack app of a pending (or active) integration."""

    integration: IntegrationView
    events_url: str
    interactions_url: str
    commands_url: str
    manifest: dict[str, Any]

    @property
    def manifest_link(self) -> str:
        """Opens the Slack page that makes a new app from the manifest."""
        text = json.dumps(self.manifest, separators=(",", ":"))
        return f"https://api.slack.com/apps?new_app=1&manifest_json={quote(text, safe='')}"

    def public(self) -> dict[str, Any]:
        return {
            "integration": self.integration.public(),
            "events_url": self.events_url,
            "interactions_url": self.interactions_url,
            "commands_url": self.commands_url,
            "manifest": self.manifest,
            "manifest_link": self.manifest_link,
        }


def slack_manifest(events_url: str, interactions_url: str, commands_url: str) -> dict[str, Any]:
    """The app manifest of a sama Slack app (https://docs.slack.dev/reference/app-manifest)."""
    return {
        "display_information": {
            "name": "sama",
            "description": "Talk to the sama agents of your org.",
        },
        "features": {
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
            "bot_user": {"display_name": "sama", "always_online": True},
            "slash_commands": [
                {
                    "command": "/sama",
                    "url": commands_url,
                    "description": "Control sama: new, stop, pair, help",
                    "usage_hint": "new | stop | pair CODE | help",
                    "should_escape": False,
                }
            ],
        },
        "oauth_config": {"scopes": {"bot": list(SLACK_SCOPES)}},
        "settings": {
            "event_subscriptions": {
                "request_url": events_url,
                "bot_events": ["app_mention", "message.im"],
            },
            "interactivity": {"is_enabled": True, "request_url": interactions_url},
            "org_deploy_enabled": False,
            "socket_mode_enabled": False,
            "token_rotation_enabled": False,
        },
    }


@dataclass(frozen=True, slots=True)
class IntegrationView:
    """An integration without secrets."""

    id: uuid.UUID
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


class Integrations:
    def __init__(
        self,
        engine: AsyncEngine,
        vault: Vault | None,
        *,
        public_url: str | None,
        telegram_client: TelegramClientFactory = TelegramClient,
        slack_client: SlackClientFactory = SlackClient,
        on_change: Callable[[uuid.UUID], None] | None = None,
    ) -> None:
        """``on_change`` gets the id of each integration that changed or went, so that a
        cache of this process forgets it at once."""
        self._engine = engine
        self._vault = vault
        self._public_url = (public_url or "").rstrip("/") or None
        self._telegram_client = telegram_client
        self._slack_client = slack_client
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
            async with _locked(self._engine, _org_key(org_id, "telegram"), _bot_key(bot_id)):
                integration_id = await self._store_telegram(
                    vault, org_id, user_id, token, bot_id, username, webhook_secret
                )
                self._changed(integration_id)
                url = f"{public_url}/telegram/{integration_id}/webhook"
                error: str | None = None
                try:
                    await client.set_webhook(url, SecretStr(webhook_secret))
                except TelegramError as failure:
                    error = str(failure)[:MAX_ERROR_CHARS]
                    log.warning("a Telegram webhook registration failed org=%s: %s", org_id, error)
                async with self._engine.begin() as connection:
                    row = (
                        await connection.execute(
                            update(Integration)
                            .where(
                                Integration.org_id == org_id,
                                Integration.id == integration_id,
                                # Only the secret that this connect registered.
                                Integration.secret_hash == secret_hash(webhook_secret),
                            )
                            .values(
                                status="failed" if error else "active",
                                last_error=error,
                                updated_at=func.now(),
                            )
                            .returning(*_VIEW)
                        )
                    ).one_or_none()
        finally:
            await client.aclose()
        if row is None:
            raise IntegrationError(
                "the integration changed at the same time. Try again.", status_code=409
            )
        return IntegrationView(**row._asdict())

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

    # -- Slack ------------------------------------------------------------------------------

    async def setup_slack(self, org_id: uuid.UUID, user_id: uuid.UUID) -> SlackSetup:
        """Makes the ``pending`` Slack row of the org, or keeps the row that it has, and
        returns the URLs and the manifest of the Slack app."""
        _, public_url = self._require()
        async with _locked(self._engine, _org_key(org_id, "slack")) as connection:
            row = (
                await connection.execute(
                    select(*_VIEW).where(Integration.org_id == org_id, Integration.kind == "slack")
                )
            ).one_or_none()
            if row is None:
                row = (
                    await connection.execute(
                        insert(Integration)
                        .values(
                            org_id=org_id,
                            kind="slack",
                            status="pending",
                            created_by_user_id=user_id,
                        )
                        .returning(*_VIEW)
                    )
                ).one()
                await _audit(
                    connection, org_id, user_id, "integration.created", row.id, {"kind": "slack"}
                )
        view = IntegrationView(**row._asdict())
        base = f"{public_url}/slack/{view.id}"
        urls = (f"{base}/events", f"{base}/interactions", f"{base}/commands")
        return SlackSetup(view, *urls, manifest=slack_manifest(*urls))

    async def connect_slack(
        self,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        bot_token: SecretStr,
        signing_secret: SecretStr,
    ) -> IntegrationView:
        """Connects the Slack app of the pending (or active) row of the org. Raises
        ``IntegrationError``."""
        vault, _ = self._require()
        token = bot_token.get_secret_value().strip()
        signing = signing_secret.get_secret_value().strip()
        if BOT_TOKEN.fullmatch(token) is None:
            raise IntegrationError(
                "this is not a Slack bot token. Copy the token that starts with xoxb- from"
                " OAuth & Permissions."
            )
        if SIGNING_SECRET.fullmatch(signing) is None:
            raise IntegrationError(
                "this is not a Slack signing secret. Copy it from Basic Information."
            )
        client = self._slack_client(SecretStr(token))
        try:
            try:
                auth = await client.auth_test()
            except SlackError:
                raise IntegrationError(
                    "Slack did not accept the token. Install the app and copy the bot token."
                ) from None
            team_id, team = str(auth["team_id"]), str(auth.get("team") or auth["team_id"])
            settings = {
                "bot_user_id": str(auth["user_id"]),
                "bot_id": str(auth["bot_id"]),
                "app_id": await client.app_id(str(auth["bot_id"])),
            }
        finally:
            await client.aclose()
        async with _locked(self._engine, _org_key(org_id, "slack"), _team_key(team_id)):
            try:
                old = await vault.get(org_id, SecretName.SLACK_BOT_TOKEN, fresh=True)
            except VaultError:
                old = None
            try:
                async with self._engine.begin() as connection:
                    view = await self._store_slack(
                        connection, vault, org_id, user_id, team_id, team, settings, token, signing
                    )
            except IntegrityError:
                raise IntegrationError(
                    "another connect is in progress. Try again.", status_code=409
                ) from None
        self._changed(view.id)
        if old is not None and old.get_secret_value() != token:
            # The new app replaces the old one: the old token must not post again.
            await self._forget_slack(org_id, old)
        return view

    async def _store_slack(
        self,
        connection: AsyncConnection,
        vault: Vault,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        team_id: str,
        team: str,
        settings: dict[str, Any],
        token: str,
        signing: str,
    ) -> IntegrationView:
        elsewhere = await connection.scalar(
            select(Integration.id).where(
                Integration.kind == "slack",
                Integration.external_id == team_id,
                Integration.org_id != org_id,
            )
        )
        if elsewhere is not None:
            raise IntegrationError(
                "this Slack workspace is connected to another org.", status_code=409
            )
        current = (
            await connection.execute(
                select(Integration.id, Integration.external_id)
                .where(Integration.org_id == org_id, Integration.kind == "slack")
                .with_for_update()
            )
        ).one_or_none()
        if current is None:
            raise IntegrationError(
                "start the Slack setup first: the Slack app needs the URLs of the setup.",
                status_code=409,
            )
        if current.external_id is not None and current.external_id != team_id:
            # Another Slack workspace: the links of the old workspace do not work with it.
            for table in (ChannelIdentity, ChannelGroup, PairingCode):
                await connection.execute(
                    delete(table).where(table.org_id == org_id, table.integration_id == current.id)
                )
        row = (
            await connection.execute(
                update(Integration)
                .where(Integration.org_id == org_id, Integration.id == current.id)
                .values(
                    status="active",
                    external_id=team_id,
                    name=team,
                    settings=settings,
                    secret_hash=secret_hash(signing),
                    last_error=None,
                    updated_at=func.now(),
                )
                .returning(*_VIEW)
            )
        ).one()
        for name, value in (
            (SecretName.SLACK_BOT_TOKEN, token),
            (SecretName.SLACK_SIGNING_SECRET, signing),
        ):
            await vault.put(org_id, name, value, actor_user_id=user_id, connection=connection)
        await _audit(
            connection,
            org_id,
            user_id,
            "integration.connected",
            current.id,
            {"kind": "slack", "name": team, "team_id": team_id},
        )
        return IntegrationView(**row._asdict())

    # -- all kinds --------------------------------------------------------------------------

    async def disconnect(self, org_id: uuid.UUID, kind: str, user_id: uuid.UUID) -> bool:
        """Removes the integration of ``kind``. False when the org has none."""
        vault, _ = self._require()
        async with _locked(self._engine, _org_key(org_id, kind)) as lock:
            row = (
                await lock.execute(
                    select(Integration.external_id).where(
                        Integration.org_id == org_id, Integration.kind == kind
                    )
                )
            ).one_or_none()
            if row is None:
                return False
            if row.external_id is not None:
                # A connect of the same bot or workspace in another org cannot start before
                # the webhook is gone or the token is revoked.
                key = _bot_key if kind == "telegram" else _team_key
                await _lock(lock, key(row.external_id))
            return await self._disconnect(vault, org_id, kind, user_id)

    async def _disconnect(
        self, vault: Vault, org_id: uuid.UUID, kind: str, user_id: uuid.UUID
    ) -> bool:
        token = await vault.get(org_id, _SECRETS[kind][0], fresh=True)
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
            for name in _SECRETS[kind]:
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
            await (self._forget_telegram if kind == "telegram" else self._forget_slack)(
                org_id, token
            )
        return True

    async def _forget_telegram(self, org_id: uuid.UUID, token: SecretStr) -> None:
        client = self._telegram_client(token)
        try:
            await client.delete_webhook()
        except TelegramError as error:
            log.warning("a Telegram webhook delete failed org=%s: %s", org_id, error)
        finally:
            await client.aclose()

    async def _forget_slack(self, org_id: uuid.UUID, token: SecretStr) -> None:
        """Revokes the bot token, so that the app cannot post with it again."""
        client = self._slack_client(token)
        try:
            await client.revoke()
        except SlackError as error:
            log.warning("a Slack token revoke failed org=%s: %s", org_id, error)
        finally:
            await client.aclose()

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


def _org_key(org_id: uuid.UUID, kind: str) -> str:
    return f"sama.integration:org:{org_id}:{kind}"


def _bot_key(bot_id: str) -> str:
    return f"sama.integration:telegram:{bot_id}"


def _team_key(team_id: str) -> str:
    return f"sama.integration:slack:{team_id}"


# Longer than the Telegram calls in a lock (timeouts and 429 waits), and an upper limit for a
# lock of a process that hangs.
LOCK_IDLE_TIMEOUT: Final = "5min"


async def _lock(connection: AsyncConnection, key: str) -> None:
    """Takes the advisory lock of ``key`` or raises ``IntegrationError`` (409) at once."""
    taken = await connection.scalar(
        select(func.pg_try_advisory_xact_lock(func.hashtextextended(key, 0)))
    )
    if not taken:
        raise IntegrationError(
            "another change of this integration is in progress. Try again.", status_code=409
        )


@asynccontextmanager
async def _locked(engine: AsyncEngine, *keys: str) -> AsyncIterator[AsyncConnection]:
    """Holds an advisory lock for each key until the block ends. The locks end with their
    transaction, also when the process stops. The transaction is idle during the Telegram
    calls, so it sets its own idle limit."""
    async with engine.begin() as connection:
        await connection.execute(
            text(f"SET LOCAL idle_in_transaction_session_timeout = '{LOCK_IDLE_TIMEOUT}'")
        )
        for key in keys:
            await _lock(connection, key)
        yield connection


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
