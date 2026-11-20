"""Wakes idle workers when the run queue changes (docs/architecture.md, Section 4.3).

Migration 0020 adds a trigger that sends ``NOTIFY sama_runs`` when a run becomes ``queued`` and
when an active run ends. ``QueueWakeup`` holds one database connection that listens on the
channel. An idle worker slot waits for a notification or for its poll time, whichever comes
first. So a new message starts at once, and the poll stays as the fallback: if the connection
breaks, or if the database does not send notifications, the workers still find each run.

A slot takes a ``ticket`` before it looks for a run, and it waits on that ticket. A notification
that comes while the slot reads the queue then wakes the slot at once, and is not lost.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncEngine

log: Final = logging.getLogger("sama.orchestrator")

CHANNEL: Final = "sama_runs"
CHECK_S: Final = 30.0
"""How often the listener checks that its connection is alive."""
RETRY_S: Final = 5.0
"""How long the listener waits before it connects again after an error."""
CHECK_TIMEOUT_S: Final = 10.0


class Ticket:
    """One wait for the next notification. ``QueueWakeup.ticket`` gives a new one."""

    __slots__ = ("_event",)

    def __init__(self, event: asyncio.Event) -> None:
        self._event = event

    @property
    def woken(self) -> bool:
        return self._event.is_set()

    async def wait(self, stop: asyncio.Event, timeout_s: float) -> bool:
        """Waits for a notification, ``stop``, or ``timeout_s``. True if a notification
        came."""
        if self._event.is_set() or stop.is_set():
            return self._event.is_set()
        woken = asyncio.ensure_future(self._event.wait())
        stopped = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({woken, stopped}, timeout=timeout_s, return_when="FIRST_COMPLETED")
        finally:
            woken.cancel()
            stopped.cancel()
        return self._event.is_set()


class QueueWakeup:
    """Listens on ``CHANNEL`` and wakes the waiting tickets.

    ``notify`` wakes them too, for a change that this process makes and that sends no
    notification. One object serves all the slots of a worker.
    """

    def __init__(
        self,
        engine: AsyncEngine | None,
        *,
        check_s: float = CHECK_S,
        retry_s: float = RETRY_S,
    ) -> None:
        self._engine = engine
        self._check_s = check_s
        self._retry_s = retry_s
        self._event = asyncio.Event()
        self.listening = asyncio.Event()
        """Set while the connection listens. For tests and health checks."""

    def ticket(self) -> Ticket:
        return Ticket(self._event)

    def notify(self) -> None:
        """Wakes every ticket that was taken before this call."""
        self._event.set()
        self._event = asyncio.Event()

    async def run(self, stop: asyncio.Event) -> None:
        """Listens until ``stop`` is set. It connects again after an error."""
        if self._engine is None:
            return
        warned = False
        while not stop.is_set():
            try:
                await self._listen(stop)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # One warning for each outage. The poll finds the runs in the meantime.
                if not warned:
                    log.warning("the run queue listener stopped; the poll goes on: %r", error)
                    warned = True
            else:
                warned = False
            finally:
                self.listening.clear()
            if not stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._retry_s)

    async def _listen(self, stop: asyncio.Event) -> None:
        assert self._engine is not None  # noqa: S101 -- run() checks it
        lost = asyncio.Event()

        def on_notify(*_: Any) -> None:
            self.notify()

        def on_lost(*_: Any) -> None:
            lost.set()

        async with self._engine.connect() as connection:
            raw = await connection.get_raw_connection()
            driver: Any = raw.driver_connection
            try:
                await driver.add_listener(CHANNEL, on_notify)
                driver.add_termination_listener(on_lost)
                self.listening.set()
                # A run can be queued while the connection was down: look once now.
                self.notify()
                while not stop.is_set() and not lost.is_set():
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(_first(stop, lost), timeout=self._check_s)
                    if not stop.is_set() and not lost.is_set():
                        await asyncio.wait_for(driver.execute("SELECT 1"), CHECK_TIMEOUT_S)
                if lost.is_set():
                    raise ConnectionError("the listen connection closed")
            finally:
                # The pool must not get back a connection that listens.
                with contextlib.suppress(Exception):
                    driver.remove_termination_listener(on_lost)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(driver.remove_listener(CHANNEL, on_notify), 5.0)
                with contextlib.suppress(Exception):
                    await connection.invalidate()


async def _first(*events: asyncio.Event) -> None:
    waits = [asyncio.ensure_future(event.wait()) for event in events]
    try:
        await asyncio.wait(waits, return_when="FIRST_COMPLETED")
    finally:
        for wait in waits:
            wait.cancel()
