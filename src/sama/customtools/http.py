"""The runner of a custom HTTP tool (docs/architecture.md, Section 10.8).

The definition fixes the method, the origin of the URL, the headers, and the body. The agent
gives only the arguments, which fill the placeholders (``spec.render_*``). The credential comes
from the vault for each call and goes only into the auth header or the auth query parameter. It
goes only to the origin that it was saved for: a version with another origin gets no
credential.

Every request goes through ``OutboundClient`` (Section 16.4): public addresses only, no
redirects, and a size limit while the body is read. An error text never holds a secret.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote, quote_plus

import httpx

from sama.connectors.errors import short_error
from sama.customtools.spec import (
    ArgumentError,
    HttpConfig,
    full_url,
    map_response,
    origin_of,
    render_body,
    render_header,
    render_url,
)
from sama.tools.outbound import (
    BlockedUrlError,
    FetchError,
    OutboundClient,
    Resolver,
    UrlPolicy,
    system_resolver,
)
from sama.vault.redact import default_redactor, redact

TIMEOUT_S: Final = 30.0
MAX_ANSWER_BYTES: Final = 2_000_000
POLICY: Final = UrlPolicy(
    max_redirects=0,
    max_bytes=MAX_ANSWER_BYTES,
    hop_timeout_s=TIMEOUT_S,
    total_timeout_s=TIMEOUT_S,
)


class ToolCallError(Exception):
    """A call of a custom tool failed. The message is safe to show: it holds no secret."""


@dataclass(frozen=True, slots=True)
class Credential:
    """The vault value of an HTTP tool. ``origin`` is the only origin that gets it."""

    origin: str
    value: str | None = None
    username: str | None = None
    password: str | None = None

    @classmethod
    def of(cls, data: Mapping[str, Any]) -> Credential:
        def text(key: str) -> str | None:
            value = data.get(key)
            return value if isinstance(value, str) and value else None

        return cls(
            origin=str(data.get("origin", "")),
            value=text("value"),
            username=text("username"),
            password=text("password"),
        )

    def stored(self) -> dict[str, str]:
        out = {"origin": self.origin}
        for key in ("value", "username", "password"):
            if (found := getattr(self, key)) is not None:
                out[key] = found
        return out


@dataclass(frozen=True, slots=True)
class HttpAnswer:
    status: int
    text: str


class HttpToolRunner:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = system_resolver,
    ) -> None:
        self._http = OutboundClient(POLICY, resolver=resolver, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def check_url(self, url: str) -> None:
        """Raises ``ToolCallError`` when the outbound policy refuses the origin of the URL."""
        try:
            await self._http.check(origin_of(url) + "/")
        except BlockedUrlError as error:
            raise ToolCallError(f"sama cannot use this URL: {error}") from None
        except FetchError as error:
            raise ToolCallError(f"sama cannot reach this URL: {error}") from None

    async def call(
        self,
        http: HttpConfig,
        arguments: Mapping[str, Any],
        credential: Credential | None,
    ) -> HttpAnswer:
        """Sends the request. A ``2xx`` answer gives the mapped text. Another status raises
        ``ToolCallError`` with the status and the start of the answer."""
        try:
            base, pairs = render_url(http.url, arguments)
            headers = {
                name: render_header(value, arguments) for name, value in http.headers.items()
            }
            body = None if http.body is None else render_body(http.body, arguments)
        except ArgumentError as error:
            raise ToolCallError(str(error)) from None
        headers.setdefault("Accept", "application/json")
        auth = http.auth
        if auth.type != "none":
            if credential is None:
                raise ToolCallError("the tool has no credential. Its owner must set it")
            if credential.origin != origin_of(http.url):
                raise ToolCallError(
                    "the credential was saved for another address. Its owner must set it again"
                )
            if auth.type == "bearer" and credential.value:
                headers["Authorization"] = "Bearer " + credential.value
            elif auth.type == "api_key" and credential.value and auth.header is not None:
                headers[auth.header] = credential.value
            elif auth.type == "api_key" and credential.value and auth.query is not None:
                # An answer can echo the URL: the redactor must know the encoded forms too.
                default_redactor().add(
                    quote(credential.value, safe=""), quote_plus(credential.value)
                )
                pairs.append((auth.query, credential.value))
            elif auth.type == "basic" and credential.username and credential.password:
                token = base64.b64encode(
                    f"{credential.username}:{credential.password}".encode()
                ).decode()
                # An answer can echo the header: the redactor must know the encoded form too.
                default_redactor().add(token)
                headers["Authorization"] = "Basic " + token
            else:
                raise ToolCallError("the credential of the tool is not complete")
        try:
            answer = await self._http.request(
                http.method, full_url(base, pairs), headers=headers, json=body
            )
        except BlockedUrlError as error:
            raise ToolCallError(f"the address is not allowed: {error}") from None
        except FetchError as error:
            raise ToolCallError(f"the API cannot be reached: {error}") from None
        # Redact before the mapping cuts the text, so a cut cannot leave part of a secret.
        text = redact(answer.content.decode("utf-8", errors="replace"))
        if not 200 <= answer.status_code < 300:
            raise ToolCallError(f"the API answered {answer.status_code}: {short_error(text)}")
        if answer.truncated:
            text += "\n[The answer is larger than 2 MB. Only the start is here.]"
        if http.method == "HEAD" or not text:
            return HttpAnswer(answer.status_code, f"HTTP {answer.status_code}, no body")
        try:
            return HttpAnswer(answer.status_code, map_response(text, http.response))
        except ArgumentError as error:
            raise ToolCallError(str(error)) from None
