"""Database engine helpers (docs/architecture.md, Section 17.1)."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from sama.db.pool import PoolSettings, configure
from sama.db.trips import instrument

ASYNC_DRIVER = "postgresql+asyncpg"


def async_url(url: str) -> str:
    """Returns ``url`` with the asyncpg driver.

    Supabase shows ``postgresql://`` URLs, and libpq uses ``sslmode``. asyncpg uses ``ssl``.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"postgres", "postgresql", ASYNC_DRIVER}:
        raise ValueError("the database URL must use the postgresql scheme")
    query = [("ssl" if key == "sslmode" else key, value) for key, value in parse_qsl(parts.query)]
    return urlunsplit(parts._replace(scheme=ASYNC_DRIVER, query=urlencode(query)))


def create_engine(url: str, *, pool: PoolSettings | None = None, **kwargs: Any) -> AsyncEngine:
    """Creates an async engine for a ``postgresql://`` URL.

    Without ``poolclass`` in ``kwargs``, the engine has the measured pool of ``pool`` (default:
    the settings of the environment, Section 4.4). The engine counts its round trips for
    ``sama.db.trips.count_trips``.
    """
    if "poolclass" not in kwargs:
        pool = pool or PoolSettings.from_env(os.environ)
        kwargs = {**pool.engine_options(), **kwargs}
    engine = create_async_engine(async_url(url), **kwargs)
    if pool is not None:
        configure(engine, pool)
    instrument(engine)
    return engine


@asynccontextmanager
async def as_user(engine: AsyncEngine, auth_user_id: uuid.UUID) -> AsyncIterator[AsyncConnection]:
    """Opens a transaction that runs as the Supabase role ``authenticated`` for one user.

    Row-level security then limits every query to the rows that the user can read, like a
    request with the user's JWT to the Supabase Data API. ``SET LOCAL`` ends with the
    transaction, so the pooled connection does not keep the role.
    """
    claims = json.dumps({"sub": str(auth_user_id), "role": "authenticated"})
    async with engine.begin() as connection:
        # One statement for both settings: each statement is a round trip to the database.
        # ``set_config('role', ..., true)`` is ``SET LOCAL ROLE``.
        await connection.execute(
            text(
                "SELECT set_config('request.jwt.claims', :claims, true),"
                " set_config('role', 'authenticated', true)"
            ),
            {"claims": claims},
        )
        yield connection


@asynccontextmanager
async def autocommit(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Opens a connection without an explicit transaction: each statement commits alone.

    Use it for work of one statement. A transaction costs two more round trips to the
    database (``BEGIN`` and ``COMMIT``), and the database can be 60 ms away (Section 4.3).
    One statement is atomic without a transaction, and its row locks last until it ends.
    """
    async with engine.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        yield connection
