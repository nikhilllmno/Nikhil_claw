"""The browser that a person chose for a session (docs/architecture.md, Section 14.2.4, M8.3).

The web app sets ``sessions.browser_target`` (``cloud`` or ``my_chrome``) with ``PATCH
/sessions/{id}``. A browser call without a target uses it (``BrowserService.resolve``). The
store keeps each answer for a short time, so the calls of a run do not each read the database.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.browser.target import CLOUD, MY_CHROME, TargetName
from sama.db.models import Session
from sama.tenancy.run_token import RunTokenClaims

CACHE_S: Final = 10.0
"""A change of the choice applies to the next browser call after at most this time."""
MAX_ENTRIES: Final = 10_000


class SessionBrowserChoice:
    """Reads the browser choice of the session of a run."""

    def __init__(self, engine: AsyncEngine, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._engine = engine
        self._clock = clock
        self._cache: dict[tuple[str, str], tuple[float, TargetName | None]] = {}

    async def __call__(self, claims: RunTokenClaims) -> TargetName | None:
        key = (claims.org_id, claims.session_id)
        now = self._clock()
        hit = self._cache.get(key)
        if hit is not None and now - hit[0] < CACHE_S:
            return hit[1]
        async with self._engine.connect() as connection:
            value = (
                await connection.execute(
                    select(Session.browser_target).where(
                        Session.org_id == uuid.UUID(claims.org_id),
                        Session.id == uuid.UUID(claims.session_id),
                    )
                )
            ).scalar_one_or_none()
        chosen: TargetName | None = None
        if value == CLOUD:
            chosen = CLOUD
        elif value == MY_CHROME:
            chosen = MY_CHROME
        if len(self._cache) >= MAX_ENTRIES:
            self._cache.clear()
        self._cache[key] = (now, chosen)
        return chosen

    def forget(self, org_id: str, session_id: str) -> None:
        """Drops the kept choice of a session after a change in this process."""
        self._cache.pop((org_id, session_id), None)
