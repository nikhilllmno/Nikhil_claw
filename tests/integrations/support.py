"""Fakes and helpers for the tests of the integrations and the chat gateways."""

from __future__ import annotations

import itertools
import json
import uuid
from email.parser import BytesParser
from email.policy import default
from typing import Any
from urllib.parse import parse_qs

import httpx
from pydantic import SecretStr
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Integration
from sama.gateway.slack_client import SlackClient
from sama.gateway.telegram_client import TelegramClient
from sama.integrations.service import secret_hash
from tests.orchestrator.conftest import Tenant

BOT_ID = 999
TOKEN = f"{BOT_ID}:" + "a" * 35

TEAM = "T0SAMA"
BOT_USER = "UBOTSAMA"
APP_ID = "A0SAMA"
SLACK_TOKEN = "xoxb-" + "1" * 12 + "-" + "b" * 24
SIGNING = "5" * 32


class FakeTelegram:
    """The Bot API: it records calls and gives each sent message a new id.

    ``bots`` maps a token to the answer of ``getMe``. ``failing`` holds the methods that fail.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.tokens: list[tuple[str, str]] = []
        self.messages: dict[int, tuple[int, str]] = {}
        self.bots: dict[str, dict[str, Any]] = {
            TOKEN: {"id": BOT_ID, "is_bot": True, "username": "sama_bot"}
        }
        self.failing: set[str] = set()
        self.files: dict[str, bytes] = {}
        """The bytes of the files that users sent, by ``file_id``. A voice note has none: it
        gets the bytes ``OGG``."""
        self.media: list[tuple[str, int, bytes]] = []
        """The ``(method, chat id, bytes)`` of each photo and voice note that the bot sent."""
        self._ids = itertools.count(100)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if "/file/" in request.url.path:
            name = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, content=self.files.get(name, b"OGG"))
        token_part, method = request.url.path.rsplit("/", 2)[-2:]
        token = token_part.removeprefix("bot")
        body = request_body(request)
        self.calls.append((method, body))
        self.tokens.append((token, method))
        if method in self.failing:
            return httpx.Response(
                400, json={"ok": False, "error_code": 400, "description": "Bad Request: no"}
            )
        if method == "getMe":
            me = self.bots.get(token)
            if me is None:
                return httpx.Response(
                    401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
                )
            return httpx.Response(200, json={"ok": True, "result": me})
        if method in ("sendPhoto", "sendVoice", "sendDocument"):
            field = {"sendPhoto": "photo", "sendVoice": "voice"}.get(method, "document")
            self.media.append((method, int(body["chat_id"]), body[field]))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": next(self._ids)}})
        if method == "sendMessage":
            message_id = next(self._ids)
            self.messages[message_id] = (body["chat_id"], body["text"])
            return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})
        if method == "editMessageText" and body["message_id"] in self.messages:
            self.messages[body["message_id"]] = (body["chat_id"], body["text"])
        if method == "getFile":
            data = self.files.get(str(body.get("file_id")))
            if data is not None:
                result = {"file_path": body["file_id"], "file_size": len(data)}
                return httpx.Response(200, json={"ok": True, "result": result})
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "v.oga"}})
        return httpx.Response(200, json={"ok": True, "result": True})

    def client(self, token: SecretStr) -> TelegramClient:
        """A client factory for ``Integrations`` and ``TelegramHub``."""
        return TelegramClient(
            token,
            base_url="https://tg.test",
            http=httpx.AsyncClient(transport=httpx.MockTransport(self)),
        )

    def method_calls(self, method: str) -> list[dict[str, Any]]:
        return [body for name, body in self.calls if name == method]

    def sent(self, chat_id: int | None = None) -> list[dict[str, Any]]:
        return [
            body for body in self.method_calls("sendMessage") if chat_id in (None, body["chat_id"])
        ]

    def texts(self, chat_id: int | None = None) -> list[str]:
        return [body["text"] for body in self.sent(chat_id)]

    def shown(self, chat_id: int) -> list[str]:
        """The text of each message in the chat, after the edits."""
        return [text for chat, text in self.messages.values() if chat == chat_id]


async def add_bot(
    engine: AsyncEngine,
    tenant: Tenant,
    username: str = "sama_bot",
    *,
    status: str = "active",
    secret_hash: str | None = None,
    external_id: str | None = None,
) -> uuid.UUID:
    """Connects a Telegram bot to the org of ``tenant`` without the Bot API. The id goes to
    ``tenant.ids["bot"]``."""
    integration_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(Integration).values(
                id=integration_id,
                org_id=tenant.org,
                kind="telegram",
                status=status,
                external_id=external_id or str(uuid.uuid4().int % 10**10),
                name=username,
                secret_hash=secret_hash,
            )
        )
    tenant.ids["bot"] = integration_id
    return integration_id


class FakeSlack:
    """The Slack Web API: it records calls and gives each posted message a new ``ts``.

    ``teams`` maps a bot token to the answer of ``auth.test``. ``failing`` holds the methods
    that fail. ``limited`` holds the methods that answer ``429`` once.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.tokens: list[tuple[str, str]] = []
        self.messages: dict[str, tuple[str, str]] = {}
        self.teams: dict[str, dict[str, Any]] = {
            SLACK_TOKEN: {
                "team_id": TEAM,
                "team": "Sama HQ",
                "user_id": BOT_USER,
                "bot_id": "B0SAMA",
            }
        }
        self.failing: set[str] = set()
        self.limited: set[str] = set()
        self.files: dict[str, bytes] = {}
        """The bytes of the shared files, by URL."""
        self.downloads: list[tuple[str, str]] = []
        """The ``(url, bearer token)`` of each file download."""
        self.uploads: list[tuple[str, bytes, str | None]] = []
        """The ``(upload URL, bytes, Authorization header)`` of each file upload."""
        self.upload_url = "https://files.slack.com/upload/v1/ABC"
        self._ts = itertools.count(1)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            url = str(request.url)
            token = request.headers.get("Authorization", "").removeprefix("Bearer ")
            self.downloads.append((url, token))
            data = self.files.get(url)
            return httpx.Response(404 if data is None else 200, content=data or b"")
        if request.url.host == "files.slack.com":
            auth = request.headers.get("Authorization")
            self.uploads.append((str(request.url), request.content, auth))
            return httpx.Response(200, text="OK")
        method = request.url.path.rsplit("/", 1)[-1]
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        body = request_body(request)
        self.calls.append((method, body))
        self.tokens.append((token, method))
        if method in self.limited:
            self.limited.discard(method)
            return httpx.Response(429, headers={"Retry-After": "0"})
        if method in self.failing:
            return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
        if method == "auth.test":
            me = self.teams.get(token)
            if me is None:
                return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
            return httpx.Response(200, json={"ok": True, **me})
        if method == "bots.info":
            return httpx.Response(200, json={"ok": True, "bot": {"app_id": APP_ID}})
        if method == "files.getUploadURLExternal":
            return httpx.Response(
                200, json={"ok": True, "upload_url": self.upload_url, "file_id": "F0FILE1"}
            )
        if method == "chat.postMessage":
            ts = f"1700000000.{next(self._ts):06d}"
            self.messages[ts] = (body["channel"], body["text"])
            return httpx.Response(200, json={"ok": True, "ts": ts, "channel": body["channel"]})
        if method == "conversations.open":
            # The direct message of a user ``U...`` is ``D...`` with the same rest.
            channel = "D" + str(body["users"])[1:]
            return httpx.Response(200, json={"ok": True, "channel": {"id": channel}})
        if method == "chat.update" and body["ts"] in self.messages:
            self.messages[body["ts"]] = (body["channel"], body["text"])
        return httpx.Response(200, json={"ok": True})

    def client(self, token: SecretStr) -> SlackClient:
        """A client factory for ``Integrations`` and ``SlackHub``."""
        return SlackClient(
            token,
            base_url="https://slack.test/api",
            http=httpx.AsyncClient(transport=httpx.MockTransport(self)),
        )

    def method_calls(self, method: str) -> list[dict[str, Any]]:
        return [body for name, body in self.calls if name == method]

    def posted(self, channel: str | None = None) -> list[dict[str, Any]]:
        return [
            body
            for body in self.method_calls("chat.postMessage")
            if channel in (None, body["channel"])
        ]

    def texts(self, channel: str | None = None) -> list[str]:
        return [body["text"] for body in self.posted(channel)]

    def whispers(self, channel: str | None = None) -> list[str]:
        return [
            body["text"]
            for body in self.method_calls("chat.postEphemeral")
            if channel in (None, body["channel"])
        ]

    def shown(self, channel: str) -> list[str]:
        """The text of each message in the channel, after the edits."""
        return [text for place, text in self.messages.values() if place == channel]


async def add_slack(
    engine: AsyncEngine,
    tenant: Tenant,
    *,
    status: str = "active",
    team_id: str | None = TEAM,
    signing: str | None = SIGNING,
) -> uuid.UUID:
    """Connects a Slack app to the org of ``tenant`` without the Web API. The id goes to
    ``tenant.ids["slack"]``."""
    integration_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(Integration).values(
                id=integration_id,
                org_id=tenant.org,
                kind="slack",
                status=status,
                external_id=team_id,
                name="Sama HQ",
                settings={"bot_user_id": BOT_USER, "bot_id": "B0SAMA", "app_id": APP_ID},
                secret_hash=None if signing is None else secret_hash(signing),
            )
        )
    tenant.ids["slack"] = integration_id
    return integration_id


def request_body(request: httpx.Request) -> dict[str, Any]:
    """The parameters of a fake API call: a JSON, a form, or a multipart body. A file part of
    a multipart body is its bytes."""
    kind = request.headers.get("content-type", "")
    if kind.startswith("multipart/form-data"):
        head = f"Content-Type: {kind}\r\n\r\n".encode()
        message = BytesParser(policy=default).parsebytes(head + request.content)
        body: dict[str, Any] = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True)
            assert isinstance(name, str)
            assert isinstance(payload, bytes)
            body[name] = payload if part.get_filename() else payload.decode()
        return body
    if kind.startswith("application/x-www-form-urlencoded"):
        return {name: values[0] for name, values in parse_qs(request.content.decode()).items()}
    loaded = json.loads(request.content or b"{}")
    assert isinstance(loaded, dict)
    return loaded
