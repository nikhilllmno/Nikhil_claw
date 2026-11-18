"""Database engine helpers (docs/architecture.md, Section 17.1)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

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


def create_engine(url: str, **kwargs: Any) -> AsyncEngine:
    """Creates an async engine for a ``postgresql://`` URL."""
    return create_async_engine(async_url(url), **kwargs)


@asynccontextmanager
async def as_user(engine: AsyncEngine, auth_user_id: uuid.UUID) -> AsyncIterator[AsyncConnection]:
    """Opens a transaction that runs as the Supabase role ``authenticated`` for one user.

    Row-level security then limits every query to the rows that the user can read, like a
    request with the user's JWT to the Supabase Data API. ``SET LOCAL`` ends with the
    transaction, so the pooled connection does not keep the role.
    """
    claims = json.dumps({"sub": str(auth_user_id), "role": "authenticated"})
    async with engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('request.jwt.claims', :claims, true)"), {"claims": claims}
        )
        await connection.execute(text("SET LOCAL ROLE authenticated"))
        yield connection
