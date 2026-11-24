"""A small client for the Telegram Bot API (https://core.telegram.org/bots/api).

The bot token is part of each request URL. Errors never contain the URL, and the
``python -m sama.gateway`` entry point sets the ``httpx`` logger to ``WARNING`` so that
request URLs are not logged.
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any, Final

import httpx
from pydantic import SecretStr

from sama.gateway.download import fetch_capped

API_URL: Final = "https://api.telegram.org"
MAX_TEXT: Final = 4096
"""The longest message text that Telegram accepts."""


class TelegramError(Exception):
    """A failed Bot API call. ``retry_after`` is set when Telegram asks the bot to wait."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def bot_id(token: SecretStr) -> int | None:
    """The user id of the bot: the number before the colon in its token."""
    head, _, _ = token.get_secret_value().partition(":")
    return int(head) if head.isdigit() else None


class TelegramClient:
    def __init__(
        self,
        token: SecretStr,
        *,
        base_url: str = API_URL,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = 20.0,
        max_retries: int = 3,
        max_wait_s: float = 30.0,
    ) -> None:
        """``max_retries`` and ``max_wait_s`` limit the waits when Telegram answers
        ``429 Too Many Requests`` with ``retry_after``. A longer wait raises at once."""
        self._token = token
        self._max_retries = max_retries
        self._max_wait_s = max_wait_s
        self._base_url = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=timeout_s)

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def max_wait_s(self) -> float:
        return self._max_wait_s

    def without_retries(self) -> TelegramClient:
        """The same client, but a ``429`` answer raises at once. The caller then decides
        whether to wait and try again. Close only the first client."""
        view = copy.copy(self)
        view._max_retries = 0
        return view

    async def call(
        self,
        method: str,
        *,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        **params: Any,
    ) -> Any:
        """Calls ``method``. With ``files``, the body is ``multipart/form-data``: a nested value
        goes as JSON text, as the Bot API asks."""
        url = f"{self._base_url}/bot{self._token.get_secret_value()}/{method}"
        body = {name: value for name, value in params.items() if value is not None}
        form = {
            name: json.dumps(value) if isinstance(value, dict | list) else str(value)
            for name, value in body.items()
        }
        for attempt in range(self._max_retries + 1):
            try:
                if files is None:
                    response = await self._http.post(url, json=body)
                else:
                    response = await self._http.post(url, data=form, files=files)
                data = response.json()
            except (httpx.HTTPError, ValueError) as error:
                raise TelegramError(f"{method} failed: {type(error).__name__}") from None
            if isinstance(data, dict) and data.get("ok"):
                return data.get("result")
            data = data if isinstance(data, dict) else {}
            wait = (data.get("parameters") or {}).get("retry_after")
            wait = wait if isinstance(wait, int | float) else None
            if wait is None or wait > self._max_wait_s or attempt == self._max_retries:
                break
            await asyncio.sleep(wait)
        raise TelegramError(
            f"{method} failed: {data.get('description', response.status_code)}", retry_after=wait
        )

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to: int | None = None,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> int:
        """Sends plain text and returns the message id."""
        result = await self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text[:MAX_TEXT],
            reply_parameters=_reply(reply_to),
            reply_markup=None if buttons is None else {"inline_keyboard": buttons},
            link_preview_options={"is_disabled": True},
        )
        return int(result["message_id"])

    async def send_photo(self, chat_id: int, data: bytes, *, reply_to: int | None = None) -> int:
        """Sends a PNG image (``image_generate``, Section 11.8). Returns the message id."""
        result = await self.call(
            "sendPhoto",
            files={"photo": ("image.png", data, "image/png")},
            chat_id=chat_id,
            reply_parameters=_reply(reply_to),
        )
        return int(result["message_id"])

    async def send_document(
        self,
        chat_id: int,
        data: bytes,
        *,
        filename: str,
        mime_type: str,
        reply_to: int | None = None,
    ) -> int:
        """Sends a file of ``file_send`` as a document (Section 11.10). Returns the message
        id."""
        result = await self.call(
            "sendDocument",
            files={"document": (filename, data, mime_type)},
            chat_id=chat_id,
            reply_parameters=_reply(reply_to),
        )
        return int(result["message_id"])

    async def send_voice(self, chat_id: int, data: bytes, *, reply_to: int | None = None) -> int:
        """Sends an Ogg Opus voice note (``text_to_speech``). Returns the message id."""
        result = await self.call(
            "sendVoice",
            files={"voice": ("voice.ogg", data, "audio/ogg")},
            chat_id=chat_id,
            reply_parameters=_reply(reply_to),
        )
        return int(result["message_id"])

    async def edit_message(self, chat_id: int, message_id: int, text: str) -> None:
        """Changes the text of a message and removes its buttons."""
        try:
            await self.call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=text[:MAX_TEXT],
                reply_markup={"inline_keyboard": []},
                link_preview_options={"is_disabled": True},
            )
        except TelegramError as error:
            if "message is not modified" not in str(error):
                raise

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    async def typing(self, chat_id: int) -> None:
        await self.call("sendChatAction", chat_id=chat_id, action="typing")

    async def download(self, file_id: str, *, max_bytes: int) -> bytes:
        """Downloads a file that a user sent. Raises ``TelegramError`` above ``max_bytes``."""
        info = await self.call("getFile", file_id=file_id)
        if int(info.get("file_size") or 0) > max_bytes or not info.get("file_path"):
            raise TelegramError("getFile failed: the file is too large")
        url = f"{self._base_url}/file/bot{self._token.get_secret_value()}/{info['file_path']}"
        return await fetch_capped(self._http, url, max_bytes=max_bytes, fail=TelegramError)

    async def set_webhook(self, url: str, secret: SecretStr) -> None:
        await self.call(
            "setWebhook",
            url=url,
            secret_token=secret.get_secret_value(),
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=False,
        )

    async def get_me(self) -> dict[str, Any]:
        """The bot of the token. Raises ``TelegramError`` for a wrong token."""
        me = await self.call("getMe")
        if not isinstance(me, dict) or not me.get("is_bot") or "id" not in me:
            raise TelegramError("getMe failed: the token is not the token of a bot")
        return me

    async def delete_webhook(self) -> None:
        await self.call("deleteWebhook", drop_pending_updates=True)


def _reply(reply_to: int | None) -> dict[str, Any] | None:
    if reply_to is None:
        return None
    return {"message_id": reply_to, "allow_sending_without_reply": True}
