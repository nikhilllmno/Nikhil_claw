"""The webhook and the approval links of the email integrations (docs/architecture.md, Section
11.9).

- ``POST /email/<integration id>/inbound``: SendGrid Inbound Parse posts each email here. The
  hub reads the row, checks the ECDSA signature of the security policy with the public key of
  the row (``sama.gateway.mail_inbound.signed``), parses the body, and gives the email to the
  ``EmailGateway``. An unknown id or a wrong signature gets ``401``.
- ``GET /email/<integration id>/approvals/<token>``: the page of an approval link. It shows a
  button and changes nothing, so a mail scanner that opens the link decides nothing.
- ``POST`` on the same URL decides the approval as the user of the link, while that user still
  has a linked address on the integration.
"""

from __future__ import annotations

import hmac
import html
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Approval, ChannelIdentity, Integration
from sama.gate.service import ApprovalError, GateService
from sama.gateway.links import ApprovalLink, read_approval_token
from sama.gateway.mail import EmailAddress, EmailGateway
from sama.gateway.mail_inbound import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    BadKey,
    parse,
    public_key,
    signed,
)
from sama.gateway.pairing import Lockout
from sama.gateway.sendgrid_client import SendGridClient
from sama.gateway.sessions import ALWAYS_OUTCOME, FollowSettings, decide
from sama.integrations.hub import GatewayCache
from sama.integrations.request_body import read_body
from sama.integrations.service import SendGridFactory, secret_hash
from sama.orchestrator.queue import RunQueue
from sama.review.apply import ReviewApplier
from sama.skills.service import SkillLibrary
from sama.vault.crypto import VaultError
from sama.vault.store import SecretName, Vault

log: Final = logging.getLogger("sama.integrations.email_hub")

UNKNOWN: Final = "unknown webhook or wrong signature"
MAX_BODY_BYTES: Final = 30 * 1024 * 1024
"""SendGrid posts emails of up to 30 MB with their attachments."""
_CHOICE_WORDS: Final = {"y": "Approve", "n": "Deny", "a": "Approve and always allow for me"}
BAD_LINK: Final = "This link is not valid or has expired."


@dataclass(slots=True)
class _Address:
    gateway: EmailGateway
    client: SendGridClient
    key: ec.EllipticCurvePublicKey
    link_secret: SecretStr
    updated_at: Any

    async def aclose(self) -> None:
        await self.gateway.aclose()
        await self.client.aclose()


def _page(title: str, body: str) -> HTMLResponse:
    """A small page without scripts. ``body`` is HTML that the caller escaped."""
    return HTMLResponse(
        "<!doctype html><html><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;"
        "padding:0 1rem;line-height:1.5}button{font-size:1rem;padding:.5rem 1rem}</style>"
        f"</head><body><h1>{html.escape(title)}</h1>{body}</body></html>",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline';"
            " form-action 'self'",
            "Referrer-Policy": "no-referrer",
        },
    )


class EmailHub(GatewayCache[_Address]):
    def __init__(
        self,
        engine: AsyncEngine,
        vault: Vault,
        *,
        public_url: str,
        settings: FollowSettings | None = None,
        sendgrid_client: SendGridFactory = SendGridClient,
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
        self._settings = settings or FollowSettings()
        self._sendgrid_client = sendgrid_client
        self._queue = queue or RunQueue(engine)
        self._gate = gate or GateService(engine)
        self._skills = skills
        self._reviews = reviews
        self._clock = clock
        self._lockout = Lockout()
        self._followers: set[object] = set()

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.post("/email/{integration_id}/inbound", include_in_schema=False)
        async def inbound(integration_id: str, request: Request) -> dict[str, str]:
            body = await read_body(request, MAX_BODY_BYTES)
            address = await self._address(integration_id)
            if not signed(
                address.key,
                request.headers.get(SIGNATURE_HEADER),
                request.headers.get(TIMESTAMP_HEADER),
                body,
                now=self._clock(),
            ):
                raise HTTPException(status_code=401, detail=UNKNOWN)
            mail = parse(body, request.headers.get("content-type", ""))
            if mail is not None:
                address.gateway.accept(mail)
            return {}

        @router.get("/email/{integration_id}/approvals/{token}", include_in_schema=False)
        async def approval_page(integration_id: str, token: str) -> HTMLResponse:
            found = await self._link(integration_id, token)
            if found is None:
                return _page("sama", f"<p>{BAD_LINK}</p>")
            link, tool = found
            word = _CHOICE_WORDS[link.choice]
            return _page(
                "sama approval",
                f"<p>The agent asks to use <b>{html.escape(tool)}</b>.</p>"
                f'<form method="post"><button type="submit">{html.escape(word)}</button></form>',
            )

        @router.post("/email/{integration_id}/approvals/{token}", include_in_schema=False)
        async def approval_decide(integration_id: str, token: str) -> HTMLResponse:
            found = await self._link(integration_id, token)
            if found is None:
                return _page("sama", f"<p>{BAD_LINK}</p>")
            link, _ = found
            org_id = await self._org_of(integration_id)
            try:
                decided = await decide(
                    self._engine,
                    self._gate,
                    self._queue,
                    org_id=org_id,
                    user_id=link.user_id,
                    approval_id=link.approval_id,
                    approve=link.choice in "ya",
                    always_allow=link.choice == "a",
                    reviews=self._reviews,
                )
            except ApprovalError as error:
                return _page("sama", f"<p>{html.escape(str(error))}</p>")
            outcome = decided.outcome
            text = f"{outcome}: {decided.tool_name}."
            if outcome == ALWAYS_OUTCOME:
                text += " sama does not ask you again for this tool."
            return _page("sama", f"<p>{html.escape(text)}</p><p>You can close this page.</p>")

        return router

    async def _row(self, integration_id: str) -> Any:
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
                    ).where(Integration.id == key, Integration.kind == "email")
                )
            ).one_or_none()
        if row is None or row.status != "active" or not row.external_id or not row.secret_hash:
            raise HTTPException(status_code=401, detail=UNKNOWN)
        return row

    async def _org_of(self, integration_id: str) -> uuid.UUID:
        return uuid.UUID(str((await self._row(integration_id)).org_id))

    async def _address(self, integration_id: str) -> _Address:
        row = await self._row(integration_id)
        address = self._bots.get(row.id)
        if address is None or address.updated_at != row.updated_at:
            async with self._lock:
                address = self._bots.get(row.id)
                if address is None or address.updated_at != row.updated_at:
                    self.forget(row.id)
                    address = await self._build(row)
                    self._bots[row.id] = address
        return address

    async def _build(self, row: Any) -> _Address:
        settings = row.settings or {}
        try:
            api_key = await self._vault.get(row.org_id, SecretName.SENDGRID_API_KEY, fresh=True)
            link = await self._vault.get(row.org_id, SecretName.EMAIL_LINK_SECRET, fresh=True)
        except VaultError:
            log.exception("an email integration secret does not decrypt id=%s", row.id)
            raise HTTPException(status_code=503, detail="the address is not available") from None
        if (
            api_key is None
            or link is None
            or not hmac.compare_digest(secret_hash(api_key.get_secret_value()), row.secret_hash)
        ):
            log.error("an email integration has no secrets id=%s", row.id)
            raise HTTPException(status_code=503, detail="the address is not available")
        try:
            key = public_key(str(settings.get("public_key") or ""))
        except BadKey:
            log.error("an email integration has no valid public key id=%s", row.id)
            raise HTTPException(status_code=503, detail="the address is not available") from None
        client = self._sendgrid_client(api_key)
        gateway = EmailGateway(
            self._engine,
            client,
            address=EmailAddress(
                row.id,
                row.org_id,
                str(row.external_id),
                str(settings.get("sender_name") or "sama"),
            ),
            public_url=self._public_url,
            link_secret=link.get_secret_value(),
            clock=self._clock,
            settings=self._settings,
            queue=self._queue,
            gate=self._gate,
            skills=self._skills,
            reviews=self._reviews,
            lockout=self._lockout,
            followers=self._followers,
        )
        return _Address(gateway, client, key, link, row.updated_at)

    async def _link(self, integration_id: str, token: str) -> tuple[ApprovalLink, str] | None:
        """The link and the tool name of a valid token, while the user of the link still has
        a linked address on the integration."""
        try:
            address = await self._address(integration_id)
        except HTTPException:
            return None
        link = read_approval_token(address.link_secret.get_secret_value(), token, now=self._clock())
        if link is None:
            return None
        gateway = address.gateway
        async with self._engine.connect() as connection:
            linked = await connection.scalar(
                select(ChannelIdentity.id).where(
                    ChannelIdentity.integration_id == gateway.integration_id,
                    ChannelIdentity.org_id == gateway.org_id,
                    ChannelIdentity.user_id == link.user_id,
                )
            )
            tool = await connection.scalar(
                select(Approval.tool_name).where(
                    Approval.org_id == gateway.org_id, Approval.id == link.approval_id
                )
            )
        if linked is None or tool is None:
            return None
        return link, str(tool)
