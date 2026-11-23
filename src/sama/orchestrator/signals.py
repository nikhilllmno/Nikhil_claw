"""Wakes the event streams of a session in this process (docs/architecture.md, Section 4.3).

The worker stores the run events of a session, and the API streams them (``session_events``).
The stream reads new rows every ``poll_s``. When the worker and the stream are in the same
process, the worker also calls ``SessionSignals.notify`` after it stores events or ends a run.
The stream then reads at once and does not wait for the rest of its poll interval.

A signal only starts a read, so a lost or an extra signal cannot change what a stream sends.
A stream in another process still finds the events on its next poll.
"""

from __future__ import annotations

import asyncio
import contextlib
import weakref


class SessionSignals:
    """One wake-up flag for each session that a stream waits on now.

    A stream takes a ticket before it reads, and then waits on that ticket. So a signal that
    comes during the read is not lost. ``notify`` sets the flag of the session and removes it,
    so the next ticket is a new flag. The map keeps a flag only while a stream holds its
    ticket.
    """

    def __init__(self) -> None:
        self._flags: weakref.WeakValueDictionary[str, asyncio.Event] = weakref.WeakValueDictionary()

    def ticket(self, session_id: str) -> asyncio.Event:
        """The flag that the next ``notify`` of the session sets."""
        flag = self._flags.get(session_id)
        if flag is None:
            flag = asyncio.Event()
            self._flags[session_id] = flag
        return flag

    def notify(self, session_id: str) -> None:
        """Wakes each stream that holds a ticket of the session."""
        flag = self._flags.pop(session_id, None)
        if flag is not None:
            flag.set()

    @staticmethod
    async def wait(ticket: asyncio.Event, timeout_s: float) -> bool:
        """Waits for the ticket or for ``timeout_s``. Returns True if a signal came."""
        if timeout_s <= 0:
            return ticket.is_set()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout_s):
                await ticket.wait()
        return ticket.is_set()

    def __len__(self) -> int:
        return len(self._flags)


__all__ = ["SessionSignals"]
