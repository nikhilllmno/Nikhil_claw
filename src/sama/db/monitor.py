"""The request middleware and the pool monitor of the control plane (Section 4.4)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, MutableMapping, Sequence
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.routing import BaseRoute, Match, Mount

from sama.db.pool import describe, named, reset_component, set_component
from sama.db.trips import count_trips

log = logging.getLogger("sama.db.monitor")

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]


UNMATCHED: Final = "unmatched"
"""The name of a request whose path matches no route. The raw path is never logged: a path can
hold a secret (the token of an email approval link or of a WhatsApp webhook)."""


def route_template(routes: Sequence[BaseRoute], scope: Scope, prefix: str = "") -> str | None:
    """The route template of a request, for example ``/sessions/{session_id}/messages``, or
    ``None`` when no route matches. It follows the mounted apps. It gives the template and
    never a value of the path."""
    partial: str | None = None
    for route in routes:
        match, child = route.matches(scope)
        if match is Match.NONE:
            continue
        path = str(getattr(route, "path", ""))
        if isinstance(route, Mount):
            inner = route_template(route.routes, {**scope, **child}, prefix + path)
            if inner is not None:
                return inner
            if match is Match.FULL and not route.routes:
                # An app that has no route list: the mount path and a fixed tail.
                return f"{prefix}{path}/*"
            continue
        if match is Match.FULL:
            return prefix + path
        partial = partial or prefix + path
    return partial


def request_name(scope: Scope) -> str:
    """``METHOD template`` for the pool log lines of a request."""
    app = scope.get("app")
    routes = getattr(app, "routes", None)
    template = route_template(routes, scope) if routes else None
    return f"{scope.get('method', '')} {template or UNMATCHED}"


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
        # The name is found when a log line needs it, from the route templates of the app.
        # The mounted apps change the scope while they route, so the name uses a copy from here.
        first = dict(scope)
        cache: list[str] = []

        def name() -> str:
            if not cache:
                cache.append(request_name(first))
            return cache[0]

        token = set_component(name)
        try:
            if not log.isEnabledFor(logging.DEBUG):
                await self._app(scope, receive, send)
                return
            with count_trips() as trips:
                try:
                    await self._app(scope, receive, send)
                finally:
                    log.debug("request %s db_round_trips=%d", name(), trips.total)
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
