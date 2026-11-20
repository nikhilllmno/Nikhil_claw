"""Wait for a condition, not for the clock.

A fixed sleep or a fixed time window is a bet that the code under test is done in time. On a slow
CI runner the bet loses and the test fails by chance. Poll the condition with a generous
deadline instead. A fast run returns at once, a slow run still passes, and a real hang fails with
a message that names what never happened.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable

DEFAULT_WITHIN = 15.0  # an upper bound: only a hang waits this long
DEFAULT_INTERVAL = 0.01


async def async_wait_until[T](
    predicate: Callable[[], T | Awaitable[T]],
    *,
    within: float = DEFAULT_WITHIN,
    interval: float = DEFAULT_INTERVAL,
    what: str = "the condition",
) -> T:
    """Returns the first true value of ``predicate()``. Raises ``AssertionError`` at the limit."""
    deadline = time.monotonic() + within
    while True:
        value = predicate()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {within}s while waiting for {what}")
        await asyncio.sleep(interval)
