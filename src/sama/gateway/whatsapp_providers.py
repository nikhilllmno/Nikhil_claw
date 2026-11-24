"""The WhatsApp providers of sama (docs/architecture.md, Section 11.9).

An org connects WhatsApp through one of three providers. The ``WhatsAppGateway`` sees only the
``WhatsAppProvider`` interface:

- ``verify`` checks the signature of a webhook request:
  - Meta WhatsApp Cloud API: ``X-Hub-Signature-256``, the HMAC-SHA256 of the raw body with the
    app secret;
  - Twilio: ``X-Twilio-Signature``, the HMAC-SHA1 of the URL and the sorted form fields with
    the auth token;
  - Gupshup: Gupshup does not sign its callbacks. The hub checks the secret token in the
    webhook URL (all providers have one), and the provider checks the secret header that the
    subscription of sama asks Gupshup to send.
- ``parse`` turns a webhook body into messages: the sender, the text, and the media. A pressed
  button is a message with the text of its payload, for example ``yes AB12CD``.
- ``send_text``, ``send_buttons``, and ``send_media`` send output. Twilio has no buttons
  without an approved template, so its cards ask for a reply code.
- ``download`` reads the media of an inbound message, with a byte cap.

Each provider calls only its own fixed API host. A media download goes only to the hosts of
the provider.
"""

from __future__ import annotations

import abc
import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal
from urllib.parse import parse_qsl, urlsplit

import httpx
from pydantic import SecretStr

from sama.gateway.download import TOO_LARGE
from sama.gateway.twilio_client import (
    ACCOUNT_SID,
    AUTH_TOKEN,
    TwilioClient,
    TwilioError,
    valid_signature,
)
from sama.media.generate import StoredMedia

Provider = Literal["meta", "twilio", "gupshup"]
PROVIDERS: Final[tuple[Provider, ...]] = ("meta", "twilio", "gupshup")
PROVIDER_TITLES: Final[dict[str, str]] = {
    "meta": "Meta WhatsApp Cloud API",
    "twilio": "Twilio",
    "gupshup": "Gupshup",
}
GRAPH_URL: Final = "https://graph.facebook.com/v21.0"
GUPSHUP_URL: Final = "https://api.gupshup.io"
SECRET_HEADER: Final = "x-sama-webhook-secret"  # noqa: S105 (a header name)
"""The header that the Gupshup subscription of sama sends (its ``meta`` field)."""
PHONE: Final = re.compile(r"\+[1-9][0-9]{6,14}")
MAX_REDIRECTS: Final = 3
_REDIRECTS: Final = frozenset({301, 302, 303, 307, 308})
MAX_BUTTON_TITLE: Final = 20


class WhatsAppError(Exception):
    """A failed provider call. The message is safe to log and has no secret."""


@dataclass(frozen=True, slots=True)
class MediaRef:
    """A file of an inbound message, before the download."""

    ref: str
    """A media id (Meta) or a URL (Twilio, Gupshup)."""
    mime_type: str
    name: str = "file"


@dataclass(frozen=True, slots=True)
class Inbound:
    """One message from a WhatsApp user."""

    id: str
    sender: str
    """The phone number of the sender in E.164 form, with ``+``."""
    text: str
    media: tuple[MediaRef, ...] = ()


@dataclass(frozen=True, slots=True)
class Button:
    title: str
    payload: str
    """The text that a press sends back, for example ``yes AB12CD``."""


def e164(number: object) -> str | None:
    """``+15551234567`` for a number with or without ``+`` and the ``whatsapp:`` prefix."""
    text = str(number or "").strip().removeprefix("whatsapp:").replace(" ", "")
    if text and not text.startswith("+"):
        text = f"+{text}"
    return text if PHONE.fullmatch(text) else None


def _host_of(url: str, hosts: Sequence[str]) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h or host.endswith(f".{h}") for h in hosts)


@dataclass(slots=True)
class _Http:
    """A capped GET that follows redirects only to the hosts of the provider. Credentials go
    only to the first URL."""

    http: httpx.AsyncClient
    hosts: tuple[str, ...]

    async def get(self, url: str, *, max_bytes: int, headers: Mapping[str, str] | None) -> bytes:
        sent = dict(headers or {})
        for _ in range(MAX_REDIRECTS + 1):
            if not _host_of(url, self.hosts):
                raise WhatsAppError("download failed: the media is not at the provider")
            data, location = await self._one(url, sent, max_bytes)
            if data is not None:
                return data
            url, sent = str(location or ""), {}
        raise WhatsAppError("download failed: too many redirects")

    async def _one(
        self, url: str, headers: dict[str, str], max_bytes: int
    ) -> tuple[bytes | None, str | None]:
        data = bytearray()
        try:
            async with self.http.stream(
                "GET",
                url,
                headers={**headers, "Accept-Encoding": "identity"},
                follow_redirects=False,
            ) as response:
                if response.status_code in _REDIRECTS:
                    return None, response.headers.get("location")
                if response.status_code != 200:
                    raise WhatsAppError(f"download failed: {response.status_code}")
                encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
                if encoding not in ("", "identity"):
                    raise WhatsAppError("download failed: the answer is encoded")
                declared = response.headers.get("Content-Length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise WhatsAppError(TOO_LARGE)
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > max_bytes:
                        raise WhatsAppError(TOO_LARGE)
                    data.extend(chunk)
        except httpx.HTTPError as error:
            raise WhatsAppError(f"download failed: {type(error).__name__}") from None
        return bytes(data), None


_OFFICE: Final = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
)
MESSAGE_TYPES: Final[Mapping[str, str]] = {
    "image/png": "image",
    "image/jpeg": "image",
    "audio/mpeg": "audio",
    "audio/ogg": "audio",
    "video/mp4": "video",
    "application/pdf": "document",
    "text/plain": "document",
    **dict.fromkeys(_OFFICE, "document"),
}
"""The WhatsApp message type of each file type that WhatsApp carries (Section 11.10). WhatsApp
refuses other types, for example CSV, Markdown, and zip."""


def message_type(media: StoredMedia) -> str:
    """``image``, ``audio``, ``video``, or ``document`` for a file of a run."""
    if media.kind in ("image", "audio"):
        return media.kind
    return MESSAGE_TYPES.get(media.mime_type, "document")


class WhatsAppProvider(abc.ABC):
    name: Provider
    max_text: int = 4096
    buttons: bool = True
    file_types: frozenset[str] = frozenset(MESSAGE_TYPES)
    """The types of a ``file_send`` file that the provider can send. ``WhatsAppChat`` sends a
    note instead of a file of another type."""
    """False when an approval card asks for a reply code and has no buttons."""

    @abc.abstractmethod
    def verify(self, url: str, headers: Mapping[str, str], body: bytes) -> bool:
        """True when the provider signed the request. ``url`` is the public webhook URL."""

    @abc.abstractmethod
    def parse(self, body: bytes, content_type: str) -> list[Inbound]:
        """The messages of a verified webhook body. Status events give no message."""

    @abc.abstractmethod
    async def check(self) -> None:
        """Asks the provider whether the credentials work. Raises ``WhatsAppError``."""

    @abc.abstractmethod
    async def send_text(self, to: str, text: str) -> str:
        """Sends a text and returns the message id."""

    async def send_buttons(self, to: str, text: str, buttons: Sequence[Button]) -> str:
        """Sends a text with reply buttons. A provider without buttons sends the text only."""
        return await self.send_text(to, text)

    @abc.abstractmethod
    async def send_media(self, to: str, media: StoredMedia, url: str) -> str:
        """Sends an image, a voice note, or a file (a document) that the provider reads from
        ``url``."""

    @abc.abstractmethod
    async def download(self, media: MediaRef, *, max_bytes: int) -> bytes:
        """The bytes of a file of an inbound message."""

    @abc.abstractmethod
    async def aclose(self) -> None: ...


# -- Meta and the Meta webhook format -----------------------------------------------------------


def _meta_messages(body: bytes, *, phone_number_id: str | None, number: str) -> list[Inbound]:
    """The messages of a webhook body in the format of the Meta Cloud API. Gupshup sends the
    same format with a subscription of version 3. A message to another number is dropped."""
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return []
    found: list[Inbound] = []
    for entry in data.get("entry") or [] if isinstance(data, dict) else []:
        for change in (entry or {}).get("changes") or []:
            value = (change or {}).get("value") or {}
            metadata = value.get("metadata") or {}
            if phone_number_id is not None and str(metadata.get("phone_number_id")) != (
                phone_number_id
            ):
                continue
            shown = metadata.get("display_phone_number")
            if phone_number_id is None and shown and e164(shown) != number:
                continue
            for message in value.get("messages") or []:
                parsed = _meta_message(message)
                if parsed is not None:
                    found.append(parsed)
    return found


def _meta_message(message: Mapping[str, Any]) -> Inbound | None:
    sender = e164(message.get("from"))
    if sender is None or not message.get("id"):
        return None
    kind = str(message.get("type") or "")
    text = ""
    media: list[MediaRef] = []
    if kind == "text":
        text = str((message.get("text") or {}).get("body") or "")
    elif kind == "interactive":
        interactive = message.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        text = str(reply.get("id") or reply.get("title") or "")
    elif kind == "button":
        button = message.get("button") or {}
        text = str(button.get("payload") or button.get("text") or "")
    elif kind in ("image", "audio", "document", "voice", "video", "sticker"):
        item = message.get(kind) or {}
        ref = item.get("url") or item.get("link") or item.get("id")
        if ref:
            media.append(
                MediaRef(
                    str(ref),
                    str(item.get("mime_type") or ""),
                    str(item.get("filename") or kind),
                )
            )
        text = str(item.get("caption") or "")
    return Inbound(str(message["id"]), sender, text.strip(), tuple(media))


class MetaProvider(WhatsAppProvider):
    """The WhatsApp Cloud API of Meta (https://developers.facebook.com/docs/whatsapp)."""

    name = "meta"

    def __init__(
        self,
        *,
        number: str,
        phone_number_id: str,
        access_token: SecretStr,
        app_secret: SecretStr,
        base_url: str = GRAPH_URL,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.number = number
        self.phone_number_id = phone_number_id
        self._token = access_token
        self._app_secret = app_secret
        self._base_url = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=20.0)
        self._get = _Http(self._http, ("facebook.com", "fbsbx.com", "whatsapp.net"))

    def verify(self, url: str, headers: Mapping[str, str], body: bytes) -> bool:
        given = headers.get("x-hub-signature-256", "")
        digest = hmac.new(
            self._app_secret.get_secret_value().encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(f"sha256={digest}", given)

    def parse(self, body: bytes, content_type: str) -> list[Inbound]:
        return _meta_messages(body, phone_number_id=self.phone_number_id, number=self.number)

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token.get_secret_value()}"}

    async def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._http.request(
                method, f"{self._base_url}/{path}", headers=self._auth, **kwargs
            )
        except httpx.HTTPError as error:
            raise WhatsAppError(f"Meta cannot be reached: {type(error).__name__}") from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400 or not isinstance(data, dict):
            problem = (data.get("error") or {}) if isinstance(data, dict) else {}
            message = str(problem.get("message") or "")[:200]
            raise WhatsAppError(f"Meta refused the call: {response.status_code} {message}".strip())
        return data

    async def check(self) -> None:
        data = await self._call("GET", f"{self.phone_number_id}?fields=display_phone_number")
        # The token proves the phone number id. The number must be the number of that id.
        if e164(data.get("display_phone_number")) != self.number:
            raise WhatsAppError("the phone number id belongs to another number")

    async def _send(self, to: str, body: dict[str, Any]) -> str:
        message = {"messaging_product": "whatsapp", "to": to.lstrip("+"), **body}
        data = await self._call("POST", f"{self.phone_number_id}/messages", json=message)
        sent = data.get("messages") or [{}]
        return str(sent[0].get("id") or "")

    async def send_text(self, to: str, text: str) -> str:
        return await self._send(to, {"type": "text", "text": {"body": text[: self.max_text]}})

    async def send_buttons(self, to: str, text: str, buttons: Sequence[Button]) -> str:
        replies = [
            {"type": "reply", "reply": {"id": b.payload, "title": b.title[:MAX_BUTTON_TITLE]}}
            for b in buttons[:3]
        ]
        return await self._send(
            to,
            {
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": text[:1024]},
                    "action": {"buttons": replies},
                },
            },
        )

    async def send_media(self, to: str, media: StoredMedia, url: str) -> str:
        kind = message_type(media)
        body: dict[str, str] = {"link": url}
        if kind == "document":
            body["filename"] = media.filename
        return await self._send(to, {"type": kind, kind: body})

    async def download(self, media: MediaRef, *, max_bytes: int) -> bytes:
        if media.ref.startswith("https://"):
            url = media.ref
        else:
            info = await self._call("GET", media.ref)
            url = str(info.get("url") or "")
        return await self._get.get(url, max_bytes=max_bytes, headers=self._auth)

    async def aclose(self) -> None:
        await self._http.aclose()


# -- Twilio -------------------------------------------------------------------------------------


class TwilioProvider(WhatsAppProvider):
    """WhatsApp through Twilio (https://www.twilio.com/docs/whatsapp)."""

    name = "twilio"
    max_text = 1600
    buttons = False
    file_types = frozenset(
        {"image/png", "image/jpeg", "audio/mpeg", "audio/ogg", "video/mp4", "application/pdf"}
    )

    def __init__(self, *, number: str, client: TwilioClient, auth_token: SecretStr) -> None:
        self.number = number
        self._client = client
        self._auth_token = auth_token

    def verify(self, url: str, headers: Mapping[str, str], body: bytes) -> bool:
        fields = parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        return valid_signature(
            self._auth_token.get_secret_value(), url, fields, headers.get("x-twilio-signature")
        )

    def parse(self, body: bytes, content_type: str) -> list[Inbound]:
        form: dict[str, str] = {}
        for name, value in parse_qsl(
            body.decode("utf-8", errors="replace"), keep_blank_values=True
        ):
            form.setdefault(name, value)
        sender = e164(form.get("From"))
        if (
            sender is None
            or not form.get("From", "").startswith("whatsapp:")
            or e164(form.get("To")) != self.number
            or not form.get("MessageSid")
        ):
            return []
        count = int(form["NumMedia"]) if form.get("NumMedia", "").isdigit() else 0
        media = tuple(
            MediaRef(form[f"MediaUrl{i}"], form.get(f"MediaContentType{i}", ""), f"media{i}")
            for i in range(min(count, 10))
            if form.get(f"MediaUrl{i}")
        )
        text = form.get("ButtonPayload") or form.get("Body") or ""
        return [Inbound(form["MessageSid"], sender, text.strip(), media)]

    async def check(self) -> None:
        try:
            await self._client.account()
        except TwilioError as error:
            raise WhatsAppError(str(error)) from None

    async def send_text(self, to: str, text: str) -> str:
        try:
            return await self._client.send(self.number, to, text[: self.max_text])
        except TwilioError as error:
            raise WhatsAppError(str(error)) from None

    async def send_media(self, to: str, media: StoredMedia, url: str) -> str:
        try:
            return await self._client.send(self.number, to, None, media_url=url)
        except TwilioError as error:
            raise WhatsAppError(str(error)) from None

    async def download(self, media: MediaRef, *, max_bytes: int) -> bytes:
        try:
            return await self._client.download(media.ref, max_bytes=max_bytes)
        except TwilioError as error:
            raise WhatsAppError(str(error)) from None

    async def aclose(self) -> None:
        await self._client.aclose()


# -- Gupshup ------------------------------------------------------------------------------------


class GupshupProvider(WhatsAppProvider):
    """WhatsApp through Gupshup (https://docs.gupshup.io). Callbacks come in the Gupshup
    format (version 2) or in the Meta format (version 3); sama reads both."""

    name = "gupshup"

    def __init__(
        self,
        *,
        number: str,
        app_id: str,
        app_name: str,
        api_key: SecretStr,
        header_secret: SecretStr | None = None,
        base_url: str = GUPSHUP_URL,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        """``header_secret`` is the value of ``X-Sama-Webhook-Secret`` that the subscription
        of sama sends."""
        self.number = number
        self.app_id = app_id
        self.app_name = app_name
        self.api_key = api_key
        self.header_secret = header_secret
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.AsyncClient(timeout=20.0)

    def verify(self, url: str, headers: Mapping[str, str], body: bytes) -> bool:
        # The hub checked the token of the webhook URL. The header of the subscription is the
        # second check. Without a secret the check fails closed.
        if self.header_secret is None or not self.header_secret.get_secret_value():
            return False
        given = headers.get(SECRET_HEADER, "")
        return hmac.compare_digest(self.header_secret.get_secret_value(), given)

    def parse(self, body: bytes, content_type: str) -> list[Inbound]:
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return []
        if not isinstance(data, dict):
            return []
        if "entry" in data:
            return _meta_messages(body, phone_number_id=None, number=self.number)
        if data.get("type") != "message" or data.get("app") not in (None, self.app_name):
            return []
        payload = data.get("payload") or {}
        sender = e164((payload.get("sender") or {}).get("phone") or payload.get("source"))
        if sender is None or not payload.get("id"):
            return []
        kind = str(payload.get("type") or "")
        inner = payload.get("payload") or {}
        text = ""
        media: tuple[MediaRef, ...] = ()
        if kind == "text":
            text = str(inner.get("text") or "")
        elif kind in ("button_reply", "quick_reply", "list_reply"):
            text = str(inner.get("postbackText") or inner.get("id") or inner.get("title") or "")
            text = text or str(inner.get("text") or "")
        elif kind in ("image", "audio", "file", "video", "voice", "sticker"):
            url = inner.get("url")
            if url:
                mime = str(inner.get("contentType") or "")
                media = (MediaRef(str(url), mime, str(inner.get("name") or kind)),)
            text = str(inner.get("caption") or "")
        return [Inbound(str(payload["id"]), sender, text.strip(), media)]

    @property
    def _headers(self) -> dict[str, str]:
        return {"apikey": self.api_key.get_secret_value()}

    async def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self.http.request(
                method, f"{self.base_url}/{path}", headers=self._headers, **kwargs
            )
        except httpx.HTTPError as error:
            raise WhatsAppError(f"Gupshup cannot be reached: {type(error).__name__}") from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        if (
            response.status_code >= 400
            or not isinstance(data, dict)
            or data.get("status")
            in (
                "error",
                "failed",
            )
        ):
            message = data.get("message") if isinstance(data, dict) else None
            if isinstance(message, dict):
                message = message.get("message")
            detail = str(message or "")[:200]
            status = response.status_code
            raise WhatsAppError(f"Gupshup refused the call: {status} {detail}".strip())
        return data

    async def check(self) -> None:
        data = await self._call("GET", f"wa/app/{self.app_id}")
        app = data.get("app") or {}
        if app.get("name") and app.get("name") != self.app_name:
            raise WhatsAppError("the app id belongs to another Gupshup app")

    async def subscribe(self, url: str, secret: SecretStr) -> str:
        """Asks Gupshup to send the messages of the app to ``url`` (version 3, the Meta
        format), with the secret header. Returns the subscription id."""
        data = await self._call(
            "POST",
            f"wa/app/{self.app_id}/subscription",
            data={
                "url": url,
                "tag": "sama",
                "version": "3",
                "modes": "MESSAGE",
                "doCheck": "false",
                "showOnUI": "false",
                "meta": json.dumps({"X-Sama-Webhook-Secret": secret.get_secret_value()}),
            },
        )
        subscription = data.get("subscription") or {}
        return str(subscription.get("id") or "")

    async def unsubscribe(self, subscription_id: str) -> None:
        await self._call("DELETE", f"wa/app/{self.app_id}/subscription/{subscription_id}")

    async def _send(self, to: str, message: dict[str, Any]) -> str:
        data = await self._call(
            "POST",
            "wa/api/v1/msg",
            data={
                "channel": "whatsapp",
                "source": self.number.lstrip("+"),
                "destination": to.lstrip("+"),
                "src.name": self.app_name,
                "message": json.dumps(message),
            },
        )
        return str(data.get("messageId") or "")

    async def send_text(self, to: str, text: str) -> str:
        return await self._send(to, {"type": "text", "text": text[: self.max_text]})

    async def send_buttons(self, to: str, text: str, buttons: Sequence[Button]) -> str:
        options = [
            {"type": "text", "title": b.title[:MAX_BUTTON_TITLE], "postbackText": b.payload}
            for b in buttons[:3]
        ]
        return await self._send(
            to,
            {
                "type": "quick_reply",
                "msgid": buttons[0].payload if buttons else "card",
                "content": {"type": "text", "text": text[:1024]},
                "options": options,
            },
        )

    async def send_media(self, to: str, media: StoredMedia, url: str) -> str:
        if media.kind == "image":
            return await self._send(to, {"type": "image", "originalUrl": url, "previewUrl": url})
        kind = message_type(media)
        if kind == "image":
            return await self._send(to, {"type": "image", "originalUrl": url, "previewUrl": url})
        if kind == "document":
            return await self._send(to, {"type": "file", "url": url, "filename": media.filename})
        return await self._send(to, {"type": kind, "url": url})

    async def download(self, media: MediaRef, *, max_bytes: int) -> bytes:
        getter = _Http(self.http, ("gupshup.io", "whatsapp.net", "fbsbx.com"))
        headers = self._headers if _host_of(media.ref, ("gupshup.io",)) else None
        return await getter.get(media.ref, max_bytes=max_bytes, headers=headers)

    async def aclose(self) -> None:
        await self.http.aclose()


# -- credentials --------------------------------------------------------------------------------

META_ID: Final = re.compile(r"[0-9]{5,30}")
GUPSHUP_APP_ID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
GUPSHUP_APP_NAME: Final = re.compile(r"[A-Za-z0-9_-]{1,100}")
TOKEN: Final = re.compile(r"[A-Za-z0-9_\-.|:=+/]{16,600}")


@dataclass(frozen=True, slots=True)
class WhatsAppCredentials:
    """What an org admin gives to connect a WhatsApp number (Section 11.9). ``api_token`` is
    the access token (Meta), the auth token (Twilio), or the API key (Gupshup). ``app_secret``
    is the app secret of Meta, or the secret header of the Gupshup subscription."""

    provider: Provider
    number: str
    api_token: SecretStr
    app_secret: SecretStr | None = None
    phone_number_id: str | None = None
    account_sid: str | None = None
    app_id: str | None = None
    app_name: str | None = None

    def problem(self) -> str | None:
        """Why the credentials cannot work, or ``None``. The text is safe to show."""
        token = self.api_token.get_secret_value()
        if PHONE.fullmatch(self.number) is None:
            return "the number must be in international form, for example +15551234567."
        if self.provider == "meta":
            if not self.phone_number_id or META_ID.fullmatch(self.phone_number_id) is None:
                return "copy the phone number ID from WhatsApp > API Setup in the Meta app."
            if TOKEN.fullmatch(token) is None:
                return "this is not a Meta access token. Make a system user token."
            secret = self.app_secret.get_secret_value() if self.app_secret else ""
            if re.fullmatch(r"[0-9a-f]{32}", secret) is None:
                return "copy the app secret from App settings > Basic in the Meta app."
        elif self.provider == "twilio":
            if not self.account_sid or ACCOUNT_SID.fullmatch(self.account_sid) is None:
                return "the account SID starts with AC. Copy it from the Twilio console."
            if AUTH_TOKEN.fullmatch(token) is None:
                return "this is not a Twilio auth token. Copy it from the Twilio console."
        else:
            if not self.app_id or GUPSHUP_APP_ID.fullmatch(self.app_id) is None:
                return "copy the app ID of the Gupshup app from its dashboard."
            if not self.app_name or GUPSHUP_APP_NAME.fullmatch(self.app_name) is None:
                return "copy the app name of the Gupshup app from its dashboard."
            if TOKEN.fullmatch(token) is None:
                return "this is not a Gupshup API key. Copy it from the Gupshup dashboard."
        return None


def build_provider(
    credentials: WhatsAppCredentials, *, http: httpx.AsyncClient | None = None
) -> WhatsAppProvider:
    """The provider of ``credentials``. ``http`` is for tests."""
    if credentials.provider == "meta":
        return MetaProvider(
            number=credentials.number,
            phone_number_id=str(credentials.phone_number_id),
            access_token=credentials.api_token,
            app_secret=credentials.app_secret or SecretStr(""),
            http=http,
        )
    if credentials.provider == "twilio":
        sid = str(credentials.account_sid)
        return TwilioProvider(
            number=credentials.number,
            client=TwilioClient(sid, credentials.api_token, http=http),
            auth_token=credentials.api_token,
        )
    return GupshupProvider(
        number=credentials.number,
        app_id=str(credentials.app_id),
        app_name=str(credentials.app_name),
        api_key=credentials.api_token,
        header_secret=credentials.app_secret,
        http=http,
    )
