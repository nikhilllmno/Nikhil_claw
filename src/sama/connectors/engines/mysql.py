"""The MySQL and MariaDB engine (docs/architecture.md, Section 10.4, "What M4.13 builds").

The driver is aiomysql. One connection for each call, to the checked target, with the TLS
context of the gateway (the real host name for SNI and the certificate).

- A read runs in ``START TRANSACTION READ ONLY`` and is rolled back. The statement time limit
  is ``max_execution_time`` (MySQL, milliseconds) or ``max_statement_time`` (MariaDB,
  seconds).
- A write runs in a transaction that is committed. MySQL commits DDL at once: a DDL
  statement cannot be rolled back.
- A lock wait stops after 5 seconds (``innodb_lock_wait_timeout``).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Sequence
from typing import Any, Final

import aiomysql
import pymysql

from sama.connectors.dbcore import (
    ColumnInfo,
    RowCollector,
    TableInfo,
    Target,
    pyformat,
    scalar,
)
from sama.connectors.dbsettings import DbSettings
from sama.connectors.engines import EngineContext
from sama.connectors.http_api import DriverError, DriverUnavailableError
from sama.connectors.postgres import pinned_tls

FETCH: Final = 500
_TIMEOUT_CODES: Final = frozenset({3024, 1969})
"""MySQL: the statement passed max_execution_time. MariaDB: max_statement_time."""
_LOGIN_CODES: Final = frozenset({1044, 1045, 1049, 1698})
_NETWORK_CODES: Final = frozenset({2002, 2003, 2006, 2013, 2026, 2055})
_KEY_CODES: Final = {
    1062: "a unique key already has this value",
    1586: "a unique key already has this value",
    1451: "a foreign key refers to this row",
    1452: "a foreign key has no row with this value",
}
"""Errors whose text has the key value. The gateway shows a fixed text (as the PostgreSQL
detail, the value can be a row value)."""

_COLUMNS_SQL: Final = """
SELECT c.TABLE_SCHEMA, c.TABLE_NAME, t.TABLE_TYPE, t.TABLE_COMMENT, c.COLUMN_NAME,
       c.COLUMN_TYPE, c.IS_NULLABLE, c.COLUMN_COMMENT
FROM information_schema.COLUMNS c
JOIN information_schema.TABLES t
  ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
WHERE c.TABLE_SCHEMA = %s AND (%s IS NULL OR c.TABLE_NAME = %s)
ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
"""
_INDEXES_SQL: Final = """
SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, COLUMN_NAME
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = %s AND (%s IS NULL OR TABLE_NAME = %s)
ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
"""


def quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


class MySqlSession:
    def __init__(self, connection: Any, settings: DbSettings, write: bool) -> None:
        self._connection = connection
        self._settings = settings
        self._write = write

    async def rows(self, sql: str, params: Sequence[Any], sink: RowCollector) -> str:
        args = [scalar(v, i) for i, v in enumerate(params)]
        text = pyformat(sql, "mysql", args)
        cursor_class = aiomysql.Cursor if self._write else aiomysql.SSCursor
        try:
            async with self._connection.cursor(cursor_class) as cursor:
                await cursor.execute(text, args or None)
                if cursor.description is None:
                    count = cursor.rowcount
                    return f"{count} row(s) changed" if count is not None and count >= 0 else ""
                sink.start([d[0] for d in cursor.description])
                while batch := await cursor.fetchmany(FETCH):
                    if not all(sink.add(row) for row in batch):
                        # The rest of the result is not read: the connection closes.
                        self._connection.close()
                        break
        except pymysql.MySQLError as error:
            raise _error(error, self._settings) from None
        return ""

    async def tables(self, schema: str | None, table: str | None) -> list[TableInfo]:
        database = schema or self._settings.database
        found: dict[str, TableInfo] = {}
        try:
            async with self._connection.cursor() as cursor:
                await cursor.execute(_COLUMNS_SQL, (database, table, table))
                for row in await cursor.fetchall():
                    name = row[1]
                    info = found.get(name)
                    if info is None:
                        kind = "view" if "VIEW" in str(row[2]) else "table"
                        info = found[name] = TableInfo(row[0], name, kind, row[3] or None)
                    info.columns.append(ColumnInfo(row[4], row[5], row[6] == "NO", row[7] or None))
                await cursor.execute(_INDEXES_SQL, (database, table, table))
                indexes: dict[tuple[str, str], tuple[bool, list[str]]] = {}
                for row in await cursor.fetchall():
                    entry = indexes.setdefault((row[0], row[1]), (not row[2], []))
                    entry[1].append(row[3])
        except pymysql.MySQLError as error:
            raise _error(error, self._settings) from None
        for (name, index), (unique, columns) in indexes.items():
            info = found.get(name)
            if info is None:
                continue
            label = "primary key" if index == "PRIMARY" else "unique" if unique else "index"
            info.details.append(f"{label}: ({', '.join(columns)})")
        return list(found.values())

    async def locate(self, parts: Sequence[str]) -> tuple[str, str, list[str]] | None:
        schema, table = (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])
        found = await self.tables(schema, table)
        if not found:
            return None
        info = found[0]
        return info.schema, info.name, [c.name for c in info.columns]

    def sample_sql(self, schema: str, table: str, columns: Sequence[str], limit: int) -> str:
        names = ", ".join(quote(c) for c in columns)
        return f"SELECT {names} FROM {quote(schema)}.{quote(table)} LIMIT {int(limit)}"  # noqa: S608


def _error(error: pymysql.MySQLError, settings: DbSettings) -> DriverError:
    code = error.args[0] if error.args and isinstance(error.args[0], int) else None
    text = " ".join(str(error.args[1] if len(error.args) > 1 else error).split())[:400]
    if code in _TIMEOUT_CODES:
        return DriverError(
            "the statement stopped after the time limit (max_execution_time). Make it faster, "
            "or use LIMIT"
        )
    if code in _LOGIN_CODES:
        return DriverError(f"the database refused the login: {text} (error {code})")
    if code in _NETWORK_CODES:
        return DriverUnavailableError(
            f"the database at {settings.host}:{settings.port} cannot be reached: {text}"
        )
    if code in _KEY_CODES:
        text = _KEY_CODES[code]
    return DriverError(f"the database answered: {text}" + (f" (error {code})" if code else ""))


@contextlib.asynccontextmanager
async def open_session(
    settings: DbSettings, target: Target, context: EngineContext, *, write: bool
) -> AsyncIterator[MySqlSession]:
    tls = None
    if context.require_tls and settings.sslmode != "disable":
        tls = pinned_tls(settings.host, settings.sslmode)
    try:
        connection = await aiomysql.connect(
            host=target.host,
            port=target.port,
            user=settings.user,
            password=settings.password,
            db=settings.database,
            ssl=tls,
            connect_timeout=10,
            autocommit=False,
            charset="utf8mb4",
            local_infile=False,
            program_name="sama",
        )
    except pymysql.MySQLError as error:
        raise _error(error, settings) from None
    try:
        async with connection.cursor() as cursor:
            limit_s = int(context.timeout_s)
            if settings.engine == "mariadb":
                await cursor.execute(f"SET SESSION max_statement_time = {limit_s}")
            else:
                await cursor.execute(f"SET SESSION max_execution_time = {limit_s * 1000}")
            await cursor.execute("SET SESSION innodb_lock_wait_timeout = 5")
            await cursor.execute("START TRANSACTION" if write else "START TRANSACTION READ ONLY")
        session = MySqlSession(connection, settings, write)
        yield session
        if not connection.closed:
            if write:
                await connection.commit()
            else:
                await connection.rollback()
    except pymysql.MySQLError as error:
        raise _error(error, settings) from None
    finally:
        connection.close()
