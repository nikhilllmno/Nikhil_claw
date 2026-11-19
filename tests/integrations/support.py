"""Fakes and helpers for the tests of the integrations and the Telegram gateway."""

from __future__ import annotations

import itertools
import json
import uuid
from typing import Any

import httpx
from pydantic import SecretStr
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Integration
from sama.gateway.telegram_client import TelegramClient
from tests.orchestrator.conftest import Tenant

BOT_ID = 999
TOKEN = f"{BOT_ID}:" + "a" * 35


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
        self._ids = itertools.count(100)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if "/file/" in request.url.path:
            return httpx.Response(200, content=b"OGG")
        token_part, method = request.url.path.rsplit("/", 2)[-2:]
        token = token_part.removeprefix("bot")
        body = json.loads(request.content or b"{}")
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
        if method == "sendMessage":
            message_id = next(self._ids)
            self.messages[message_id] = (body["chat_id"], body["text"])
            return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})
        if method == "editMessageText" and body["message_id"] in self.messages:
            self.messages[body["message_id"]] = (body["chat_id"], body["text"])
        if method == "getFile":
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
