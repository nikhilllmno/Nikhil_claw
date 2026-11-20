"""The ``http_api`` driver: a connector that calls one HTTPS API with a credential from the
connect form (docs/architecture.md, Section 10.3).

The manifest fixes the host, the auth header, the test request, and the request of each tool.
The agent gives only the arguments: a path argument is quoted into one path segment, so the
agent cannot change the host or leave the path. The credential comes from the vault entry
``connection:<id>`` for each call and goes only into the auth header.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, Final, Protocol
from urllib.parse import quote

import httpx

from sama.connectors.manifest import HttpApi, HttpRequest, Manifest, ToolSpec, path_params
from sama.vault.redact import redact

log: Final = logging.getLogger("sama.connectors.http_api")

TIMEOUT_S: Final = 30.0
MAX_ANSWER_BYTES: Final = 2_000_000
MAX_ERROR_CHARS: Final = 300


class DriverError(Exception):
    """A call of the API failed. The message is safe to show: it holds no secret."""


class FormDriver(Protocol):
    """Tests the form values of a connection and runs its tools. The form values hold the
    secret fields; a driver never returns or logs them."""

    async def test(self, manifest: Manifest, values: Mapping[str, str]) -> None:
        """Raises ``DriverError`` if the values do not work."""
        ...

    async def call(
        self, manifest: Manifest, tool: ToolSpec, values: Mapping[str, str], arguments: Any
    ) -> str: ...


class HttpApiDriver:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def test(self, manifest: Manifest, values: Mapping[str, str]) -> None:
        await self._send(_http(manifest), manifest.http.test if manifest.http else None, values, {})

    async def call(
        self, manifest: Manifest, tool: ToolSpec, values: Mapping[str, str], arguments: Any
    ) -> str:
        return await self._send(_http(manifest), tool.request, values, dict(arguments or {}))

    async def _send(
        self,
        http: HttpApi,
        request: HttpRequest | None,
        values: Mapping[str, str],
        arguments: dict[str, Any],
    ) -> str:
        if request is None:
            raise DriverError("the connector has no request for this")
        path = request.path
        for name in path_params(path):
            value = arguments.pop(name, None)
            if not isinstance(value, str | int) or not str(value):
                raise DriverError(f"missing argument: {name}")
            if str(value) in (".", ".."):
                raise DriverError(f"not a valid value for {name}")
            path = path.replace("{" + name + "}", quote(str(value), safe=""))
        credential = values.get(http.auth.field)
        if not credential:
            raise DriverError("the connection has no credential")
        headers = {
            **dict(http.headers),
            http.auth.header: http.auth.prefix + credential,
            "Accept": "application/json",
        }
        body = None if request.method in ("GET", "DELETE") else arguments
        params = (
            {k: _param(v) for k, v in arguments.items()}
            if request.method in ("GET", "DELETE")
            else None
        )
        try:
            response = await self._http.request(
                request.method, http.base_url + path, headers=headers, json=body, params=params
            )
        except httpx.HTTPError as error:
            raise DriverError(f"the API cannot be reached: {type(error).__name__}") from None
        text = response.content[:MAX_ANSWER_BYTES].decode("utf-8", errors="replace")
        if response.status_code >= 400:
            raise DriverError(f"the API answered {response.status_code}: {_cut(text)}")
        return text


def _http(manifest: Manifest) -> HttpApi:
    if manifest.http is None:
        raise DriverError(f"{manifest.name} is not an http_api connector")
    return manifest.http


def _param(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str | int | float):
        return str(value)
    return json.dumps(value, separators=(",", ":"))


def _cut(text: str) -> str:
    text = redact(" ".join(text.split()))
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 3] + "..."
