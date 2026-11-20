"""Tests of the wait helper of the tests."""

from __future__ import annotations

import asyncio

import pytest

from tests.waiting import async_wait_until


async def test_it_returns_the_first_true_value() -> None:
    calls: list[int] = []

    def ready() -> str:
        calls.append(1)
        return "done" if len(calls) == 3 else ""

    assert await async_wait_until(ready, within=5, interval=0.001) == "done"


async def test_it_awaits_an_async_predicate() -> None:
    async def ready() -> bool:
        return True

    assert await async_wait_until(ready, within=5) is True


async def test_it_fails_with_the_name_of_the_condition() -> None:
    with pytest.raises(AssertionError, match=r"timed out after 0\.05s while waiting for the thing"):
        await async_wait_until(lambda: False, within=0.05, interval=0.001, what="the thing")


async def test_a_blocked_async_predicate_cannot_pass_the_limit() -> None:
    async def blocked() -> bool:
        await asyncio.Event().wait()
        return True

    with pytest.raises(AssertionError, match=r"timed out after 0\.05s while waiting for the lock"):
        await asyncio.wait_for(async_wait_until(blocked, within=0.05, what="the lock"), timeout=5)
