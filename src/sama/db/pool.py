"""The database connection pool: its settings, the wait time of a checkout, and its stats.

One control plane process shares one pool between the API, the worker, the tool server, the gate,
the model proxy, and the event streams. The database can be 60 ms away (Section 4.3), so a
connection that is open too long makes the next request wait. This module measures each wait
and logs a warning with the name of the component that waited (Section 4.4).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import AsyncAdaptedQueuePool, ConnectionPoolEntry, PoolProxiedConnection

log = logging.getLogger("sama.db.pool")

UNKNOWN_COMPONENT: Final = "unknown"

_component: contextvars.ContextVar[str] = contextvars.ContextVar(
    "sama_db_component", default=UNKNOWN_COMPONENT
)
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def component() -> str:
    """The name of the component that runs now, for the pool log lines."""
    return _component.get()


def set_component(name: str) -> contextvars.Token[str]:
    """Names the component of the current task. A loop sets it once at its start."""
    return _component.set(name)


def reset_component(token: contextvars.Token[str]) -> None:
    """Puts back the name from before ``set_component``."""
    _component.reset(token)


@contextmanager
def named(name: str) -> Iterator[None]:
    """Names the component for the code in the block."""
    token = _component.set(name)
    try:
        yield
    finally:
        _component.reset(token)


def route_name(method: str, path: str) -> str:
    """A name for a request, with each id in the path replaced by ``{id}``."""
    return f"{method} {_UUID.sub('{id}', path)}"


@dataclass(frozen=True, slots=True)
class PoolSettings:
    """The size of the pool of one process, from the environment.

    The default fits one Fly machine and the session mode of the Supabase pooler (port 5432),
    which gives 15 server connections to one database user by default: ``size`` plus
    ``overflow`` stays at or below this number. With the transaction mode (port 6543), set
    ``SAMA_DB_PREPARED_STATEMENTS=off`` and use a larger size.
    """

    size: int = 10
    """Connections that the pool keeps open (``SAMA_DB_POOL_SIZE``)."""
    overflow: int = 5
    """Extra connections for a burst, closed again at once (``SAMA_DB_POOL_OVERFLOW``)."""
    timeout_s: float = 30.0
    """Seconds that a checkout waits before it fails (``SAMA_DB_POOL_TIMEOUT_S``)."""
    recycle_s: float = 1800.0
    """A connection older than this is replaced (``SAMA_DB_POOL_RECYCLE_S``)."""
    prewarm: int = 4
    """Connections that the process opens at its start, so the first requests do not wait for
    the connect (about 4 round trips) (``SAMA_DB_POOL_PREWARM``)."""
    warn_ms: float = 100.0
    """A checkout that waits longer logs a warning (``SAMA_DB_POOL_WARN_MS``)."""
    prepared_statements: bool = True
    """``off`` is for the transaction mode of a pooler (``SAMA_DB_PREPARED_STATEMENTS``)."""

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> PoolSettings:
        """Reads the settings. A bad value raises ``ValueError`` with the name of the variable."""
        default = cls()

        def number(name: str, fallback: float, minimum: float) -> float:
            text = env.get(name, "").strip()
            if not text:
                return fallback
            try:
                value = float(text)
            except ValueError:
                raise ValueError(f"{name} must be a number") from None
            if value < minimum:
                raise ValueError(f"{name} must be at least {minimum:g}")
            return value

        flag = env.get("SAMA_DB_PREPARED_STATEMENTS", "on").strip().lower()
        if flag not in {"on", "off"}:
            raise ValueError("SAMA_DB_PREPARED_STATEMENTS must be on or off")
        size = int(number("SAMA_DB_POOL_SIZE", default.size, 1))
        return cls(
            size=size,
            overflow=int(number("SAMA_DB_POOL_OVERFLOW", default.overflow, 0)),
            timeout_s=number("SAMA_DB_POOL_TIMEOUT_S", default.timeout_s, 1),
            recycle_s=number("SAMA_DB_POOL_RECYCLE_S", default.recycle_s, 30),
            prewarm=min(size, int(number("SAMA_DB_POOL_PREWARM", default.prewarm, 0))),
            warn_ms=number("SAMA_DB_POOL_WARN_MS", default.warn_ms, 0),
            prepared_statements=flag == "on",
        )

    def engine_options(self) -> dict[str, Any]:
        """The ``create_async_engine`` options for these settings."""
        options: dict[str, Any] = {
            "poolclass": TimedQueuePool,
            "pool_size": self.size,
            "max_overflow": self.overflow,
            "pool_timeout": self.timeout_s,
            "pool_recycle": self.recycle_s,
            # The last connection that came back is the most likely to be open and warm.
            "pool_use_lifo": True,
        }
        if not self.prepared_statements:
            # The recipe of SQLAlchemy for a pooler in transaction mode.
            options["connect_args"] = {
                "statement_cache_size": 0,
                "prepared_statement_cache_size": 0,
                "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
            }
        return options


@dataclass(slots=True)
class PoolStats:
    """The counters of one pool."""

    warn_s: float = 0.1
    checkouts: int = 0
    connects: int = 0
    slow: int = 0
    """Checkouts that waited longer than ``warn_s``."""
    wait_total_s: float = 0.0
    wait_max_s: float = 0.0
    connect_total_s: float = 0.0
    by_component: dict[str, int] = field(default_factory=dict)
    """Slow checkouts for each component."""

    def record(self, wait_s: float, connect_s: float, name: str) -> bool:
        """Adds one checkout. Gives ``True`` when it waited too long."""
        self.checkouts += 1
        self.wait_total_s += wait_s
        self.wait_max_s = max(self.wait_max_s, wait_s)
        if connect_s > 0:
            self.connects += 1
            self.connect_total_s += connect_s
        if wait_s <= self.warn_s:
            return False
        self.slow += 1
        self.by_component[name] = self.by_component.get(name, 0) + 1
        return True


_CONNECT_KEY: Final = "sama_connect_s"


class TimedQueuePool(AsyncAdaptedQueuePool):
    """A queue pool that measures the wait of each checkout.

    The wait is the time to get a connection, without the time to open a new one: a new
    connection costs about 4 round trips and has its own counter (``connect_total_s``).
    """

    stats: PoolStats

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.stats = PoolStats()

    def _create_connection(self) -> ConnectionPoolEntry:
        start = time.monotonic()
        record = super()._create_connection()
        record.info[_CONNECT_KEY] = time.monotonic() - start
        return record

    def connect(self) -> PoolProxiedConnection:
        start = time.monotonic()
        proxied = super().connect()
        elapsed = time.monotonic() - start
        record = proxied._connection_record
        connect_s = 0.0
        if record is not None:
            connect_s = float(record.info.pop(_CONNECT_KEY, 0.0))
        wait_s = max(0.0, elapsed - connect_s)
        name = component()
        if self.stats.record(wait_s, connect_s, name):
            log.warning(
                "database pool wait %.0f ms component=%s %s",
                wait_s * 1000,
                name,
                describe(self),
            )
        return proxied


def describe(pool: Any) -> str:
    """One line with the state of the pool. It has numbers only, no host and no secret."""
    stats = getattr(pool, "stats", None)
    line = (
        f"pool size={pool.size()} in_use={pool.checkedout()} idle={pool.checkedin()}"
        f" overflow={max(0, pool.overflow())}"
    )
    if stats is not None:
        line += (
            f" checkouts={stats.checkouts} slow={stats.slow} connects={stats.connects}"
            f" wait_max_ms={stats.wait_max_s * 1000:.0f}"
            f" wait_avg_ms={_average(stats.wait_total_s, stats.checkouts) * 1000:.1f}"
        )
    return line


def _average(total: float, count: int) -> float:
    return total / count if count else 0.0


def configure(engine: AsyncEngine, settings: PoolSettings) -> None:
    """Gives the warn limit of the settings to the pool of the engine, if it measures."""
    pool = engine.sync_engine.pool
    if isinstance(pool, TimedQueuePool):
        pool.stats.warn_s = settings.warn_ms / 1000


async def prewarm(engine: AsyncEngine, count: int) -> int:
    """Opens ``count`` connections at once and gives them back. Gives the number that opened.

    A failure only logs: the pool opens a connection when a request needs one.
    """
    if count <= 0:
        return 0

    async def one() -> bool:
        try:
            async with engine.connect() as connection:
                await connection.exec_driver_sql("SELECT 1")
        except Exception:
            log.warning("a database connection could not be opened at the start", exc_info=True)
            return False
        return True

    with named("prewarm"):
        # The checkouts overlap, so the pool opens ``count`` different connections.
        results = await asyncio.gather(*(one() for _ in range(count)))
    opened = sum(results)
    log.info("database pool warm opened=%d %s", opened, describe(engine.sync_engine.pool))
    return opened
