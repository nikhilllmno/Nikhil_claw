"""Tests of ``SessionSignals`` (docs/architecture.md, Section 4.4)."""

from __future__ import annotations

import asyncio
import gc
import time

from sama.orchestrator.signals import SessionSignals


async def test_a_notify_wakes_the_holder_of_a_ticket_at_once() -> None:
    signals = SessionSignals()
    ticket = signals.ticket("s1")
    asyncio.get_running_loop().call_later(0.02, signals.notify, "s1")
    started = time.monotonic()
    assert await SessionSignals.wait(ticket, 5.0)
    assert time.monotonic() - started < 1.0


async def test_a_notify_during_the_read_is_not_lost() -> None:
    signals = SessionSignals()
    ticket = signals.ticket("s1")
    signals.notify("s1")  # before the wait, as during the read of a stream
    assert await SessionSignals.wait(ticket, 0.01)


async def test_a_wait_without_a_notify_ends_at_the_timeout() -> None:
    signals = SessionSignals()
    ticket = signals.ticket("s1")
    signals.notify("s2")  # another session
    assert not await SessionSignals.wait(ticket, 0.02)
    assert not await SessionSignals.wait(ticket, 0)


async def test_each_notify_needs_a_new_ticket() -> None:
    signals = SessionSignals()
    first = signals.ticket("s1")
    assert signals.ticket("s1") is first  # two streams of one session share the flag
    signals.notify("s1")
    second = signals.ticket("s1")
    assert second is not first
    assert first.is_set()
    assert not second.is_set()


async def test_a_notify_without_a_ticket_does_nothing() -> None:
    signals = SessionSignals()
    signals.notify("s1")
    assert len(signals) == 0


async def test_the_map_forgets_a_ticket_that_nobody_holds() -> None:
    signals = SessionSignals()
    ticket = signals.ticket("s1")
    assert len(signals) == 1
    del ticket
    gc.collect()
    assert len(signals) == 0
