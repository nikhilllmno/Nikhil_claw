"""A small client of the Composio API (https://docs.composio.dev/reference).

sama uses the hosted link flow of Composio for every auth scheme (Section 10.3):

1. ``auth_config`` finds (or makes) the Composio-managed auth config of a toolkit;
2. ``link`` makes a connected account for an entity and gives a ``redirect_url``. The user
   opens it in the browser and signs in on the Composio page (OAuth, or a form for a key);
3. ``status`` reads the status of the account. Only this check sets a connection
   ``connected``;
4. ``execute`` runs a tool with the account.

The client never returns or logs the ``data`` or ``state`` of an account: they can hold
credentials. The API key is in the ``x-api-key`` header only. A Composio error message is cut
and goes through the redactor before it reaches a log, a row, or the agent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx
from pydantic import SecretStr

from sama.vault.redact import redact

log: Final = logging.getLogger("sama.connectors.composio")

BASE_URL: Final = "https://backend.composio.dev"
TIMEOUT_S: Final = 30.0
MAX_ERROR_CHARS: Final = 300

ACTIVE: Final = "ACTIVE"
PENDING_STATUSES: Final = frozenset({"INITIALIZING", "INITIATED"})
EXPIRED_STATUSES: Final = frozenset({"EXPIRED", "INACTIVE", "REVOKED"})


class ComposioError(Exception):
    """A Composio call failed. The message is safe to show: it holds no secret."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class Link:
    account_id: str
    redirect_url: str


class ComposioClient:
    """One client for the process. Each call takes the API key of the org."""

    def __init__(
        self, *, base_url: str = BASE_URL, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=TIMEOUT_S, transport=transport, follow_redirects=True
        )
        self._auth_configs: dict[tuple[str, str], str] = {}
        """``(key digest, toolkit)`` to the auth config id. One Composio project per key."""
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        key: SecretStr,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        try:
            response = await self._http.request(
                method,
                path,
                json=json_body,
                params=params,
                headers={"x-api-key": key.get_secret_value()},
            )
        except httpx.HTTPError as error:
            raise ComposioError(f"Composio cannot be reached: {type(error).__name__}") from None
        if response.status_code >= 400:
            raise ComposioError(
                f"Composio answered {response.status_code}: {_message(response)}",
                status_code=response.status_code,
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            raise ComposioError("Composio gave an answer that is not JSON") from None

    async def auth_config(self, key: SecretStr, toolkit: str) -> str:
        """The id of the auth config of the toolkit. It uses the first Composio-managed config
        of the project, and makes one if there is none."""
        cache_key = (_digest(key), toolkit)
        if cached := self._auth_configs.get(cache_key):
            return cached
        async with self._lock:
            if cached := self._auth_configs.get(cache_key):
                return cached
            found = await self._request(
                key, "GET", "/api/v3/auth_configs", params={"toolkit_slug": toolkit}
            )
            config_id = None
            for item in found.get("items") or []:
                toolkit_of = (item.get("toolkit") or {}).get("slug")
                if toolkit_of == toolkit and item.get("status", "ENABLED") == "ENABLED":
                    config_id = item.get("id")
                    if item.get("is_composio_managed"):
                        break
            if not config_id:
                made = await self._request(
                    key,
                    "POST",
                    "/api/v3/auth_configs",
                    json_body={
                        "toolkit": {"slug": toolkit},
                        "auth_config": {"type": "use_composio_managed_auth"},
                    },
                )
                config_id = (made.get("auth_config") or {}).get("id") or made.get("id")
            if not isinstance(config_id, str) or not config_id:
                raise ComposioError(f"Composio has no auth config for {toolkit}")
            self._auth_configs[cache_key] = config_id
            return config_id

    async def link(
        self, key: SecretStr, *, auth_config_id: str, user_id: str, callback_url: str
    ) -> Link:
        """Makes a connected account and the link of its sign-in page (valid 10 minutes)."""
        answer = await self._request(
            key,
            "POST",
            "/api/v3.1/connected_accounts/link",
            json_body={
                "auth_config_id": auth_config_id,
                "user_id": user_id,
                "callback_url": callback_url,
            },
        )
        account_id, url = answer.get("connected_account_id"), answer.get("redirect_url")
        if not isinstance(account_id, str) or not isinstance(url, str):
            raise ComposioError("Composio gave no link")
        if not url.startswith("https://"):
            raise ComposioError("Composio gave a link that is not https")
        return Link(account_id=account_id, redirect_url=url)

    async def status(self, key: SecretStr, account_id: str) -> str:
        """The status of a connected account, for example ``ACTIVE``. Nothing else of the
        account leaves this method."""
        answer = await self._request(key, "GET", f"/api/v3.1/connected_accounts/{_id(account_id)}")
        status = answer.get("status")
        if not isinstance(status, str):
            raise ComposioError("Composio gave no status")
        return status.upper()

    async def delete(self, key: SecretStr, account_id: str) -> None:
        """Deletes a connected account. An account that is gone already is not an error."""
        try:
            await self._request(key, "DELETE", f"/api/v3.1/connected_accounts/{_id(account_id)}")
        except ComposioError as error:
            if error.status_code != 404:
                raise

    async def execute(
        self,
        key: SecretStr,
        slug: str,
        *,
        account_id: str,
        user_id: str,
        arguments: Mapping[str, Any],
    ) -> Any:
        """Runs a tool. Returns the ``data`` of the answer. A tool that fails raises."""
        answer = await self._request(
            key,
            "POST",
            f"/api/v3.1/tools/execute/{_id(slug)}",
            json_body={
                "connected_account_id": account_id,
                "user_id": user_id,
                "arguments": dict(arguments),
            },
        )
        if answer.get("successful") is False or answer.get("error"):
            error = answer.get("error") or "the tool failed"
            raise ComposioError(_cut(str(error)))
        return answer.get("data")


def _id(value: str) -> str:
    """An id in a path: letters, digits, ``_``, and ``-`` only."""
    if not value or not all(c.isalnum() or c in "_-" for c in value) or len(value) > 100:
        raise ComposioError("not a valid Composio id")
    return value


def _digest(key: SecretStr) -> str:
    return hashlib.sha256(key.get_secret_value().encode()).hexdigest()[:16]


def _cut(text: str) -> str:
    text = redact(" ".join(text.split()))
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 3] + "..."


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return _cut(response.text or "no message")
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return _cut(str(error.get("message") or error.get("code") or "no message"))
    if isinstance(error, str):
        return _cut(error)
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        return _cut(body["message"])
    return _cut(json.dumps(body)[:MAX_ERROR_CHARS])
