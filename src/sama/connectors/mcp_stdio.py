"""An MCP client over stdio, for a server in a connector host (docs/architecture.md,
Section 10.3.2).

The messages are JSON-RPC, one JSON object on each line (the MCP stdio transport). The
process is a ``SandboxProcess``: the bridge of the host, whose stdin and stdout are the
stdin and stdout of the server.

The error messages never hold the stderr of the server, because it can show a secret.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from collections.abc import Mapping
from typing import Any, Final

from sama.connectors.host_bridge import MEMORY_EXIT, SETUP_EXIT
from sama.connectors.mcp_remote import (
    CLIENT_INFO,
    MAX_PAGES,
    MAX_TOOLS,
    PROTOCOL_VERSION,
    SUPPORTED_VERSIONS,
    McpError,
    McpUnavailableError,
    RemoteTool,
    parse_tool,
)
from sama.sandbox.base import SandboxError, SandboxProcess

log: Final = logging.getLogger("sama.connectors.stdio")

START_TIMEOUT_S: Final = 240.0
"""The first start downloads and installs the package, so it can take some minutes."""
CALL_TIMEOUT_S: Final = 60.0
MAX_LINE_CHARS: Final = 4_000_000
METHOD_NOT_FOUND: Final = -32601


class McpProcessEndedError(McpUnavailableError):
    """The server stopped. The next call starts it again."""

    def __init__(self, code: int | None) -> None:
        self.code = code
        if code == MEMORY_EXIT:
            text = (
                "the server used more than its memory limit; sama starts it again on the next call"
            )
        elif code == SETUP_EXIT:
            text = "the server did not install or start"
        else:
            text = f"the server stopped (exit code {code}); sama starts it again on the next call"
        super().__init__(text)


class McpNotSentError(McpUnavailableError):
    """The request did not reach the server, so it is safe to send it again."""


class StdioMcpClient:
    """One MCP session with one server process. Requests can run at the same time."""

    def __init__(self, process: SandboxProcess) -> None:
        self._process = process
        self._prefix = secrets.token_hex(4)
        self._next = 0
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._ended: McpProcessEndedError | None = None
        self._reader = asyncio.create_task(self._read())

    @property
    def alive(self) -> bool:
        return self._ended is None and not self._reader.done()

    async def initialize(self, *, timeout_s: float = START_TIMEOUT_S) -> str:
        """``initialize`` and ``notifications/initialized``. Returns the protocol version."""
        params = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }
        result = await self.request("initialize", params, timeout_s=timeout_s)
        version = result.get("protocolVersion")
        if version not in SUPPORTED_VERSIONS:
            raise McpError(f"the server uses an MCP version that sama does not know: {version}")
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return str(version)

    async def list_tools(self) -> list[RemoteTool]:
        """All pages of ``tools/list``, at most ``MAX_PAGES`` pages and ``MAX_TOOLS`` tools."""
        tools: list[RemoteTool] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            result = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            for item in result.get("tools") or []:
                tool = parse_tool(item)
                if tool is not None and len(tools) < MAX_TOOLS:
                    tools.append(tool)
            following = result.get("nextCursor")
            if not isinstance(following, str) or not following or len(tools) >= MAX_TOOLS:
                break
            cursor = following
        return tools

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any], *, timeout_s: float = CALL_TIMEOUT_S
    ) -> dict[str, Any]:
        return await self.request(
            "tools/call", {"name": name, "arguments": dict(arguments)}, timeout_s=timeout_s
        )

    async def request(
        self, method: str, params: Mapping[str, Any], *, timeout_s: float = CALL_TIMEOUT_S
    ) -> dict[str, Any]:
        """Sends one request and waits for its answer."""
        if self._ended is not None:
            raise McpNotSentError(str(self._ended))
        self._next += 1
        request_id = f"{self._prefix}-{self._next}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
            )
            answer = await asyncio.wait_for(asyncio.shield(future), timeout=timeout_s)
        except TimeoutError:
            raise McpUnavailableError(f"the server did not answer {method} in time") from None
        finally:
            self._pending.pop(request_id, None)
        error = answer.get("error")
        if error is not None:
            message = error.get("message") if isinstance(error, Mapping) else None
            raise McpError(f"the server refused {method}: {str(message or 'no details')[:200]}")
        result = answer.get("result")
        if not isinstance(result, dict):
            raise McpError("the server sent a result that is not valid")
        return result

    async def close(self) -> None:
        """Stops the process. The pending requests fail."""
        with contextlib.suppress(Exception):
            await self._process.close_stdin()
        with contextlib.suppress(Exception):
            await self._process.kill()
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(self._reader), timeout=5)
        if not self._reader.done():
            self._reader.cancel()
        self._fail(self._ended or McpProcessEndedError(None))

    async def _send(self, message: Mapping[str, Any]) -> None:
        try:
            await self._process.write(json.dumps(message, separators=(",", ":")) + "\n")
        except SandboxError:
            self._ended = self._ended or McpProcessEndedError(None)
            raise McpNotSentError("the server is not running") from None

    async def _read(self) -> None:
        try:
            async for line in self._process.lines():
                await self._handle(line)
        except Exception as error:
            log.info("stdio reader stopped: %s", type(error).__name__)
        code: int | None
        try:
            code = await asyncio.wait_for(self._process.wait(), timeout=10)
        except Exception:
            code = None
        self._fail(McpProcessEndedError(code))

    def _fail(self, error: McpProcessEndedError) -> None:
        self._ended = self._ended or error
        for future in self._pending.values():
            if not future.done():
                future.set_exception(self._ended)
        self._pending.clear()

    async def _handle(self, line: str) -> None:
        line = line.strip()
        if not line or len(line) > MAX_LINE_CHARS:
            return
        try:
            message = json.loads(line)
        except ValueError:
            return  # A server that writes logs to stdout: not MCP, so skip it.
        if not isinstance(message, dict):
            return
        message_id = message.get("id")
        if "method" in message:
            if message_id is None:
                return  # A notification.
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id}
            if message["method"] == "ping":
                reply["result"] = {}
            else:
                reply["error"] = {"code": METHOD_NOT_FOUND, "message": "not supported"}
            with contextlib.suppress(McpUnavailableError):
                await self._send(reply)
            return
        future = self._pending.get(str(message_id)) if isinstance(message_id, str) else None
        if future is not None and not future.done():
            future.set_result(message)
