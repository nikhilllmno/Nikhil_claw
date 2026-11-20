"""A reader for the event stream of the API that reads while the stream is open.

``httpx.ASGITransport`` returns a response only when the stream has ended. A test that needs the
events of a run then depends on the end time of the stream, and a slow CI runner makes it fail.
This reader calls the ASGI app itself, collects the chunks as they arrive, and closes the stream
when the test has what it needs.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, MutableMapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from tests.waiting import async_wait_until

Event = tuple[str, dict[str, Any], str | None]


def parse_events(body: str) -> list[Event]:
    """The complete events of a Server-Sent Events body: ``(event, data, id)``."""
    events: list[Event] = []
    for block in body.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith(":") or ":" not in line:
                continue
            name, _, value = line.partition(":")
            fields[name] = value.lstrip()
        if "data" in fields:
            events.append(
                (fields.get("event", "message"), json.loads(fields["data"]), fields.get("id"))
            )
    return events


class EventStream:
    """One open event stream. ``events`` grows while the stream runs."""

    def __init__(self) -> None:
        self.status: int | None = None
        self.chunks: list[str] = []
        self._closed = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def events(self) -> list[Event]:
        # The last block of the text can be partial. Only the blocks that end with a blank line
        # are complete.
        text = "".join(self.chunks)
        return parse_events(text[: text.rfind("\n\n") + 2] if "\n\n" in text else "")

    async def wait_for(
        self, predicate: Callable[[list[Event]], bool], *, what: str = "the stream event"
    ) -> list[Event]:
        """Waits until ``predicate(events)`` is true, or until the stream ends."""
        await async_wait_until(
            lambda: predicate(self.events) or self._task is None or self._task.done(), what=what
        )
        assert predicate(self.events), f"the stream ended before {what}"
        return self.events

    async def _run(self, app: FastAPI, path: str, headers: dict[str, str]) -> None:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "server": ("sama.test", 80),
            "client": ("127.0.0.1", 1),
        }
        sent_request = False

        async def receive() -> dict[str, Any]:
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await self._closed.wait()
            return {"type": "http.disconnect"}

        async def send(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                self.status = message["status"]
            elif message["type"] == "http.response.body":
                self.chunks.append(message.get("body", b"").decode())

        await app(scope, receive, send)

    async def close(self) -> None:
        self._closed.set()
        if self._task is not None:
            await asyncio.wait_for(self._task, timeout=10)


@asynccontextmanager
async def open_stream(
    app: FastAPI, path: str, headers: dict[str, str]
) -> AsyncIterator[EventStream]:
    """Opens the stream and closes it at the end of the block."""
    stream = EventStream()
    task = asyncio.create_task(stream._run(app, path, headers))
    stream._task = task
    try:
        await async_wait_until(
            lambda: stream.status is not None or task.done(), what="the stream to open"
        )
        yield stream
    finally:
        await stream.close()
