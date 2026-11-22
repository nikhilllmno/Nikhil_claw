"""Warms the session sandbox before a run (docs/architecture.md, Section 4.4).

``SessionWarmer.request`` starts ``warm`` (``SandboxedRuntime.warm``) for one session in the
background and returns at once. The API calls it when a user creates a session
(``POST /sessions``) and when the web app asks for it (``POST /sessions/{id}/warm``, for
example when the user starts to type). A warm creates no run, issues no run token, and puts no
secret in the sandbox.

Limits, all in the memory of this process:

- one warm at a time for each session, and no new warm for ``min_interval_s`` after the
  start of the last one (the answer is ``recent``),
- at most ``per_user`` warm requests of one user in ``window_s`` (``WarmRateLimitedError``).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Final

log: Final = logging.getLogger("sama.orchestrator")

WarmFunction = Callable[[str, str], Awaitable[str]]
"""Warms the sandbox of ``(org_id, session_id)`` and returns a short result. It must not
raise."""

MIN_INTERVAL_S: Final = 60.0
PER_USER: Final = 30
WINDOW_S: Final = 60.0
_MEMORY: Final = 10_000


class WarmStatus(StrEnum):
    WARMING = "warming"
    """A warm of the session started now."""
    RECENT = "recent"
    """A warm of the session runs now, or started less than ``min_interval_s`` ago."""
    OFF = "off"
    """This process has no session sandbox to warm."""


class WarmRateLimitedError(Exception):
    def __init__(self, retry_after_s: int) -> None:
        super().__init__("too many warm requests. Try again later.")
        self.retry_after_s = retry_after_s


class SessionWarmer:
    def __init__(
        self,
        warm: WarmFunction,
        *,
        min_interval_s: float = MIN_INTERVAL_S,
        per_user: int = PER_USER,
        window_s: float = WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._warm = warm
        self._min_interval_s = min_interval_s
        self._per_user = per_user
        self._window_s = window_s
        self._clock = clock
        self._started: dict[tuple[str, str], float] = {}
        self._running: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._users: dict[str, deque[float]] = {}

    def request(self, org_id: str, session_id: str, user_id: str) -> WarmStatus:
        """Starts a warm of the session in the background, unless one is recent. Raises
        ``WarmRateLimitedError`` when the user asked too often."""
        now = self._clock()
        self._limit(user_id, now)
        key = (org_id, session_id)
        last = self._started.get(key)
        if key in self._running or (last is not None and now - last < self._min_interval_s):
            return WarmStatus.RECENT
        self._started.pop(key, None)
        self._started[key] = now
        while len(self._started) > _MEMORY:
            del self._started[next(iter(self._started))]
        task = asyncio.create_task(self._run(key))
        self._running[key] = task
        return WarmStatus.WARMING

    def _limit(self, user_id: str, now: float) -> None:
        times = self._users.setdefault(user_id, deque())
        while times and now - times[0] >= self._window_s:
            times.popleft()
        if len(times) >= self._per_user:
            left = self._window_s - (now - times[0]) if times else self._window_s
            raise WarmRateLimitedError(max(1, math.ceil(left)))
        times.append(now)
        if len(self._users) > _MEMORY:
            idle = [u for u, t in self._users.items() if not t or now - t[-1] >= self._window_s]
            for user in idle:
                del self._users[user]

    async def _run(self, key: tuple[str, str]) -> None:
        try:
            result = await self._warm(*key)
            log.info("session warm org=%s session=%s result=%s", key[0], key[1], result)
        except Exception:
            log.exception("the session warm failed org=%s session=%s", key[0], key[1])
        finally:
            self._running.pop(key, None)

    async def wait_idle(self) -> None:
        """Waits for the running warms. For a shutdown and for tests."""
        while running := list(self._running.values()):
            await asyncio.wait(running)

    async def aclose(self) -> None:
        """Cancels the running warms."""
        for task in list(self._running.values()):
            task.cancel()
        await asyncio.gather(*self._running.values(), return_exceptions=True)


__all__ = [
    "MIN_INTERVAL_S",
    "PER_USER",
    "WINDOW_S",
    "SessionWarmer",
    "WarmRateLimitedError",
    "WarmStatus",
]
