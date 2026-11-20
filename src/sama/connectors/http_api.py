"""The ``http_api`` driver: a connector that calls one HTTPS API with a credential from the
connect form (docs/architecture.md, Section 10.3).

The manifest fixes the host, the auth header, the test request, and the request of each tool.
The agent gives only the arguments: a path argument is quoted into one path segment, so the
agent cannot change the host or leave the path. The credential comes from the vault entry
``connection:<id>`` for each call and goes only into the auth header.

Every request goes through ``OutboundClient`` (Section 16.4): public addresses only, no
redirects, and a size limit while the body is read.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final, Protocol
from urllib.parse import quote

import httpx

from sama.connectors.errors import short_error
from sama.connectors.manifest import HttpApi, HttpRequest, Manifest, ToolSpec, path_params
from sama.tools.outbound import (
    BlockedUrlError,
    FetchError,
    OutboundClient,
    Resolver,
    UrlPolicy,
    system_resolver,
)

TIMEOUT_S: Final = 30.0
MAX_ANSWER_BYTES: Final = 2_000_000
UNAVAILABLE_CODES: Final = frozenset({408, 429, 500, 502, 503, 504})
POLICY: Final = UrlPolicy(
    max_redirects=0,
    max_bytes=MAX_ANSWER_BYTES,
    hop_timeout_s=TIMEOUT_S,
    total_timeout_s=TIMEOUT_S,
)


class DriverError(Exception):
    """A call of the API failed. The message is safe to show: it holds no secret."""


class DriverUnavailableError(DriverError):
    """The API cannot be reached now (a network error, a time limit, or a 5xx or 429 answer).
    The credential can still be good, so a check keeps the state of the connection."""


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
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = system_resolver,
    ) -> None:
        self._http = OutboundClient(POLICY, resolver=resolver, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def test(self, manifest: Manifest, values: Mapping[str, str]) -> None:
        http = _http(manifest)
        await self._send(http, http.test, values, {})

    async def call(
        self, manifest: Manifest, tool: ToolSpec, values: Mapping[str, str], arguments: Any
    ) -> str:
        if tool.request is None:
            raise DriverError("the connector has no request for this")
        return await self._send(_http(manifest), tool.request, values, dict(arguments or {}))

    async def _send(
        self,
        http: HttpApi,
        request: HttpRequest,
        values: Mapping[str, str],
        arguments: dict[str, Any],
    ) -> str:
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
        in_query = request.method in ("GET", "DELETE")
        try:
            answer = await self._http.request(
                request.method,
                http.base_url + path,
                headers=headers,
                params={k: _param(v) for k, v in arguments.items()} if in_query else None,
                json=None if in_query else arguments,
            )
        except BlockedUrlError as error:
            raise DriverError(f"the API address is not allowed: {error}") from None
        except FetchError as error:
            raise DriverUnavailableError(f"the API cannot be reached: {error}") from None
        text = answer.content.decode("utf-8", errors="replace")
        if answer.status_code in UNAVAILABLE_CODES:
            raise DriverUnavailableError(
                f"the API answered {answer.status_code}: {short_error(text)}"
            )
        if answer.status_code >= 400:
            raise DriverError(f"the API answered {answer.status_code}: {short_error(text)}")
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
