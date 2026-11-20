"""The driver of a custom HTTP API (docs/architecture.md, Section 10.3.3).

A workspace admin imports the operations (``openapi.py``). Each call sends the request of
one operation:

- a path argument is quoted into one path segment, so the agent cannot leave the base URL;
- a query argument goes into the query, and ``body`` is the JSON body;
- an argument that the operation does not have is refused;
- the auth comes from the vault entry of the connection for each call: an API key (a header
  or a query parameter), a bearer token, basic auth, or an OAuth 2 token from the client
  credentials grant. The driver keeps an OAuth 2 token in memory until it expires.

Every request goes through ``OutboundClient`` (Section 16.4): public addresses only, no
redirects, and a size limit while the body is read. An error message never holds a secret.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote

import httpx

from sama.connectors.errors import short_error
from sama.connectors.http_api import (
    POLICY,
    UNAVAILABLE_CODES,
    DriverError,
    DriverUnavailableError,
)
from sama.connectors.manifest import ApiSpec
from sama.connectors.openapi import BODY, ApiOperation
from sama.tools.outbound import (
    BlockedUrlError,
    FetchError,
    FetchResult,
    OutboundClient,
    Resolver,
    UrlPolicy,
    system_resolver,
)

TOKEN_MARGIN_S: Final = 60.0
DEFAULT_TOKEN_S: Final = 3600.0
DOCUMENT_POLICY: Final = UrlPolicy(
    max_redirects=3, max_bytes=2_000_000, hop_timeout_s=20, total_timeout_s=30
)


@dataclass(frozen=True, slots=True)
class _Token:
    value: str
    expires: float
    key: str


class ApiDriver:
    """Sends the requests of custom APIs."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = system_resolver,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = OutboundClient(POLICY, resolver=resolver, transport=transport)
        self._documents = OutboundClient(DOCUMENT_POLICY, resolver=resolver, transport=transport)
        self._clock = clock
        self._tokens: dict[uuid.UUID, _Token] = {}
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._documents.aclose()

    def forget(self, connection_id: uuid.UUID) -> None:
        """Drops the OAuth 2 token of the connection (new values, or a delete)."""
        self._tokens.pop(connection_id, None)
        self._locks.pop(connection_id, None)

    async def check_url(self, url: str) -> None:
        """Raises ``BlockedUrlError`` or ``FetchError`` if the policy refuses the URL."""
        await self._http.check(url)

    async def fetch_document(self, url: str) -> str:
        """GETs an OpenAPI document. Raises ``DriverError``."""
        try:
            answer = await self._documents.get(url)
        except BlockedUrlError as error:
            raise DriverError(f"sama cannot use this URL: {error}") from None
        except FetchError as error:
            raise DriverError(f"the document cannot be read: {error}") from None
        if answer.status_code != 200:
            raise DriverError(f"the document URL answered {answer.status_code}")
        if answer.truncated:
            raise DriverError("the document is larger than 2 MB")
        return answer.content.decode("utf-8", errors="replace")

    async def test(
        self,
        connection_id: uuid.UUID,
        api: ApiSpec,
        operations: Sequence[ApiOperation],
        values: Mapping[str, str],
    ) -> None:
        """The health check: an OAuth 2 token, then the test operation (else the first
        ``GET`` without required arguments). Without such an operation, only the URL."""
        if api.auth.type == "oauth2":
            self.forget(connection_id)
            await self._token(connection_id, api, values)
        operation = _test_operation(api, operations)
        if operation is None:
            try:
                await self.check_url(api.base_url)
            except BlockedUrlError as error:
                raise DriverError(f"the API address is not allowed: {error}") from None
            except FetchError as error:
                raise DriverUnavailableError(f"the API cannot be reached: {error}") from None
            return
        await self.call(connection_id, api, operation, values, {})

    async def call(
        self,
        connection_id: uuid.UUID,
        api: ApiSpec,
        operation: ApiOperation,
        values: Mapping[str, str],
        arguments: Mapping[str, Any],
    ) -> str:
        url, params, body = _request(api, operation, arguments)
        answer = await self._send(connection_id, api, operation, values, url, params, body)
        if answer.status_code == 401 and api.auth.type == "oauth2":
            # The token can be revoked before it expires. A 401 did not run the request.
            self.forget(connection_id)
            answer = await self._send(connection_id, api, operation, values, url, params, body)
        text = answer.content.decode("utf-8", errors="replace")
        if answer.status_code in UNAVAILABLE_CODES or answer.status_code >= 500:
            raise DriverUnavailableError(
                f"the API answered {answer.status_code}: {short_error(text)}"
            )
        if answer.status_code >= 400:
            raise DriverError(f"the API answered {answer.status_code}: {short_error(text)}")
        if answer.truncated:
            text += "\n[the answer was larger than 2 MB and is cut]"
        return text or f"(the API answered {answer.status_code} with no body)"

    async def _send(
        self,
        connection_id: uuid.UUID,
        api: ApiSpec,
        operation: ApiOperation,
        values: Mapping[str, str],
        url: str,
        params: dict[str, str],
        body: Any,
    ) -> FetchResult:
        headers = {"Accept": "application/json"}
        query = dict(params)
        auth = api.auth
        if auth.type == "api_key":
            key = _value(values, "api_key")
            if auth.location == "header":
                headers[str(auth.name)] = key
            else:
                query[str(auth.name)] = key
        elif auth.type == "bearer":
            headers["Authorization"] = f"Bearer {_value(values, 'token')}"
        elif auth.type == "basic":
            pair = f"{_value(values, 'username')}:{_value(values, 'password')}"
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        elif auth.type == "oauth2":
            token = await self._token(connection_id, api, values)
            headers["Authorization"] = f"Bearer {token}"
        try:
            return await self._http.request(
                operation.method,
                url,
                headers=headers,
                params=query or None,
                json=body,
            )
        except BlockedUrlError as error:
            raise DriverError(f"the API address is not allowed: {error}") from None
        except FetchError as error:
            raise DriverUnavailableError(f"the API cannot be reached: {error}") from None

    async def _token(
        self, connection_id: uuid.UUID, api: ApiSpec, values: Mapping[str, str]
    ) -> str:
        """An OAuth 2 token from the client credentials grant, from memory if it is valid."""
        client_id, secret = _value(values, "client_id"), _value(values, "client_secret")
        key = hashlib.sha256(
            json.dumps([api.auth.token_url, api.auth.scope, client_id, secret]).encode()
        ).hexdigest()
        lock = self._locks.setdefault(connection_id, asyncio.Lock())
        async with lock:
            cached = self._tokens.get(connection_id)
            if cached is not None and cached.key == key and cached.expires > self._clock():
                return cached.value
            form = {"grant_type": "client_credentials"}
            if api.auth.scope:
                form["scope"] = api.auth.scope
            headers = {"Accept": "application/json"}
            if api.auth.client_auth == "basic":
                pair = f"{quote(client_id, safe='')}:{quote(secret, safe='')}"
                headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
            else:
                form.update(client_id=client_id, client_secret=secret)
            try:
                answer = await self._http.request(
                    "POST", str(api.auth.token_url), headers=headers, form=form
                )
            except BlockedUrlError as error:
                raise DriverError(f"the token URL is not allowed: {error}") from None
            except FetchError as error:
                raise DriverUnavailableError(f"the token URL cannot be reached: {error}") from None
            if answer.status_code in UNAVAILABLE_CODES or answer.status_code >= 500:
                raise DriverUnavailableError(f"the token URL answered {answer.status_code}")
            try:
                data = json.loads(answer.content)
            except ValueError:
                data = None
            token = data.get("access_token") if isinstance(data, dict) else None
            if answer.status_code >= 400 or not isinstance(token, str) or not token:
                # The OAuth error code is safe to show. The body can echo the client id only.
                code = data.get("error") if isinstance(data, dict) else None
                reason = f" ({str(code)[:60]})" if isinstance(code, str) else ""
                raise DriverError(
                    f"the token URL refused the client credentials: {answer.status_code}{reason}"
                )
            lifetime = data.get("expires_in") if isinstance(data, dict) else None
            seconds = float(lifetime) if isinstance(lifetime, int | float) else DEFAULT_TOKEN_S
            self._tokens[connection_id] = _Token(
                token, self._clock() + max(0.0, seconds - TOKEN_MARGIN_S), key
            )
            return token


def _value(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if not value:
        raise DriverError("the connection has no credential")
    return value


def _test_operation(api: ApiSpec, operations: Sequence[ApiOperation]) -> ApiOperation | None:
    if api.test is not None:
        found = next((op for op in operations if op.tool.name == api.test), None)
        if found is None:
            raise DriverError(f"the API has no operation {api.test} for the test")
        return found
    for operation in operations:
        required = operation.tool.input_schema.get("required") or ()
        if operation.method == "GET" and not required:
            return operation
    return None


def _request(
    api: ApiSpec, operation: ApiOperation, arguments: Mapping[str, Any]
) -> tuple[str, dict[str, str], Any]:
    """The URL, the query, and the JSON body of one call."""
    known = {name for name, _ in operation.params} | ({BODY} if operation.body else set())
    if unknown := sorted(set(arguments) - known):
        raise DriverError(f"unknown arguments: {', '.join(unknown)[:200]}")
    path = operation.path
    query: dict[str, str] = {}
    for name, where in operation.params:
        value = arguments.get(name)
        if where == "path":
            if (
                not isinstance(value, str | int | float)
                or isinstance(value, bool)
                or not str(value)
            ):
                raise DriverError(f"missing argument: {name}")
            if str(value) in (".", ".."):
                raise DriverError(f"not a valid value for {name}")
            path = path.replace("{" + name + "}", quote(str(value), safe=""))
        elif value is not None:
            query[name] = _param(value)
    body = arguments.get(BODY) if operation.body else None
    return api.base_url.rstrip("/") + path, query, body


def _param(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str | int | float):
        return str(value)
    return json.dumps(value, separators=(",", ":"))
