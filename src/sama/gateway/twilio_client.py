"""A small client for the WhatsApp messages of the Twilio API (docs/architecture.md, Section
11.9).

Twilio signs each webhook request with the auth token of the account: ``X-Twilio-Signature``
is the Base64 HMAC-SHA1 of the full request URL and the sorted form fields
(https://www.twilio.com/docs/usage/security). ``valid_signature`` checks it in constant time.

The auth token is the password of the HTTP basic auth of each API call. Errors never contain
it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from collections.abc import Iterable
from typing import Any, Final
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from sama.gateway.download import TOO_LARGE

API_URL: Final = "https://api.twilio.com"
MAX_TEXT: Final = 1600
"""The longest message body that Twilio sends to WhatsApp."""
ACCOUNT_SID: Final = re.compile(r"AC[0-9a-f]{32}")
AUTH_TOKEN: Final = re.compile(r"[0-9a-f]{32}")
PHONE: Final = re.compile(r"\+[1-9][0-9]{6,14}")
"""A phone number in E.164 form."""
MEDIA_HOSTS: Final = ("twilio.com", "twiliocdn.com", "amazonaws.com")
"""The hosts where a media URL of Twilio can redirect to. Twilio keeps the files there."""
MAX_REDIRECTS: Final = 3
_REDIRECTS: Final = frozenset({301, 302, 303, 307, 308})


class TwilioError(Exception):
    """A failed Twilio call. The message is safe to log."""


def signature(auth_token: str, url: str, fields: Iterable[tuple[str, str]]) -> str:
    """The ``X-Twilio-Signature`` of a form request to ``url``."""
    values: dict[str, set[str]] = {}
    for name, value in fields:
        values.setdefault(name, set()).add(value)
    text = url + "".join(name + value for name in sorted(values) for value in sorted(values[name]))
    digest = hmac.new(auth_token.encode(), text.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def valid_signature(
    auth_token: str, url: str, fields: Iterable[tuple[str, str]], given: str | None
) -> bool:
    if not given:
        return False
    return hmac.compare_digest(signature(auth_token, url, fields), given)


def whatsapp(number: str) -> str:
    """The Twilio address of a WhatsApp number."""
    return f"whatsapp:{number}"


def _media_host(host: str) -> bool:
    return any(host == name or host.endswith(f".{name}") for name in MEDIA_HOSTS)


class TwilioClient:
    def __init__(
        self,
        account_sid: str,
        auth_token: SecretStr,
        *,
        base_url: str = API_URL,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = 20.0,
    ) -> None:
        self.account_sid = account_sid
        self._auth = httpx.BasicAuth(account_sid, auth_token.get_secret_value())
        self._base_url = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=timeout_s)

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def _account_url(self) -> str:
        return f"{self._base_url}/2010-04-01/Accounts/{self.account_sid}"

    async def account(self) -> dict[str, Any]:
        """The account of the SID. Raises ``TwilioError`` when Twilio refuses the token."""
        return await self._call("GET", f"{self._account_url}.json")

    async def send(
        self, sender: str, to: str, text: str | None, *, media_url: str | None = None
    ) -> str:
        """Sends one WhatsApp message and returns its SID. ``sender`` and ``to`` are phone
        numbers in E.164 form."""
        form = {"From": whatsapp(sender), "To": whatsapp(to)}
        if text:
            form["Body"] = text[:MAX_TEXT]
        if media_url:
            form["MediaUrl"] = media_url
        answer = await self._call("POST", f"{self._account_url}/Messages.json", data=form)
        return str(answer.get("sid") or "")

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        """The bytes of a media URL of an inbound message. Only a URL of the account gets the
        credentials. Twilio redirects to its file store: a redirect gets no credentials and
        must go over ``https`` to a host of Twilio."""
        if not url.startswith(f"{self._account_url}/Messages/"):
            raise TwilioError("download failed: the media URL is not of this account")
        auth: httpx.BasicAuth | None = self._auth
        for _ in range(MAX_REDIRECTS + 1):
            data, location = await self._fetch(url, auth=auth, max_bytes=max_bytes)
            if data is not None:
                return data
            parts = urlsplit(location or "")
            if parts.scheme != "https" or not _media_host((parts.hostname or "").lower()):
                raise TwilioError("download failed: the media is not at Twilio")
            url, auth = str(location), None
        raise TwilioError("download failed: too many redirects")

    async def _fetch(
        self, url: str, *, auth: httpx.BasicAuth | None, max_bytes: int
    ) -> tuple[bytes | None, str | None]:
        """``(bytes, None)`` for a file, or ``(None, location)`` for a redirect. The cap counts
        the bytes on the wire, as ``sama.gateway.download.fetch_capped`` does."""
        data = bytearray()
        try:
            async with self._http.stream(
                "GET",
                url,
                auth=auth,
                headers={"Accept-Encoding": "identity"},
                follow_redirects=False,
            ) as response:
                if response.status_code in _REDIRECTS:
                    return None, response.headers.get("location")
                if response.status_code != 200:
                    raise TwilioError(f"download failed: {response.status_code}")
                encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
                if encoding not in ("", "identity"):
                    raise TwilioError("download failed: the answer is encoded")
                declared = response.headers.get("Content-Length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise TwilioError(TOO_LARGE)
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > max_bytes:
                        raise TwilioError(TOO_LARGE)
                    data.extend(chunk)
        except httpx.HTTPError as error:
            raise TwilioError(f"download failed: {type(error).__name__}") from None
        return bytes(data), None

    async def _call(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._http.request(method, url, auth=self._auth, **kwargs)
        except httpx.HTTPError as error:
            raise TwilioError(f"Twilio cannot be reached: {type(error).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400 or not isinstance(body, dict):
            message = body.get("message") if isinstance(body, dict) else None
            code = body.get("code") if isinstance(body, dict) else None
            raise TwilioError(
                f"Twilio refused the call: {response.status_code}"
                + (f" {code}" if code else "")
                + (f" {message}" if message else "")
            )
        return body
