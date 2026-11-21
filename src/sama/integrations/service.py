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
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Final
from urllib.parse import quote

from pydantic import SecretStr
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import AuditEntry, ChannelGroup, ChannelIdentity, Integration, PairingCode
from sama.gateway.mail_inbound import BadKey, public_key
from sama.gateway.sendgrid_client import API_KEY as SENDGRID_KEY
from sama.gateway.sendgrid_client import SEND_SCOPE, SendGridClient, SendGridError
from sama.gateway.slack_client import BOT_TOKEN, SIGNING_SECRET, SlackClient, SlackError
from sama.gateway.telegram_client import TelegramClient, TelegramError
from sama.gateway.whatsapp_providers import (
    PROVIDER_TITLES,
    GupshupProvider,
    WhatsAppCredentials,
    WhatsAppError,
    WhatsAppProvider,
    build_provider,
)
from sama.vault.crypto import VaultError
from sama.vault.redact import default_redactor
from sama.vault.store import SecretName, Vault

log: Final = logging.getLogger("sama.integrations")

TELEGRAM_TOKEN: Final = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{30,100}")
MAX_ERROR_CHARS: Final = 500

TelegramClientFactory = Callable[[SecretStr], TelegramClient]
SlackClientFactory = Callable[[SecretStr], SlackClient]
WhatsAppFactory = Callable[[WhatsAppCredentials], WhatsAppProvider]
SendGridFactory = Callable[[SecretStr], SendGridClient]
EMAIL_ADDRESS: Final = re.compile(r"[a-z0-9._-]{1,64}@[a-z0-9-]+(\.[a-z0-9-]+)+")
"""An inbound address: no ``+`` in the local part, because sama adds ``+t<token>``."""

SLACK_SCOPES: Final = (
    "app_mentions:read",
    "chat:write",
    "commands",
    "files:read",
    "files:write",
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
        whatsapp_provider: WhatsAppFactory = build_provider,
        sendgrid_client: SendGridFactory = SendGridClient,
        on_change: Callable[[uuid.UUID], None] | None = None,
    ) -> None:
        """``on_change`` gets the id of each integration that changed or went, so that a
        cache of this process forgets it at once."""
        self._engine = engine
        self._vault = vault
        self._public_url = (public_url or "").rstrip("/") or None
        self._telegram_client = telegram_client
        self._slack_client = slack_client
        self._whatsapp_provider = whatsapp_provider
        self._sendgrid_client = sendgrid_client
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

    # -- WhatsApp and email (Section 11.9) --------------------------------------------------

    async def connect_whatsapp(
        self, org_id: uuid.UUID, user_id: uuid.UUID, credentials: WhatsAppCredentials
    ) -> ChannelSetup:
        """Connects the WhatsApp number of the org through its provider, or replaces it.
        Raises ``IntegrationError``. For Gupshup, sama also adds the webhook subscription; a
        failed subscription gives the status ``failed``."""
        vault, public_url = self._require()
        problem = credentials.problem()
        if problem is not None:
            raise IntegrationError(problem)
        title = PROVIDER_TITLES[credentials.provider]
        if credentials.provider == "gupshup":
            # The secret header of the subscription: Gupshup does not sign its callbacks.
            credentials = replace(credentials, app_secret=SecretStr(secrets.token_urlsafe(32)))
        provider = self._whatsapp_provider(credentials)
        try:
            try:
                await provider.check()
            except WhatsAppError as error:
                log.warning("a WhatsApp check failed org=%s: %s", org_id, error)
                raise IntegrationError(
                    f"{title} did not accept the credentials. Check them and try again."
                ) from None
            token = secrets.token_urlsafe(24)
            settings: dict[str, Any] = {"provider": credentials.provider}
            for name in ("phone_number_id", "account_sid", "app_id", "app_name"):
                value = getattr(credentials, name)
                if value:
                    settings[name] = value
            async with _locked(
                self._engine,
                _org_key(org_id, "whatsapp"),
                _external_key("whatsapp", whatsapp_external_id(credentials)),
            ):
                old = await self._old_gupshup(vault, org_id)
                async with self._engine.begin() as connection:
                    view = await self._store_channel(
                        connection,
                        org_id,
                        user_id,
                        kind="whatsapp",
                        external_id=whatsapp_external_id(credentials),
                        name=credentials.number,
                        settings=settings,
                        digest=secret_hash(token),
                        details={"kind": "whatsapp", "provider": credentials.provider},
                    )
                    values = {
                        SecretName.WHATSAPP_API_TOKEN: credentials.api_token.get_secret_value(),
                        SecretName.WHATSAPP_LINK_SECRET: secrets.token_urlsafe(32),
                    }
                    if credentials.app_secret is not None:
                        values[SecretName.WHATSAPP_APP_SECRET] = (
                            credentials.app_secret.get_secret_value()
                        )
                    else:
                        await vault.delete(
                            org_id,
                            SecretName.WHATSAPP_APP_SECRET,
                            actor_user_id=user_id,
                            connection=connection,
                        )
                    for name, value in values.items():
                        await vault.put(
                            org_id, name, value, actor_user_id=user_id, connection=connection
                        )
                self._changed(view.id)
                url = f"{public_url}/whatsapp/{view.id}/webhook/{token}"
                if isinstance(provider, GupshupProvider):
                    view = await self._subscribe(org_id, view, provider, url, old)
        finally:
            await provider.aclose()
        return ChannelSetup(
            view,
            webhook_url=url,
            verify_token=token if credentials.provider == "meta" else None,
            steps=whatsapp_steps(credentials.provider, url, token),
        )

    async def _old_gupshup(self, vault: Vault, org_id: uuid.UUID) -> tuple[Any, SecretStr] | None:
        """The settings and the key of the Gupshup number that the org has now, so that a
        new connect can remove the old subscription."""
        async with self._engine.connect() as connection:
            settings = await connection.scalar(
                select(Integration.settings).where(
                    Integration.org_id == org_id, Integration.kind == "whatsapp"
                )
            )
        if not settings or settings.get("provider") != "gupshup":
            return None
        try:
            key = await vault.get(org_id, SecretName.WHATSAPP_API_TOKEN, fresh=True)
        except VaultError:
            return None
        return None if key is None else (settings, key)

    async def _subscribe(
        self,
        org_id: uuid.UUID,
        view: IntegrationView,
        provider: GupshupProvider,
        url: str,
        old: tuple[Any, SecretStr] | None,
    ) -> IntegrationView:
        if old is not None:
            await self._unsubscribe(org_id, old[0], old[1])
        error: str | None = None
        subscription = ""
        try:
            assert provider.header_secret is not None  # noqa: S101 (connect_whatsapp sets it)
            subscription = await provider.subscribe(url, provider.header_secret)
        except WhatsAppError as failure:
            error = str(failure)[:MAX_ERROR_CHARS]
            log.warning("a Gupshup subscription failed org=%s: %s", org_id, error)
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    update(Integration)
                    .where(Integration.org_id == org_id, Integration.id == view.id)
                    .values(
                        status="failed" if error else "active",
                        last_error=error,
                        settings=Integration.settings.op("||")(
                            func.jsonb_build_object("subscription_id", subscription)
                        ),
                        updated_at=func.now(),
                    )
                    .returning(*_VIEW)
                )
            ).one()
        self._changed(view.id)
        return IntegrationView(**row._asdict())

    async def connect_email(
        self,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        address: str,
        api_key: SecretStr,
        parse_public_key: str,
        sender_name: str = "sama",
    ) -> ChannelSetup:
        """Connects the inbound address of the org with SendGrid, or replaces it. Raises
        ``IntegrationError``."""
        vault, public_url = self._require()
        address = address.strip().lower()
        key = api_key.get_secret_value().strip()
        if EMAIL_ADDRESS.fullmatch(address) is None:
            raise IntegrationError(
                "the address must be a plain address without +, for example sama@mail.example.com."
            )
        if SENDGRID_KEY.fullmatch(key) is None:
            raise IntegrationError("this is not a SendGrid API key. It starts with SG.")
        try:
            public_key(parse_public_key)
        except BadKey as error:
            raise IntegrationError(
                f"{error}. Copy the public key of the security policy of the parse webhook."
            ) from None
        client = self._sendgrid_client(SecretStr(key))
        try:
            scopes = await client.scopes()
        except SendGridError as error:
            log.warning("a SendGrid check failed org=%s: %s", org_id, error)
            raise IntegrationError("SendGrid did not accept the API key.") from None
        finally:
            await client.aclose()
        if SEND_SCOPE not in scopes:
            raise IntegrationError("the SendGrid API key needs the Mail Send permission.")
        settings = {"public_key": parse_public_key.strip(), "sender_name": sender_name}
        # The address is not proven, so it is unique only with the SendGrid key (Section 11.9).
        email_id = f"sendgrid:{secret_hash(key)[:16]}:{address}"
        async with (
            _locked(self._engine, _org_key(org_id, "email"), _external_key("email", email_id)),
            self._engine.begin() as connection,
        ):
            view = await self._store_channel(
                connection,
                org_id,
                user_id,
                kind="email",
                external_id=email_id,
                name=address,
                settings=settings,
                digest=secret_hash(key),
                details={"kind": "email", "address": address},
            )
            for name, value in (
                (SecretName.SENDGRID_API_KEY, key),
                (SecretName.EMAIL_LINK_SECRET, secrets.token_urlsafe(32)),
            ):
                await vault.put(org_id, name, value, actor_user_id=user_id, connection=connection)
        self._changed(view.id)
        url = f"{public_url}/email/{view.id}/inbound"
        return ChannelSetup(view, webhook_url=url, steps=email_steps(address, url))

    async def _store_channel(
        self,
        connection: AsyncConnection,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        kind: str,
        external_id: str,
        name: str,
        settings: dict[str, Any],
        digest: str,
        details: dict[str, Any],
    ) -> IntegrationView:
        """Writes the ``active`` row of a WhatsApp number or an email address. A new number
        or address deletes the old row with its links."""
        elsewhere = await connection.scalar(
            select(Integration.id).where(
                Integration.kind == kind,
                Integration.external_id == external_id,
                Integration.org_id != org_id,
            )
        )
        if elsewhere is not None:
            raise IntegrationError(
                f"this {_NOUNS[kind]} is connected to another org.", status_code=409
            )
        current = (
            await connection.execute(
                select(Integration.id, Integration.external_id)
                .where(Integration.org_id == org_id, Integration.kind == kind)
                .with_for_update()
            )
        ).one_or_none()
        if current is not None and current.external_id != external_id:
            # Another number or address: the links of the old one do not work with it.
            await connection.execute(
                delete(Integration).where(
                    Integration.org_id == org_id, Integration.id == current.id
                )
            )
            self._changed(current.id)
            current = None
        values = {
            "status": "active",
            "external_id": external_id,
            "name": name,
            "settings": settings,
            "secret_hash": digest,
            "last_error": None,
        }
        try:
            if current is None:
                row = (
                    await connection.execute(
                        insert(Integration)
                        .values(org_id=org_id, kind=kind, created_by_user_id=user_id, **values)
                        .returning(*_VIEW)
                    )
                ).one()
            else:
                row = (
                    await connection.execute(
                        update(Integration)
                        .where(Integration.org_id == org_id, Integration.id == current.id)
                        .values(**values, updated_at=func.now())
                        .returning(*_VIEW)
                    )
                ).one()
        except IntegrityError:
            raise IntegrationError(
                "another connect is in progress. Try again.", status_code=409
            ) from None
        await _audit(connection, org_id, user_id, "integration.connected", row.id, details)
        return IntegrationView(**row._asdict())

    # -- all kinds --------------------------------------------------------------------------

    async def check(
        self, org_id: uuid.UUID, kind: str, user_id: uuid.UUID
    ) -> tuple[bool, str | None, IntegrationView]:
        """Asks the chat app of ``kind`` if its stored token still works: ``getMe`` for Telegram,
        ``auth.test`` for Slack. Returns ``(ok, detail, view)``. ``detail`` is a short text for
        a failure and ``None`` for a success. A failure sets ``last_error`` of the row. A success
        clears ``last_error`` of an ``active`` row. The row keeps its status, because the check
        does not register a webhook again. Writes the audit entry ``integration.checked``.
        Raises ``IntegrationError`` with ``404`` when the org has no integration of this kind.
        """
        vault, _ = self._require()
        row = await self._read(org_id, kind)
        if row is None:
            raise IntegrationError("the org has no such integration", status_code=404)
        detail = await self._probe(vault, org_id, kind)
        ok = detail is None
        values: dict[str, Any] = {"last_error": detail}
        if ok and row.status != "active":
            values = {}
        async with self._engine.begin() as connection:
            if values:
                await connection.execute(
                    update(Integration)
                    .where(Integration.org_id == org_id, Integration.id == row.id)
                    .values(**values, updated_at=func.now())
                )
            await _audit(
                connection, org_id, user_id, "integration.checked", row.id, {"kind": kind, "ok": ok}
            )
        fresh = await self._read(org_id, kind)
        if fresh is None:
            raise IntegrationError("the org has no such integration", status_code=404)
        return ok, detail, fresh

    async def _read(self, org_id: uuid.UUID, kind: str) -> IntegrationView | None:
        async with self._engine.connect() as connection:
            found = (
                await connection.execute(
                    select(*_VIEW).where(Integration.org_id == org_id, Integration.kind == kind)
                )
            ).one_or_none()
        return None if found is None else IntegrationView(*found)

    async def _probe(self, vault: Vault, org_id: uuid.UUID, kind: str) -> str | None:
        """The answer of the chat app to its stored token: ``None`` when the token works, else a
        short text without a secret."""
        try:
            token = await vault.get(org_id, _SECRETS[kind][0], fresh=True)
        except VaultError:
            return "the stored token cannot be read"
        if token is None:
            return "the app has no bot token yet"
        if kind == "telegram":
            telegram = self._telegram_client(token)
            try:
                await telegram.get_me()
            except TelegramError as error:
                return _short_error("Telegram did not accept the token", error)
            finally:
                await telegram.aclose()
            return None
        slack = self._slack_client(token)
        try:
            await slack.auth_test()
        except SlackError as error:
            return _short_error("Slack did not accept the token", error)
        finally:
            await slack.aclose()
        return None

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
                await _lock(lock, _external_key(kind, row.external_id))
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
                    .returning(Integration.id, Integration.name, Integration.settings)
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
        if token is None:
            return True
        if kind == "telegram":
            await self._forget_telegram(org_id, token)
        elif kind == "slack":
            await self._forget_slack(org_id, token)
        elif kind == "whatsapp" and (row.settings or {}).get("provider") == "gupshup":
            await self._unsubscribe(org_id, row.settings, token)
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

    async def _unsubscribe(self, org_id: uuid.UUID, settings: Any, api_key: SecretStr) -> None:
        """Removes the Gupshup subscription of sama (best effort)."""
        subscription = str((settings or {}).get("subscription_id") or "")
        app_id = str((settings or {}).get("app_id") or "")
        if not subscription or not app_id:
            return
        provider = self._whatsapp_provider(
            WhatsAppCredentials(
                provider="gupshup",
                number="+10000000",
                api_token=api_key,
                app_id=app_id,
                app_name=str(settings.get("app_name") or ""),
            )
        )
        try:
            if isinstance(provider, GupshupProvider):
                await provider.unsubscribe(subscription)
        except WhatsAppError as error:
            log.warning("a Gupshup subscription delete failed org=%s: %s", org_id, error)
        finally:
            await provider.aclose()

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
    "whatsapp": (
        SecretName.WHATSAPP_API_TOKEN,
        SecretName.WHATSAPP_APP_SECRET,
        SecretName.WHATSAPP_LINK_SECRET,
    ),
    "email": (SecretName.SENDGRID_API_KEY, SecretName.EMAIL_LINK_SECRET),
}
_NOUNS: Final = {"whatsapp": "WhatsApp number", "email": "email address"}


def _short_error(prefix: str, error: Exception) -> str:
    """A failure text for ``last_error``: the reason of the app, with known secrets removed and
    cut to ``MAX_ERROR_CHARS``."""
    reason = default_redactor().text(str(error))
    return f"{prefix}: {reason}"[:MAX_ERROR_CHARS]


def _org_key(org_id: uuid.UUID, kind: str) -> str:
    return f"sama.integration:org:{org_id}:{kind}"


def whatsapp_external_id(credentials: WhatsAppCredentials) -> str:
    """The ``external_id`` of a WhatsApp row: the account or app of the provider that the
    credentials proved, with the number. A number alone is not proven, so it cannot block the
    same number in another org (Section 11.9)."""
    if credentials.provider == "meta":
        return f"meta:{credentials.phone_number_id}"
    if credentials.provider == "twilio":
        return f"twilio:{credentials.account_sid}:{credentials.number}"
    return f"gupshup:{credentials.app_id}"


def _external_key(kind: str, external_id: str) -> str:
    """The lock of one bot, Slack workspace, WhatsApp number, or email address."""
    return f"sama.integration:{kind}:{external_id}"


def _bot_key(bot_id: str) -> str:
    return _external_key("telegram", bot_id)


def _team_key(team_id: str) -> str:
    return _external_key("slack", team_id)


@dataclass(frozen=True, slots=True)
class ChannelSetup:
    """The result of a WhatsApp or email connect: the URL that the admin gives to the
    provider, and the steps that are left for a person."""

    integration: IntegrationView
    webhook_url: str
    verify_token: str | None = None
    """The verify token of the Meta webhook. sama shows it once and keeps only its digest."""
    steps: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            **self.integration.public(),
            "webhook_url": self.webhook_url,
            "verify_token": self.verify_token,
            "steps": self.steps,
        }


def whatsapp_steps(provider: str, url: str, token: str) -> list[str]:
    """The steps that a person does at the provider after the connect (Section 11.9)."""
    if provider == "meta":
        return [
            "In the Meta app, open WhatsApp > Configuration > Webhook and click Edit.",
            f"Set the callback URL to {url} and the verify token to {token}. Click Verify and"
            " save.",
            "Subscribe the webhook field messages.",
            "A business-initiated message needs an approved template. Replies within 24 hours of"
            " a user message do not.",
        ]
    if provider == "twilio":
        return [
            "In the Twilio console, open Messaging > Senders > WhatsApp senders (or the WhatsApp"
            " sandbox) and select the number.",
            f"Set 'When a message comes in' to {url} with the method POST, and save.",
            "For the sandbox, each tester first sends the join word of the sandbox to its number.",
        ]
    return [
        "sama added the webhook subscription of the Gupshup app. Check it under the app >"
        " Webhooks in the Gupshup dashboard.",
        "Each tester of a Gupshup app that is not live first opts in to the number.",
    ]


def email_steps(address: str, url: str) -> list[str]:
    domain = address.partition("@")[2]
    return [
        f"Add an MX record for {domain} that points to mx.sendgrid.net (priority 10). Use a"
        " subdomain that receives no other mail.",
        f"In SendGrid, authenticate the domain {domain} (Settings > Sender Authentication), so"
        f" that mail from {address} passes DKIM.",
        f"In SendGrid, add an Inbound Parse host for {domain} with the URL {url}. Do not tick"
        " 'Post the raw, full MIME message' unless you want it; sama reads both forms.",
        "Make a security policy with signature verification for the parse webhook and attach it."
        " Paste its public key here when you connect.",
    ]


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
