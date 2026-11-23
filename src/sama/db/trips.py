"""A count of the database round trips of a block of code.

One round trip to the database takes about 58 ms from the control plane on Fly (Section 4.4),
so the number of sequential statements decides the latency of a request. ``count_trips`` counts
the statements and the transaction commands (``BEGIN``, ``COMMIT``, and ``ROLLBACK``) that the
code in its block runs on any engine of this process. Tests assert an upper bound of the count,
and the API logs the count of each request at the level ``DEBUG``.
"""

from __future__ import annotations

import contextvars
import re
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine

_MAX_KEPT = 200
_SPACE = re.compile(r"\s+")


@dataclass(slots=True)
class Trips:
    """The round trips in one block."""

    statements: int = 0
    begins: int = 0
    ends: int = 0
    text: list[str] = field(default_factory=list)
    """The first words of each statement, in order, for a failing test to print."""

    @property
    def total(self) -> int:
        return self.statements + self.begins + self.ends

    def show(self) -> str:
        return "\n".join(f"{n:3d} {line}" for n, line in enumerate(self.text, 1))


_installed: weakref.WeakSet[Engine] = weakref.WeakSet()
_current: contextvars.ContextVar[Trips | None] = contextvars.ContextVar("sama_trips", default=None)


@contextmanager
def count_trips() -> Iterator[Trips]:
    """Counts the round trips of the code in the block, also in the tasks that it starts."""
    trips = Trips()
    token = _current.set(trips)
    try:
        yield trips
    finally:
        _current.reset(token)


def _note(trips: Trips, kind: str, text: str = "") -> None:
    if kind == "statement":
        trips.statements += 1
    elif kind == "begin":
        trips.begins += 1
    else:
        trips.ends += 1
    if len(trips.text) < _MAX_KEPT:
        trips.text.append(kind.upper() if not text else _SPACE.sub(" ", text).strip()[:110])


def _autocommit(connection: Any) -> bool:
    """A connection in ``AUTOCOMMIT`` sends no ``BEGIN``, ``COMMIT``, or ``ROLLBACK``."""
    return bool(connection.get_execution_options().get("isolation_level") == "AUTOCOMMIT")


def instrument(engine: AsyncEngine | Engine) -> None:
    """Counts the round trips of the engine. A second call does nothing."""
    sync = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
    if sync in _installed:
        return
    _installed.add(sync)

    @event.listens_for(sync, "before_cursor_execute")
    def statement(*args: Any) -> None:
        trips = _current.get()
        if trips is not None:
            _note(trips, "statement", str(args[2]))

    @event.listens_for(sync, "begin")
    def begin(connection: Any) -> None:
        trips = _current.get()
        if trips is not None and not _autocommit(connection):
            _note(trips, "begin")

    @event.listens_for(sync, "commit")
    def commit(connection: Any) -> None:
        trips = _current.get()
        if trips is not None and not _autocommit(connection):
            _note(trips, "commit")

    @event.listens_for(sync, "rollback")
    def rollback(connection: Any) -> None:
        trips = _current.get()
        if trips is not None and not _autocommit(connection):
            _note(trips, "rollback")
