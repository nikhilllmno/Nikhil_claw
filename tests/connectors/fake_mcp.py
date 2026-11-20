"""A fake remote MCP server with its OAuth authorization server, for the tests of
``sama.connectors.mcp_remote``, ``oauth``, ``mcp_driver``, and the connection service
(docs/architecture.md, Section 10.3).

The server is at ``https://mcp.remote.test/mcp``. The authorization server is on the same host.
``OutboundClient`` sends a request to the IP address and names the host in ``Host``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from sama.connectors.mcp_driver import McpDriver
from sama.connectors.mcp_remote import McpClient
from sama.connectors.oauth import OAuthFlow
from sama.tools.outbound import IPAddress, OutboundClient
from sama.vault.store import Vault
from tests.support import wire_transport

HOST = "mcp.remote.test"
URL = f"https://{HOST}/mcp"
GOOD_TOKEN = "mcp-token-good-123"
METADATA = f"https://{HOST}/.well-known/oauth-protected-resource/mcp"


def tool(
    name: str, *, read_only: bool | None = None, destructive: bool | None = None
) -> dict[str, Any]:
    hints: dict[str, Any] = {}
    if read_only is not None:
        hints["readOnlyHint"] = read_only
    if destructive is not None:
        hints["destructiveHint"] = destructive
    return {
        "name": name,
        "description": f"The {name} tool.",
        "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
        "annotations": hints,
    }


class FakeMcp:
    """The MCP server and the authorization server. ``auth`` is the credential check:
    ``none`` (no check), ``bearer`` (``Authorization: Bearer <token>``), ``header``
    (``X-Api-Key: <token>``), or ``oauth`` (a bearer token that ``/token`` gave)."""

    def __init__(self, auth: str = "bearer", *, stream: bool = False) -> None:
        self.auth = auth
        self.stream = stream
        self.mcp_down = False
        """The MCP endpoint answers 503; the OAuth endpoints still work."""
        self.crlf = False
        """The event stream ends its lines with CRLF."""
        self.stay_open = False
        """The event stream stays open after the answer, as a server can do."""
        self.tools: list[dict[str, Any]] = [
            tool("list_items", read_only=True),
            tool("create_item", destructive=False),
            tool("drop_item"),
        ]
        self.page_size: int | None = None
        self.tokens = {GOOD_TOKEN}
        self.sessions: set[str] = set()
        self.use_sessions = True
        self.opened = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.down = False
        self.seen: list[httpx.Request] = []
        # OAuth
        self.registered: list[dict[str, Any]] = []
        self.client_secret: str | None = None
        self.codes: dict[str, str] = {}
        """code -> PKCE challenge"""
        self.refresh_tokens: set[str] = set()
        self.issued = 0
        self.expires_in: int | None = 3600
        self.token_requests: list[dict[str, str]] = []

    # -- the user -------------------------------------------------------------------------

    def approve(self, authorization_url: str) -> tuple[str, str]:
        """The user signs in: returns ``(code, state)`` of the redirect."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(authorization_url).query).items()}
        if query.get("code_challenge_method") != "S256":
            raise AssertionError("no PKCE")
        code = f"code-{len(self.codes) + 1}"
        self.codes[code] = query["code_challenge"]
        return code, query["state"]

    # -- HTTP -----------------------------------------------------------------------------

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if self.down:
            return httpx.Response(503)
        path = request.url.path
        if path == "/mcp" and request.method == "POST":
            if self.mcp_down:
                return httpx.Response(503)
            return self._mcp(request)
        if path == "/.well-known/oauth-protected-resource/mcp" and self.auth == "oauth":
            return httpx.Response(
                200, json={"resource": URL, "authorization_servers": [f"https://{HOST}"]}
            )
        if path == "/.well-known/oauth-authorization-server" and self.auth == "oauth":
            return httpx.Response(
                200,
                json={
                    "issuer": f"https://{HOST}",
                    "authorization_endpoint": f"https://{HOST}/authorize",
                    "token_endpoint": f"https://{HOST}/token",
                    "registration_endpoint": f"https://{HOST}/register",
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if path == "/register" and request.method == "POST":
            body = json.loads(request.content)
            self.registered.append(body)
            answer: dict[str, Any] = {"client_id": f"client-{len(self.registered)}"}
            if self.client_secret:
                answer["client_secret"] = self.client_secret
                answer["token_endpoint_auth_method"] = "client_secret_post"
            return httpx.Response(201, json=answer)
        if path == "/token" and request.method == "POST":
            return self._token(request)
        return httpx.Response(404)

    def _credential_ok(self, request: httpx.Request) -> bool:
        if self.auth == "none":
            return True
        if self.auth == "header":
            return request.headers.get("x-api-key") in self.tokens
        value = request.headers.get("authorization", "")
        return value.startswith("Bearer ") and value[7:] in self.tokens

    def _mcp(self, request: httpx.Request) -> httpx.Response:
        if not self._credential_ok(request):
            return httpx.Response(
                401,
                headers={"www-authenticate": f'Bearer resource_metadata="{METADATA}"'},
                json={"error": "invalid_token"},
            )
        body = json.loads(request.content)
        method = body.get("method")
        if "id" not in body:
            return httpx.Response(202)
        if method == "initialize":
            self.opened += 1
            headers = {}
            if self.use_sessions:
                session = f"s-{self.opened}"
                self.sessions.add(session)
                headers["mcp-session-id"] = session
            opened: dict[str, Any] = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "fake", "version": "1"},
            }
            return self._answer(body["id"], opened, headers)
        if self.use_sessions:
            session = request.headers.get("mcp-session-id")
            if session not in self.sessions:
                return httpx.Response(404 if session else 400)
            if request.headers.get("mcp-protocol-version") is None:
                return httpx.Response(400)
        if method == "tools/list":
            start = int(body["params"].get("cursor") or 0)
            size = self.page_size or len(self.tools)
            page = self.tools[start : start + size]
            result: dict[str, Any] = {"tools": page}
            if start + size < len(self.tools):
                result["nextCursor"] = str(start + size)
            return self._answer(body["id"], result)
        if method == "tools/call":
            name = body["params"]["name"]
            arguments = body["params"].get("arguments") or {}
            self.calls.append((name, arguments))
            if name not in {t["name"] for t in self.tools}:
                return self._answer(
                    body["id"],
                    {"content": [{"type": "text", "text": f"no tool {name}"}], "isError": True},
                )
            text = f"ran {name} {json.dumps(arguments, sort_keys=True)}"
            return self._answer(body["id"], {"content": [{"type": "text", "text": text}]})
        return self._answer(body["id"], None, error={"code": -32601, "message": "no method"})

    def _answer(
        self,
        request_id: int,
        result: Any,
        headers: dict[str, str] | None = None,
        *,
        error: dict[str, Any] | None = None,
    ) -> httpx.Response:
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result
        if not self.stream:
            return httpx.Response(200, headers=headers, json=message)
        # A request of the server comes first: the client does not answer it.
        ping = {"jsonrpc": "2.0", "id": "srv-1", "method": "ping"}
        text = f"event: message\ndata: {json.dumps(ping)}\n\n"
        text += f"event: message\ndata: {json.dumps(message)}\n\n"
        if self.crlf:
            text = text.replace("\n", "\r\n")
        sent = {**(headers or {}), "content-type": "text/event-stream"}
        if not self.stay_open:
            return httpx.Response(200, headers=sent, content=text.encode())

        async def events() -> AsyncIterator[bytes]:
            yield text.encode()
            await asyncio.Event().wait()  # never ends

        return httpx.Response(200, headers=sent, content=events())

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_requests.append(form)
        if form.get("resource") != URL:
            return httpx.Response(400, json={"error": "invalid_target"})
        if self.client_secret and form.get("client_secret") != self.client_secret:
            return httpx.Response(401, json={"error": "invalid_client"})
        grant = form.get("grant_type")
        if grant == "authorization_code":
            challenge = self.codes.pop(form.get("code", ""), None)
            verifier = form.get("code_verifier", "")
            digest = hashlib.sha256(verifier.encode()).digest()
            if challenge is None or challenge != base64.urlsafe_b64encode(digest).rstrip(
                b"="
            ).decode("ascii"):
                return httpx.Response(400, json={"error": "invalid_grant"})
        elif grant == "refresh_token":
            old = form.get("refresh_token", "")
            if old not in self.refresh_tokens:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.refresh_tokens.discard(old)
        else:
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        self.issued += 1
        access = f"access-{self.issued}"
        refresh = f"refresh-{self.issued}"
        self.tokens = {access}
        self.refresh_tokens.add(refresh)
        answer: dict[str, Any] = {
            "access_token": access,
            "token_type": "Bearer",
            "refresh_token": refresh,
        }
        if self.expires_in is not None:
            answer["expires_in"] = self.expires_in
        return httpx.Response(200, json=answer)


async def public(host: str, port: int) -> Sequence[IPAddress]:
    if host == "private.remote.test":
        return [ipaddress.ip_address("10.0.0.7")]
    return [ipaddress.ip_address("93.184.215.14")]


def outbound(fake: FakeMcp) -> OutboundClient:
    from sama.connectors.mcp_remote import POLICY

    return OutboundClient(POLICY, resolver=public, transport=wire_transport(fake))


def client(fake: FakeMcp) -> McpClient:
    return McpClient(outbound(fake))


def driver(fake: FakeMcp, vault: Vault | None) -> McpDriver:
    return McpDriver(vault, client=client(fake), oauth=OAuthFlow(outbound(fake)))
