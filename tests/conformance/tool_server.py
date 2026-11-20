"""A small MCP tool server for the conformance suite (streamable HTTP, like the sama server).

It has two tools:

- ``lookup``: returns a value that only this server knows. The gate allows it.
- ``send_note``: a write. The gate asks for approval. The server records each call, so a test
  can check that a rejected call did not run.

The server checks the bearer token, like the run token of the real sama MCP server.
It accepts a static test token and the run tokens that it signed.

It also answers ``POST /session-items`` for the OpenAI runner, with the items in memory
(``session_items``). As in the real tool server, the session comes from the run token.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from sama.runtime.contract import Audience, RunScope
from sama.runtime.session_items import (
    SESSION_ITEMS_PATH,
    MemorySessionItems,
    SessionItemsError,
    parse_session_items,
)
from sama.tenancy import InvalidRunTokenError, RunTokenSigner

SERVER_NAME = "sama"
LOOKUP = "lookup"
SEND_NOTE = "send_note"


def tool_id(name: str) -> str:
    """The name that the Claude CLI gives an MCP tool."""
    return f"mcp__{SERVER_NAME}__{name}"


@dataclass
class ToolServer:
    """``token`` is a static bearer for runtimes in the test process. ``run_tokens`` also
    accepts the run tokens that it signed, for runners in a sandbox."""

    token: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    run_tokens: RunTokenSigner = field(
        default_factory=lambda: RunTokenSigner(secrets.token_bytes(32))
    )
    secret: str = field(default_factory=lambda: f"amber-{secrets.token_hex(4)}")
    notes: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    session_items: MemorySessionItems = field(default_factory=MemorySessionItems)

    def app(self) -> ASGIApp:
        server: MCPServer = MCPServer(SERVER_NAME)

        @server.tool(name=LOOKUP, description="Look up the value of a key.")
        def lookup(key: str) -> str:
            self.calls.append(LOOKUP)
            return f"{key}={self.secret}"

        @server.tool(name=SEND_NOTE, description="Send a note to the team.")
        def send_note(text: str) -> str:
            self.calls.append(SEND_NOTE)
            self.notes.append(text)
            return "sent"

        inner = server.streamable_http_app(stateless_http=True, json_response=True)
        return _BearerAuth(inner, self.accepts, self._session_items)

    async def _session_items(self, request: Request, bearer: str) -> JSONResponse:
        try:
            claims = self.run_tokens.verify(bearer)
        except InvalidRunTokenError:
            return JSONResponse({"error": "bad run token"}, status_code=401)
        try:
            body = parse_session_items(await request.body())
        except ValueError:
            return JSONResponse({"error": "the request is not valid"}, status_code=422)
        scope = RunScope(
            org_id=claims.org_id,
            workspace_id=claims.workspace_id,
            session_id=claims.session_id,
            run_id=claims.run_id,
            audience=Audience.PRIVATE if claims.user_id else Audience.SHARED,
            user_id=claims.user_id,
        )
        try:
            answer = await self.session_items.call(scope, body)
        except SessionItemsError as error:
            return JSONResponse({"error": str(error)}, status_code=409)
        return JSONResponse(answer.model_dump(mode="json"))

    def accepts(self, bearer: str) -> bool:
        if secrets.compare_digest(bearer, self.token):
            return True
        try:
            self.run_tokens.verify(bearer)
        except InvalidRunTokenError:
            return False
        return True


@dataclass
class _BearerAuth:
    app: Starlette
    accepts: Callable[[str], bool]
    session_items: Callable[[Request, str], Awaitable[JSONResponse]]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request = Request(scope, receive)
            header = request.headers.get("authorization", "")
            bearer = header.removeprefix("Bearer ")
            if bearer == header or not self.accepts(bearer):
                response = JSONResponse({"error": "bad run token"}, status_code=401)
                await response(scope, receive, send)
                return
            if request.url.path == SESSION_ITEMS_PATH and request.method == "POST":
                response = await self.session_items(request, bearer)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
