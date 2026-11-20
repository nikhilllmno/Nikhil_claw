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
"""How long a database call of the listener can take: the LISTEN setup and each check."""
STOP_WAIT_S: Final = 5.0
"""How long ``run`` waits for the listener to end after ``stop``, before it cancels it."""


class Ticket:
    """One wait for the next notification. ``QueueWakeup.ticket`` gives a new one."""

    __slots__ = ("_event",)

    def __init__(self, event: asyncio.Event) -> None:
        self._event = event

    async def wait(self, stop: asyncio.Event, timeout_s: float) -> bool:
        """Waits for a notification, ``stop``, or ``timeout_s``. True if a notification
        came."""
        if not self._event.is_set() and not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(_first(self._event, stop), timeout=timeout_s)
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
        self.backend_pid: int | None = None
        """The server process of the listen connection, while it listens. For tests."""

    def ticket(self) -> Ticket:
        return Ticket(self._event)

    def notify(self) -> None:
        """Wakes every ticket that was taken before this call."""
        self._event.set()
        self._event = asyncio.Event()

    async def run(self, stop: asyncio.Event) -> None:
        """Listens until ``stop`` is set. It connects again after an error."""
        engine = self._engine
        if engine is None:
            return
        warned = False
        while not stop.is_set():
            try:
                await self._listen_until_stop(engine, stop)
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

    async def _listen_until_stop(self, engine: AsyncEngine, stop: asyncio.Event) -> None:
        """Runs ``_listen``. A connect or a setup that hangs cannot hold the stop of the worker:
        after ``stop`` the listener gets ``STOP_WAIT_S`` to end, and then it is cancelled."""
        listen = asyncio.create_task(self._listen(engine, stop))
        stopped = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({listen, stopped}, return_when="FIRST_COMPLETED")
            if not listen.done():
                await asyncio.wait({listen}, timeout=STOP_WAIT_S)
        finally:
            stopped.cancel()
            if not listen.done():
                listen.cancel()
                await asyncio.wait({listen}, timeout=STOP_WAIT_S)
        if listen.done() and not listen.cancelled():
            listen.result()  # raises the error of the listener

    async def _listen(self, engine: AsyncEngine, stop: asyncio.Event) -> None:
        lost = asyncio.Event()

        def on_notify(*_: Any) -> None:
            self.notify()

        def on_lost(*_: Any) -> None:
            lost.set()

        async with engine.connect() as connection:
            raw = await connection.get_raw_connection()
            driver: Any = raw.driver_connection
            try:
                await asyncio.wait_for(driver.add_listener(CHANNEL, on_notify), CHECK_TIMEOUT_S)
                driver.add_termination_listener(on_lost)
                self.backend_pid = driver.get_server_pid()
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
                self.backend_pid = None
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
