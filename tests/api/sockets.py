"""A WebSocket client for the API tests that calls the ASGI app itself.

``httpx.ASGITransport`` has no WebSocket support, and the Starlette test client runs the app in
another event loop, which the asyncpg engine of the tests cannot use. This client runs the app
in the event loop of the test and talks to it through two queues.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, MutableMapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

WAIT_S = 10.0


class SocketClosed(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(f"{code} {reason}")
        self.code = code
        self.reason = reason


class Socket:
    def __init__(self) -> None:
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.accepted = False
        self.close_code: int | None = None
        self.close_reason = ""
        self.frames: list[dict[str, Any]] = []
        """Every frame that the client received."""

    async def send(self, frame: dict[str, Any] | str) -> None:
        text = frame if isinstance(frame, str) else json.dumps(frame)
        await self.inbox.put({"type": "websocket.receive", "text": text})

    async def send_bytes(self, data: bytes) -> None:
        await self.inbox.put({"type": "websocket.receive", "bytes": data})

    async def receive(self, wait_s: float = WAIT_S) -> dict[str, Any]:
        """The next frame. Raises ``SocketClosed`` when the server closes the socket."""
        if self.close_code is not None:
            raise SocketClosed(self.close_code, self.close_reason)
        message = await asyncio.wait_for(self.outbox.get(), wait_s)
        if message["type"] == "websocket.close":
            self.close_code = message.get("code", 1000)
            self.close_reason = message.get("reason") or ""
            raise SocketClosed(self.close_code, self.close_reason)
        frame: dict[str, Any] = json.loads(message["text"])
        self.frames.append(frame)
        return frame

    async def until(
        self, predicate: Callable[[dict[str, Any]], bool], wait_s: float = WAIT_S
    ) -> dict[str, Any]:
        """Reads frames until one matches ``predicate``."""
        async with asyncio.timeout(wait_s):
            while True:
                frame = await self.receive(wait_s)
                if predicate(frame):
                    return frame

    async def closed(self, wait_s: float = WAIT_S) -> SocketClosed:
        """Reads frames until the server closes the socket."""
        async with asyncio.timeout(wait_s):
            while True:
                try:
                    await self.receive(wait_s)
                except SocketClosed as closed:
                    return closed


@asynccontextmanager
async def open_socket(
    app: FastAPI,
    path: str = "/channels/chrome/ws",
    *,
    origin: str | None = "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
) -> AsyncIterator[Socket]:
    socket = Socket()
    headers = [] if origin is None else [(b"origin", origin.encode())]
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "scheme": "ws",
        "http_version": "1.1",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "server": ("sama.test", 80),
        "client": ("127.0.0.1", 1),
        "subprotocols": [],
        "state": {},
    }

    async def receive() -> dict[str, Any]:
        return await socket.inbox.get()

    async def send(message: MutableMapping[str, Any]) -> None:
        await socket.outbox.put(dict(message))

    await socket.inbox.put({"type": "websocket.connect"})
    task = asyncio.create_task(app(scope, receive, send))
    first = await asyncio.wait_for(socket.outbox.get(), WAIT_S)
    if first["type"] == "websocket.accept":
        socket.accepted = True
    else:
        await socket.outbox.put(first)  # The close comes back from ``receive``.
    try:
        yield socket
    finally:
        await socket.inbox.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, WAIT_S)
