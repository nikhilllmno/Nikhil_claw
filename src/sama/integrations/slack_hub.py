"""The request URLs of the Slack integrations (docs/architecture.md, Section 11.6).

Slack sends the requests of each app to three URLs of its integration row:

- ``POST /slack/<integration id>/events``: the Events API (JSON);
- ``POST /slack/<integration id>/interactions``: button clicks (a form with a JSON
  ``payload``);
- ``POST /slack/<integration id>/commands``: the ``/sama`` command (a form).

The hub:

1. reads the integration row. An unknown id gets ``401``. A ``pending`` row answers only the
   ``url_verification`` challenge that Slack sends when the admin makes the app from the
   manifest. The row has no signing secret yet, and the challenge shows nothing;
2. finds the ``SlackGateway`` of an ``active`` row. It builds the gateway on the first request,
   with the bot token and the signing secret from the vault, and keeps it while the
   ``updated_at`` of the row stays the same. The SHA-256 digest of the signing secret must be
   the digest in the row, else the hub answers ``503``;
3. checks the ``X-Slack-Signature`` of the raw body with the signing secret, and the time in
   ``X-Slack-Request-Timestamp``. A wrong signature gets ``401``;
4. gives an event or a click to the gateway, which handles it in the background, and answers a
   command at once with a message that only the sender sees.
"""

from __future__ import annotations

import hmac
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Final
from urllib.parse import parse_qs

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Integration
from sama.gate.service import GateService
from sama.gateway.pairing import Lockout
from sama.gateway.sessions import FollowSettings
from sama.gateway.slack import SeenEvents, SlackApp, SlackGateway, escape
from sama.gateway.slack_client import SlackClient, signed
from sama.integrations.hub import GatewayCache
from sama.integrations.service import SlackClientFactory, secret_hash
from sama.orchestrator.queue import RunQueue
from sama.vault.crypto import VaultError
from sama.vault.store import SecretName, Vault

log: Final = logging.getLogger("sama.integrations.slack_hub")

UNKNOWN: Final = "unknown request URL or wrong signature"
MAX_BODY_BYTES: Final = 1024 * 1024


@dataclass(slots=True)
class _App:
    gateway: SlackGateway
    client: SlackClient
    signing_secret: SecretStr
    team_id: str
    updated_at: Any

    async def aclose(self) -> None:
        await self.gateway.aclose()
        await self.client.aclose()


class SlackHub(GatewayCache[_App]):
    def __init__(
        self,
        engine: AsyncEngine,
        vault: Vault,
        *,
        settings: FollowSettings | None = None,
        slack_client: SlackClientFactory = SlackClient,
        queue: RunQueue | None = None,
        gate: GateService | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__()
        self._engine = engine
        self._vault = vault
        self._settings = settings or FollowSettings()
        self._slack_client = slack_client
        self._queue = queue or RunQueue(engine)
        self._gate = gate or GateService(engine)
        self._clock = clock
        self._lockout = Lockout()
        self._followers: set[object] = set()
        self._seen = SeenEvents()

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.post("/slack/{integration_id}/events", include_in_schema=False)
        async def events(
            integration_id: str,
            request: Request,
            x_slack_request_timestamp: Annotated[str | None, Header()] = None,
            x_slack_signature: Annotated[str | None, Header()] = None,
        ) -> dict[str, Any]:
            body = await _body(request)
            app = await self.app(integration_id, body, x_slack_request_timestamp, x_slack_signature)
            envelope = _json(body)
            if envelope.get("type") == "url_verification":
                return {"challenge": str(envelope.get("challenge") or "")}
            if app is None:
                raise HTTPException(status_code=401, detail=UNKNOWN)
            if envelope.get("type") == "event_callback" and envelope.get("team_id") == app.team_id:
                app.gateway.accept(envelope)
            return {}

        @router.post("/slack/{integration_id}/interactions", include_in_schema=False)
        async def interactions(
            integration_id: str,
            request: Request,
            x_slack_request_timestamp: Annotated[str | None, Header()] = None,
            x_slack_signature: Annotated[str | None, Header()] = None,
        ) -> Response:
            body = await _body(request)
            app = await self._active(
                integration_id, body, x_slack_request_timestamp, x_slack_signature
            )
            payload = _json(_form(body).get("payload", "").encode())
            team = payload.get("team") or {}
            if isinstance(team, dict) and team.get("id") == app.team_id:
                app.gateway.accept_action(payload)
            return Response(status_code=200)

        @router.post("/slack/{integration_id}/commands", include_in_schema=False)
        async def commands(
            integration_id: str,
            request: Request,
            x_slack_request_timestamp: Annotated[str | None, Header()] = None,
            x_slack_signature: Annotated[str | None, Header()] = None,
        ) -> dict[str, str]:
            body = await _body(request)
            app = await self._active(
                integration_id, body, x_slack_request_timestamp, x_slack_signature
            )
            form = _form(body)
            if form.get("team_id") != app.team_id:
                raise HTTPException(status_code=401, detail=UNKNOWN)
            text = await app.gateway.command(form)
            return {"response_type": "ephemeral", "text": escape(text)}

        return router

    async def _active(
        self, integration_id: str, body: bytes, timestamp: str | None, given: str | None
    ) -> _App:
        app = await self.app(integration_id, body, timestamp, given)
        if app is None:
            raise HTTPException(status_code=401, detail=UNKNOWN)
        return app

    async def app(
        self, integration_id: str, body: bytes, timestamp: str | None, given: str | None
    ) -> _App | None:
        """The app of an ``active`` integration when the request is signed by it, or None for
        a ``pending`` integration. Raises ``HTTPException``."""
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
                        Integration.external_id,
                        Integration.secret_hash,
                        Integration.settings,
                        Integration.updated_at,
                    ).where(Integration.id == key, Integration.kind == "slack")
                )
            ).one_or_none()
        if row is None:
            raise HTTPException(status_code=401, detail=UNKNOWN)
        if row.status != "active" or not row.external_id or not row.secret_hash:
            return None
        app = self._bots.get(key)
        if app is None or app.updated_at != row.updated_at:
            async with self._lock:
                app = self._bots.get(key)
                if app is None or app.updated_at != row.updated_at:
                    self.forget(key)
                    app = await self._build(row)
                    self._bots[key] = app
        secret = app.signing_secret.get_secret_value()
        if not signed(secret, timestamp, given, body, self._clock()):
            raise HTTPException(status_code=401, detail=UNKNOWN)
        return app

    async def _build(self, row: Any) -> _App:
        try:
            # Not from the cache: another process can have replaced the app a moment ago.
            token = await self._vault.get(row.org_id, SecretName.SLACK_BOT_TOKEN, fresh=True)
            signing = await self._vault.get(row.org_id, SecretName.SLACK_SIGNING_SECRET, fresh=True)
        except VaultError:
            log.exception("a Slack integration secret does not decrypt id=%s", row.id)
            raise HTTPException(status_code=503, detail="the app is not available") from None
        if (
            token is None
            or signing is None
            or not hmac.compare_digest(secret_hash(signing.get_secret_value()), row.secret_hash)
        ):
            log.error("a Slack integration has no secrets for its app id=%s", row.id)
            raise HTTPException(status_code=503, detail="the app is not available")
        settings = row.settings or {}
        client = self._slack_client(token)
        gateway = SlackGateway(
            self._engine,
            client,
            app=SlackApp(
                integration_id=row.id,
                org_id=row.org_id,
                team_id=str(row.external_id),
                bot_user_id=str(settings.get("bot_user_id") or ""),
                app_id=settings.get("app_id"),
            ),
            settings=self._settings,
            queue=self._queue,
            gate=self._gate,
            lockout=self._lockout,
            followers=self._followers,
            seen=self._seen,
        )
        return _App(gateway, client, signing, str(row.external_id), row.updated_at)


async def _body(request: Request) -> bytes:
    """The raw body: the signature covers the bytes as Slack sent them."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="the body is too large")
    # Read in chunks and stop at the limit: a chunked body has no Content-Length.
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="the body is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _json(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="the body is not JSON") from None
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="the body is not an object")
    return value


def _form(body: bytes) -> dict[str, str]:
    """The fields of a form body. A field that is given more than once keeps its first value."""
    fields = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {name: values[0] for name, values in fields.items()}
