"""The engines of the database gateway other than PostgreSQL (docs/architecture.md, Section
10.4, "What M4.13 builds").

Each engine module has ``open_session(settings, target, context, *, write)``: an async
context manager that gives a ``SqlSession`` (``sama.connectors.dbcore``) for one call. The
drivers are in the optional ``databases`` dependency group, so this package imports an engine
module only when a connection of that engine is used. An engine whose driver is not installed
raises ``DriverUnavailableError``.
"""

from __future__ import annotations

import contextlib
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from sama.connectors.dbcore import SqlSession, Target
from sama.connectors.dbsettings import DbSettings
from sama.connectors.http_api import DriverUnavailableError
from sama.connectors.postgres import pinned_tls
from sama.tools.outbound import Resolver, system_resolver

if TYPE_CHECKING:
    from sama.connectors.engines.mongo import MongoSession


@dataclass(frozen=True, slots=True)
class EngineContext:
    """The values of the gateway that an engine uses.

    - ``timeout_s``: the time limit of one statement.
    - ``require_tls``: ``False`` only in tests (a local server without TLS).
    - ``resolver`` and ``transport``: for the HTTP engines (BigQuery, Snowflake), which send
      requests through ``OutboundClient``. ``transport`` is a fake in tests.
    """

    timeout_s: float
    require_tls: bool = True
    resolver: Resolver = system_resolver
    transport: httpx.AsyncBaseTransport | None = None

    def tls(self, settings: DbSettings) -> ssl.SSLContext | None:
        """The TLS context for the database host (its real name for SNI and the
        certificate), or ``None`` for no TLS."""
        if not self.require_tls or settings.sslmode == "disable":
            return None
        return pinned_tls(settings.host, settings.sslmode)


def _missing(engine: str) -> DriverUnavailableError:
    return DriverUnavailableError(
        f"the {engine} driver is not installed on this server (the 'databases' dependency group)"
    )


@contextlib.asynccontextmanager
async def open_sql(
    settings: DbSettings, target: Target, context: EngineContext, *, write: bool
) -> AsyncIterator[SqlSession]:
    """A session of an SQL engine (not PostgreSQL) for one call."""
    engine = settings.engine
    try:
        if engine in ("mysql", "mariadb"):
            from sama.connectors.engines import mysql as module
        elif engine == "mssql":
            from sama.connectors.engines import mssql as module  # type: ignore[no-redef]
        elif engine == "clickhouse":
            from sama.connectors.engines import clickhouse as module  # type: ignore[no-redef]
        elif engine == "bigquery":
            from sama.connectors.engines import bigquery as module  # type: ignore[no-redef]
        elif engine == "snowflake":
            from sama.connectors.engines import snowflake as module  # type: ignore[no-redef]
        else:
            raise _missing(engine)
    except ImportError:
        raise _missing(engine) from None
    async with module.open_session(settings, target, context, write=write) as session:
        yield session


@contextlib.asynccontextmanager
async def open_mongo(
    settings: DbSettings, target: Target, context: EngineContext
) -> AsyncIterator[MongoSession]:
    """A MongoDB session for one call."""
    try:
        from sama.connectors.engines import mongo
    except ImportError:
        raise _missing("MongoDB") from None
    async with mongo.open_session(settings, target, context) as session:
        yield session
