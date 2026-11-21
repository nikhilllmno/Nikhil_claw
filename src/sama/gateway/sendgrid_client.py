"""A small client for the SendGrid v3 API (docs/architecture.md, Section 11.9).

sama sends the email of a channel with ``POST /v3/mail/send`` and checks a new API key with
``GET /v3/scopes``. The key goes only in the ``Authorization`` header. Errors never contain it.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
from pydantic import SecretStr

API_URL: Final = "https://api.sendgrid.com"
API_KEY: Final = re.compile(r"SG\.[A-Za-z0-9_-]{16,32}\.[A-Za-z0-9_-]{20,64}")
SEND_SCOPE: Final = "mail.send"


class SendGridError(Exception):
    """A failed SendGrid call. The message is safe to log."""


@dataclass(frozen=True, slots=True)
class Attachment:
    name: str
    mime_type: str
    data: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class Mail:
    """One plain text email."""

    sender: str
    sender_name: str
    to: str
    subject: str
    text: str
    reply_to: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    """For example ``Message-ID``, ``In-Reply-To``, and ``References``."""
    attachments: tuple[Attachment, ...] = ()

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "personalizations": [{"to": [{"email": self.to}]}],
            "from": {"email": self.sender, "name": self.sender_name},
            "subject": self.subject,
            "content": [{"type": "text/plain", "value": self.text or " "}],
        }
        if self.reply_to:
            out["reply_to"] = {"email": self.reply_to}
        if self.headers:
            out["headers"] = dict(self.headers)
        if self.attachments:
            out["attachments"] = [
                {
                    "content": base64.b64encode(a.data).decode(),
                    "type": a.mime_type,
                    "filename": a.name,
                    "disposition": "attachment",
                }
                for a in self.attachments
            ]
        return out


class SendGridClient:
    def __init__(
        self,
        api_key: SecretStr,
        *,
        base_url: str = API_URL,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = 20.0,
    ) -> None:
        self._key = api_key
        self._base_url = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=timeout_s)

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key.get_secret_value()}"}

    async def scopes(self) -> list[str]:
        """The scopes of the key. Raises ``SendGridError`` when SendGrid refuses the key."""
        try:
            response = await self._http.get(f"{self._base_url}/v3/scopes", headers=self._headers)
        except httpx.HTTPError as error:
            raise SendGridError(f"SendGrid cannot be reached: {type(error).__name__}") from None
        if response.status_code != 200:
            raise SendGridError(f"SendGrid refused the key: {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            data = {}
        scopes = data.get("scopes") if isinstance(data, dict) else None
        return [str(s) for s in scopes] if isinstance(scopes, list) else []

    async def send(self, mail: Mail) -> str:
        """Sends ``mail`` and returns the SendGrid message id."""
        try:
            response = await self._http.post(
                f"{self._base_url}/v3/mail/send", headers=self._headers, json=mail.body()
            )
        except httpx.HTTPError as error:
            raise SendGridError(f"SendGrid cannot be reached: {type(error).__name__}") from None
        if response.status_code >= 300:
            detail = ""
            try:
                errors = response.json().get("errors") or []
                detail = "; ".join(str(e.get("message") or "") for e in errors)[:300]
            except (ValueError, AttributeError):
                pass
            raise SendGridError(f"SendGrid refused the email: {response.status_code} {detail}")
        return str(response.headers.get("x-message-id", ""))
