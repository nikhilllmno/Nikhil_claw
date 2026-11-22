"""The request middleware and the pool monitor of the control plane (Section 4.4)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.pool import describe, named, reset_component, route_name, set_component
from sama.db.trips import count_trips

log = logging.getLogger("sama.db.monitor")

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]


class RequestMonitor:
    """An ASGI middleware. It names each HTTP request for the pool log lines, and at the level
    ``DEBUG`` it logs the number of database round trips of the request. It never reads or
    changes the request or the answer."""

    def __init__(self, app: App) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        name = route_name(str(scope.get("method", "")), str(scope.get("path", "")))
        token = set_component(name)
        try:
            if not log.isEnabledFor(logging.DEBUG):
                await self._app(scope, receive, send)
                return
            with count_trips() as trips:
                try:
                    await self._app(scope, receive, send)
                finally:
                    log.debug("request %s db_round_trips=%d", name, trips.total)
        finally:
            reset_component(token)


class PoolMonitor:
    """A background loop that logs the pool state at the level ``DEBUG``."""

    def __init__(self, engine: AsyncEngine, *, interval_s: float = 30.0) -> None:
        self._engine = engine
        self._interval_s = interval_s

    async def serve(self, stop: asyncio.Event) -> None:
        with named("pool_monitor"):
            while not stop.is_set():
                log.debug("database %s", describe(self._engine.sync_engine.pool))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._interval_s)
