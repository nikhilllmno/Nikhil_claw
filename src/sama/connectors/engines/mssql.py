"""The SQL Server engine (docs/architecture.md, Section 10.4, "What M4.13 builds").

The driver is python-tds (``pytds``), a blocking driver: each call runs in a worker thread.
The gateway opens the TCP socket to the checked target and gives it to the driver, with the
real host name for TLS (SNI and the certificate check).

- TLS: ``verify-full`` checks the certificate chain (certifi) and the host name. ``require``
  encrypts and does not check the certificate. ``disable`` (only through an SSH tunnel)
  does not encrypt.
- SQL Server can send a routing redirect to another host. The gateway does not follow it,
  because the other host is not checked.
- A call runs in a transaction. A read is always rolled back. SQL Server has no read-only
  transaction: the SQL check refuses writes in ``query``.
- A lock wait stops after 5 seconds (``SET LOCK_TIMEOUT``). A statement stops when the socket
  has no answer for the time limit.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import socket
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, Final

import certifi
import OpenSSL.SSL
import pytds
import pytds.tls

from sama.connectors.dbcore import (
    ColumnInfo,
    RowCollector,
    TableInfo,
    Target,
    locate_by_tables,
    pyformat,
    scalar,
)
from sama.connectors.dbsettings import DbSettings
from sama.connectors.engines import EngineContext
from sama.connectors.http_api import DriverError, DriverUnavailableError

FETCH: Final = 500
NO_VERIFY: Final = "sama:tls-without-verify"
"""The ``cafile`` value for TLS without a certificate check (``require``)."""
_LOGIN_CODES: Final = frozenset({18456, 4060, 18452})
_KEY_CODES: Final = {
    2627: "a unique key already has this value",
    2601: "a unique key already has this value",
    547: "a constraint refused the change",
}
"""Errors whose text has the key value. The gateway shows a fixed text (as the PostgreSQL
detail, the value can be a row value)."""
_GUARDED: Final = contextvars.ContextVar("sama_mssql_guarded", default=False)
"""True in a login of the gateway. Other users of the driver in the process do not change."""

_COLUMNS_SQL: Final = """
SELECT c.TABLE_SCHEMA, c.TABLE_NAME, t.TABLE_TYPE, c.COLUMN_NAME, c.DATA_TYPE,
       c.CHARACTER_MAXIMUM_LENGTH, c.IS_NULLABLE
FROM INFORMATION_SCHEMA.COLUMNS c
JOIN INFORMATION_SCHEMA.TABLES t
  ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
WHERE (%s IS NULL OR c.TABLE_SCHEMA = %s) AND (%s IS NULL OR c.TABLE_NAME = %s)
  AND c.TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA')
ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.ORDINAL_POSITION
"""
_INDEXES_SQL: Final = """
SELECT s.name, o.name, i.is_primary_key, i.is_unique, i.index_id, c.name
FROM sys.indexes i
JOIN sys.objects o ON o.object_id = i.object_id
JOIN sys.schemas s ON s.schema_id = o.schema_id
JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE o.type IN ('U', 'V') AND ic.is_included_column = 0
  AND (%s IS NULL OR s.name = %s) AND (%s IS NULL OR o.name = %s)
ORDER BY s.name, o.name, i.index_id, ic.key_ordinal
"""


def _patch_driver() -> None:
    """Two changes to the driver, made once:

    - ``cafile=NO_VERIFY`` gives a TLS context without a certificate check.
    - In a login of the gateway, a routing redirect (the driver connects again with no
      socket) is refused."""
    if getattr(pytds, "_sama_patched", False):
        return
    create_context = pytds.tls.create_context

    def context(cafile: str) -> OpenSSL.SSL.Context:
        if cafile != NO_VERIFY:
            made: OpenSSL.SSL.Context = create_context(cafile)
            return made
        ctx = OpenSSL.SSL.Context(OpenSSL.SSL.TLS_CLIENT_METHOD)
        ctx.set_min_proto_version(OpenSSL.SSL.TLS1_2_VERSION)
        ctx.set_verify(OpenSSL.SSL.VERIFY_NONE, lambda *_: True)
        return ctx

    connect: Callable[..., Any] = pytds._connect

    def guarded(*args: Any, **kwargs: Any) -> Any:
        if _GUARDED.get() and kwargs.get("sock") is None:
            raise pytds.tds_base.InterfaceError(
                "the server sent a redirect to another host; sama does not follow redirects"
            )
        return connect(*args, **kwargs)

    pytds.tls.create_context = context
    pytds._connect = guarded
    pytds._sama_patched = True


_patch_driver()


def quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


class MsSqlSession:
    def __init__(self, connection: Any, settings: DbSettings, write: bool, timeout_s: float):
        self._connection = connection
        self._settings = settings
        self._write = write
        self._timeout_s = timeout_s
        self.broken = False

    async def _run[T](self, work: Callable[[], T]) -> T:
        try:
            return await asyncio.to_thread(work)
        except TimeoutError:
            self.broken = True
            raise DriverError(
                f"the statement stopped after {self._timeout_s:g} seconds. Make it faster, or "
                "use TOP"
            ) from None
        except pytds.Error as error:
            raise _error(error, self._settings) from None

    async def rows(self, sql: str, params: Sequence[Any], sink: RowCollector) -> str:
        args = tuple(scalar(v, i) for i, v in enumerate(params))
        text = pyformat(sql, "tsql", args)
        cursor = self._connection.cursor()

        def start() -> Any:
            cursor.execute(text, args or None)
            return cursor.description

        description = await self._run(start)
        if description is None:
            count = cursor.rowcount
            return f"{count} row(s) changed" if count is not None and count >= 0 else ""
        sink.start([d[0] for d in description])
        while batch := await self._run(lambda: cursor.fetchmany(FETCH)):
            if not all(sink.add(row) for row in batch):
                if self._write:
                    # The transaction must end; read the rest of the result.
                    while await self._run(lambda: cursor.fetchmany(FETCH)):
                        pass
                else:
                    self.broken = True  # The connection closes; the rest is not read.
                break
        return ""

    async def execute(self, sql: str) -> None:
        def work() -> None:
            with self._connection.cursor() as cursor:
                cursor.execute(sql)

        await self._run(work)

    async def end(self, *, commit: bool) -> None:
        await self._run(self._connection.commit if commit else self._connection.rollback)

    async def _fetch(self, sql: str, args: tuple[Any, ...]) -> list[Any]:
        def work() -> list[Any]:
            with self._connection.cursor() as cursor:
                cursor.execute(sql, args)
                return list(cursor.fetchall())

        return await self._run(work)

    async def tables(self, schema: str | None, table: str | None) -> list[TableInfo]:
        found: dict[tuple[str, str], TableInfo] = {}
        args = (schema, schema, table, table)
        for row in await self._fetch(_COLUMNS_SQL, args):
            key = (row[0], row[1])
            info = found.get(key)
            if info is None:
                kind = "view" if row[2] == "VIEW" else "table"
                info = found[key] = TableInfo(row[0], row[1], kind)
            type_name = row[4]
            if row[5] is not None:
                type_name += "(max)" if row[5] == -1 else f"({row[5]})"
            info.columns.append(ColumnInfo(row[3], type_name, row[6] == "NO"))
        indexes: dict[tuple[str, str, int], tuple[str, list[str]]] = {}
        for row in await self._fetch(_INDEXES_SQL, args):
            label = "primary key" if row[2] else "unique" if row[3] else "index"
            indexes.setdefault((row[0], row[1], row[4]), (label, []))[1].append(row[5])
        for (owner, name, _), (label, columns) in indexes.items():
            info = found.get((owner, name))
            if info is not None:
                info.details.append(f"{label}: ({', '.join(columns)})")
        return list(found.values())

    async def locate(self, parts: Sequence[str]) -> tuple[str, str, list[str]] | None:
        # A name without a schema: dbo first, as SQL Server finds it for most users.
        return await locate_by_tables(
            self, parts, order=lambda t: (t.schema.lower() != "dbo", t.schema)
        )

    def sample_sql(self, schema: str, table: str, columns: Sequence[str], limit: int) -> str:
        names = ", ".join(quote(c) for c in columns)
        return f"SELECT TOP ({int(limit)}) {names} FROM {quote(schema)}.{quote(table)}"  # noqa: S608


def _error(error: pytds.Error, settings: DbSettings) -> DriverError:
    number = getattr(error, "number", 0) or getattr(error, "msg_no", 0)
    text = " ".join(str(getattr(error, "text", "") or error).split())[:400]
    if isinstance(error, pytds.LoginError) or number in _LOGIN_CODES:
        return DriverError(f"the database refused the login: {text}")
    if number == 1222:
        return DriverError("a lock wait passed 5 seconds (LOCK_TIMEOUT). Try again later")
    if isinstance(error, pytds.InterfaceError):
        return DriverError(f"the statement was not run: {text}")
    if isinstance(error, pytds.OperationalError) and not number:
        return DriverUnavailableError(
            f"the database at {settings.host}:{settings.port} cannot be reached: {text}"
        )
    if number in _KEY_CODES:
        text = _KEY_CODES[number]
    return DriverError(f"the database answered: {text}" + (f" (error {number})" if number else ""))


def _connect(settings: DbSettings, target: Target, context: EngineContext) -> Any:
    """Opens the socket and logs in (in a worker thread)."""
    cafile: str | None = None
    if context.require_tls and settings.sslmode != "disable":
        cafile = certifi.where() if settings.sslmode == "verify-full" else NO_VERIFY
    sock = socket.create_connection((target.host, target.port), timeout=10)
    _GUARDED.set(True)  # The worker thread has a copy of the context.
    try:
        return pytds.connect(
            dsn=settings.host,
            port=target.port,
            database=settings.database,
            user=settings.user,
            password=settings.password,
            timeout=context.timeout_s,
            login_timeout=10,
            appname="sama",
            autocommit=False,
            cafile=cafile,
            validate_host=settings.sslmode == "verify-full",
            sock=sock,
            disable_connect_retry=True,
            bytes_to_unicode=True,
        )
    except BaseException:
        sock.close()
        raise


@contextlib.asynccontextmanager
async def open_session(
    settings: DbSettings, target: Target, context: EngineContext, *, write: bool
) -> AsyncIterator[MsSqlSession]:
    try:
        connection = await asyncio.to_thread(_connect, settings, target, context)
    except pytds.Error as error:
        raise _error(error, settings) from None
    session = MsSqlSession(connection, settings, write, context.timeout_s)
    try:
        await session.execute("SET LOCK_TIMEOUT 5000")
        yield session
        if not session.broken:
            await session.end(commit=write)
    finally:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(connection.close)
