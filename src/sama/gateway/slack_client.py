"""A small client for the Slack Web API (https://docs.slack.dev/reference/methods) and the
request signature check of Slack (https://docs.slack.dev/authentication/verifying-requests-from-slack).

The bot token goes in the ``Authorization`` header and never in a URL or an error.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import re
import time
from typing import Any, Final

import httpx
from pydantic import SecretStr

API_URL: Final = "https://slack.com/api"
MAX_TEXT: Final = 39_000
"""Slack cuts a message text after 40,000 characters."""
MAX_SIGNATURE_AGE_S: Final = 300
"""A signed request older (or newer) than 5 minutes is refused, so a copy cannot be sent
again later."""

BOT_TOKEN: Final = re.compile(r"xoxb-[A-Za-z0-9-]{20,250}")
SIGNING_SECRET: Final = re.compile(r"[0-9a-f]{32}")


class SlackError(Exception):
    """A failed Web API call. ``retry_after`` is set when Slack asks the app to wait."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def signature(secret: str, timestamp: str, body: bytes) -> str:
    """The ``X-Slack-Signature`` value of a request."""
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def signed(
    secret: str, timestamp: str | None, given: str | None, body: bytes, now: float | None = None
) -> bool:
    """True when ``given`` is the signature of ``body`` and the time is in the window."""
    if not timestamp or not given or not timestamp.isdigit():
        return False
    now = time.time() if now is None else now
    if abs(now - int(timestamp)) > MAX_SIGNATURE_AGE_S:
        return False
    return hmac.compare_digest(signature(secret, timestamp, body), given)


class SlackClient:
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
        """``max_retries`` and ``max_wait_s`` limit the waits when Slack answers ``429`` with
        ``Retry-After``. A longer wait raises at once."""
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

    def without_retries(self) -> SlackClient:
        """The same client, but a ``429`` answer raises at once. Close only the first
        client."""
        view = copy.copy(self)
        view._max_retries = 0
        return view

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        url = f"{self._base_url}/{method}"
        body = {name: value for name, value in params.items() if value is not None}
        headers = {"Authorization": f"Bearer {self._token.get_secret_value()}"}
        wait: float | None = None
        error = "unknown"
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._http.post(url, json=body, headers=headers)
            except httpx.HTTPError as failure:
                raise SlackError(f"{method} failed: {type(failure).__name__}") from None
            if response.status_code == 429:
                wait = _retry_after(response)
                error = "ratelimited"
            else:
                try:
                    data = response.json()
                except ValueError:
                    raise SlackError(f"{method} failed: {response.status_code}") from None
                if isinstance(data, dict) and data.get("ok"):
                    return data
                error = str((data if isinstance(data, dict) else {}).get("error") or "unknown")
                wait = None
            if wait is None or wait > self._max_wait_s or attempt == self._max_retries:
                break
            await asyncio.sleep(wait)
        raise SlackError(f"{method} failed: {error}", retry_after=wait)

    async def auth_test(self) -> dict[str, Any]:
        """The workspace and the bot user of the token. Raises ``SlackError``."""
        data = await self.call("auth.test")
        if not data.get("team_id") or not data.get("user_id") or not data.get("bot_id"):
            raise SlackError("auth.test failed: the token is not the token of a bot")
        return data

    async def app_id(self, bot_id: str) -> str | None:
        """The app of the bot, or None (best effort)."""
        try:
            data = await self.call("bots.info", bot=bot_id)
        except SlackError:
            return None
        value = (data.get("bot") or {}).get("app_id")
        return str(value) if value else None

    async def post_message(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> str:
        """Posts plain text (or blocks with ``text`` as the fallback) and returns the ``ts``
        of the message."""
        data = await self.call(
            "chat.postMessage",
            channel=channel,
            text=text[:MAX_TEXT],
            thread_ts=thread_ts,
            blocks=blocks,
            unfurl_links=False,
            unfurl_media=False,
        )
        return str(data["ts"])

    async def update_message(self, channel: str, ts: str, text: str) -> None:
        """Changes the text of a message and removes its blocks (for example buttons)."""
        await self.call("chat.update", channel=channel, ts=ts, text=text[:MAX_TEXT], blocks=[])

    async def post_ephemeral(
        self, channel: str, user: str, text: str, *, thread_ts: str | None = None
    ) -> None:
        """A message that only ``user`` sees."""
        await self.call(
            "chat.postEphemeral", channel=channel, user=user, text=text, thread_ts=thread_ts
        )

    async def revoke(self) -> None:
        await self.call("auth.revoke")


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After", "")
    return float(value) if value.isdigit() else None
