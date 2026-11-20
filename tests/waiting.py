"""Wait for a condition, not for the clock.

A fixed sleep or a fixed time window is a bet that the code under test is done in time. On a slow
CI runner the bet loses and the test fails by chance. Poll the condition with a generous
deadline instead. A fast run returns at once, a slow run still passes, and a real hang fails with
a message that names what never happened.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

DEFAULT_WITHIN = 15.0  # an upper bound: only a hang waits this long
DEFAULT_INTERVAL = 0.01


async def async_wait_until(
    predicate: Callable[[], Any],
    *,
    within: float = DEFAULT_WITHIN,
    interval: float = DEFAULT_INTERVAL,
    what: str = "the condition",
) -> Any:
    """Returns the first true value of ``predicate()``. Raises ``AssertionError`` at the limit."""
    # The limit covers the whole wait, also an async predicate that never returns.
    try:
        async with asyncio.timeout(within):
            while True:
                value = predicate()
                if inspect.isawaitable(value):
                    value = await value
                if value:
                    return value
                await asyncio.sleep(interval)
    except TimeoutError:
        raise AssertionError(f"timed out after {within}s while waiting for {what}") from None
