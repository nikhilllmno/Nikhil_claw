"""The database gateway: the tools of ``database`` connectors (docs/architecture.md,
Section 10.4).

The tools are ``db_<conn>_schema``, ``sample``, ``query``, ``export``, and ``execute``. The
connection values come from the vault (the password) and the row (the other fields). The
agent never sees them.

The rules:

- ``schema``, ``sample``, ``query``, and ``export`` run in a read-only transaction that is
  always rolled back. ``execute`` runs in a read-write transaction. The gate asks a person
  before each ``execute`` call (access ``destructive``), and the approval card shows the SQL.
- Each statement passes the SQL check first (``sama.connectors.sql``): one statement, no
  write in ``query``, no DDL unless the connection allows it.
- Each statement stops after ``timeout_s`` (30 seconds): ``statement_timeout`` in the
  database, and a local timer that ends the connection if the server does not answer.
- The model gets at most 1,000 rows and 50 KB. The full result goes to a CSV file in the
  session sandbox (``.sama/spill/``). ``export`` writes a CSV file to ``.sama/exports/``.
- Hidden columns never show: the SQL check refuses references to them, and the gateway drops
  result columns with their names.
- The gateway connects only to public addresses. It resolves the host, checks each address,
  and connects to the checked address (no second DNS answer). TLS is required.
- One small pool for each connection, at most ``per_org`` calls at a time for each org.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import ssl
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import asyncpg
import httpx
from asyncpg.pool import PoolConnectionProxy

from sama.connectors.dbcore import (
    MAX_MODEL_BYTES,
    MAX_ROWS,
    RowCollector,
    SqlSession,
    Target,
    render_tables,
)
from sama.connectors.dbcore import Rows as _Rows
from sama.connectors.dbforms import (
    MAX_EXECUTE_SQL_CHARS,
    MAX_PARAMS,
    MAX_SAMPLE_ROWS,
    MAX_SQL_CHARS,
    SPECS,
)
from sama.connectors.dbsettings import DbSettings
from sama.connectors.engines import EngineContext, open_mongo, open_sql
from sama.connectors.http_api import DriverError, DriverUnavailableError
from sama.connectors.manifest import DatabaseOperation, Manifest, ToolSpec
from sama.connectors.mongoquery import MongoRead, collection_of, read_of
from sama.connectors.postgres import (
    COLUMNS_SQL,
    CONSTRAINTS_SQL,
    INDEXES_SQL,
    KINDS,
    MAX_CELL_CHARS,
    RELATION_SQL,
    SAMPLE_COLUMNS_SQL,
    coerce,
    mentions,
    pinned_tls,
    quote_ident,
)
from sama.connectors.sql import Checked, Mode, SqlRejectedError, check
from sama.connectors.tunnel import Tunnels, forward, open_ssh
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.outbound import (
    METADATA_HOSTS,
    IPAddress,
    Resolver,
    address_problem,
    system_resolver,
)
from sama.tools.output import Spill
from sama.vault.redact import redact

if TYPE_CHECKING:
    from sama.connectors.engines.mongo import MongoSession

__all__ = ["MAX_MODEL_BYTES", "MAX_ROWS", "DatabaseGateway", "_Rows"]

log: Final = logging.getLogger("sama.connectors.database")

type _Connection = PoolConnectionProxy[asyncpg.Record]

TIMEOUT_S: Final = 30.0
"""A statement stops after this time."""
CONNECT_TIMEOUT_S: Final = 10.0
SPILL_MAX_ROWS: Final = 100_000
SPILL_MAX_BYTES: Final = 20_000_000
EXPORT_MAX_ROWS: Final = 200_000
EXPORT_MAX_BYTES: Final = 50_000_000
FILE_TIMEOUT_S: Final = 120.0
SCHEMA_TTL_S: Final = 600.0
"""The ``schema`` result is kept this long."""
MAX_CARD_CHARS: Final = 1_400
"""The arguments of ``execute`` must fit on the approval card in full
(``sama.gateway.sessions.MAX_ARGUMENT_CHARS`` is 1,500, with the indent of the card)."""
POOL_SIZE: Final = 2
POOL_IDLE_S: Final = 300.0
PER_ORG: Final = 4
PREFETCH: Final = 500


@dataclass(slots=True)
class _Pool:
    fingerprint: str
    pool: asyncpg.Pool
    used_at: float
    borrowers: int = 0
    """The calls that use the pool now. A pool with borrowers is not closed."""
    retired: bool = False
    """The pool is out of the map (other settings); its last borrower closes it."""


class DatabaseGateway:
    """The gateway for all orgs. One for the process. A ``FormDriver`` for the connection
    test, and ``run`` for the tools.

    - ``spill``: where the full result of a large query goes (``.sama/spill/``).
    - ``exports``: where ``export`` writes (``.sama/exports/``).
    - ``resolver``, ``allow_private``, ``require_tls``, and ``http_transport`` are for tests:
      the gateway in the app connects only to public addresses, with TLS. ``allow_private``
      is ``True`` (every host) or a set of host names (only these hosts).
    """

    def __init__(
        self,
        *,
        spill: Spill | None = None,
        exports: Spill | None = None,
        timeout_s: float = TIMEOUT_S,
        resolver: Resolver = system_resolver,
        allow_private: bool | frozenset[str] = False,
        require_tls: bool = True,
        per_org: int = PER_ORG,
        clock: Any = time.monotonic,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.spill = spill
        self.exports = exports
        self.timeout_s = timeout_s
        self._resolve = resolver
        self._allow_private = allow_private
        self._require_tls = require_tls
        self._per_org = per_org
        self._clock = clock
        self._pools: dict[str, _Pool] = {}
        self._lock = asyncio.Lock()
        self._limits: dict[str, asyncio.Semaphore] = {}
        self._closing: set[asyncio.Future[None]] = set()
        self._schemas: dict[tuple[str, str, str, str, bool], tuple[float, str]] = {}
        self._tunnels = Tunnels(clock=clock, idle_s=POOL_IDLE_S)
        self._engine = EngineContext(
            timeout_s=timeout_s,
            require_tls=require_tls,
            resolver=resolver,
            transport=http_transport,
        )

    async def aclose(self) -> None:
        async with self._lock:
            pools, self._pools = list(self._pools.values()), {}
        for entry in pools:
            await _close(entry.pool)
        await self._tunnels.aclose()

    # -- FormDriver ------------------------------------------------------------------------

    async def test(self, manifest: Manifest, values: Mapping[str, str]) -> None:
        """Connects (through a new SSH tunnel, if the form has one), runs ``SELECT 1`` (or a
        ``ping``) in a read-only transaction, and disconnects."""
        settings = DbSettings.parse(manifest, values)
        key = f"test:{uuid.uuid4()}"
        try:
            if settings.engine != "postgres":
                await self._test_engine(key, settings)
                return
            async with self._route(key, settings) as target:
                await self._test_postgres(settings, target)
        finally:
            await self._tunnels.discard(key)

    async def _test_postgres(self, settings: DbSettings, target: Target) -> None:
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_S + self.timeout_s):
                connection = await asyncpg.connect(
                    **self._connect_args(settings, target.host, target.port)
                )
                try:
                    async with connection.transaction(readonly=True):
                        await connection.fetchval("SELECT 1")
                finally:
                    await connection.close(timeout=5)
        except asyncpg.InvalidAuthorizationSpecificationError as error:
            raise DriverError(f"the database refused the login: {_message(error)}") from None
        except asyncpg.InvalidCatalogNameError as error:
            raise DriverError(f"the database does not exist: {_message(error)}") from None
        except asyncpg.PostgresError as error:
            raise DriverError(f"the database answered: {_message(error)}") from None
        except (OSError, ssl.SSLError, TimeoutError, asyncpg.InterfaceError) as error:
            raise DriverUnavailableError(_network(error, settings)) from None

    async def _test_engine(self, key: str, settings: DbSettings) -> None:
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_S * 2 + self.timeout_s):
                if settings.engine == "mongodb":
                    async with self._mongo_session(key, settings) as mongo:
                        await mongo.ping()
                    return
                async with (
                    self._route(key, settings) as target,
                    open_sql(settings, target, self._engine, write=False) as session,
                ):
                    sink = RowCollector(frozenset(), 1, SPILL_MAX_BYTES)
                    await session.rows("SELECT 1", [], sink)
        except (OSError, ssl.SSLError) as error:  # TimeoutError is an OSError
            raise DriverUnavailableError(_network(error, settings)) from None

    async def call(
        self,
        manifest: Manifest,
        tool: ToolSpec,
        values: Mapping[str, str],
        arguments: Any,
        *,
        idempotency_key: str | None = None,
    ) -> str:
        raise DriverError("a database tool needs the run (use run)")

    # -- the tools -------------------------------------------------------------------------

    async def run(
        self,
        claims: RunTokenClaims,
        connection_id: uuid.UUID,
        manifest: Manifest,
        tool: ToolSpec,
        values: Mapping[str, str],
        arguments: Mapping[str, Any],
    ) -> str:
        """Runs one database tool. Raises ``DriverError`` with a message for the model."""
        operation = manifest.operation(tool)
        if operation is None:
            raise DriverError(f"{tool.name} is not a database tool")
        settings = DbSettings.parse(manifest, values)
        if operation == "execute":
            _check_card(arguments)
            if not settings.writes:
                raise DriverError(
                    "writes are off for this connection. An admin can turn on 'Allow writes'"
                )
        conn = manifest.name.replace("-", "_")
        if settings.engine == "mongodb":
            return await self._mongo(claims, connection_id, settings, operation, arguments, conn)
        if operation == "schema":
            return await self._schema(claims, connection_id, settings, arguments)
        if operation == "sample":
            return await self._sample(claims, connection_id, settings, arguments)
        sql = arguments.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise DriverError("sql is required")
        limit = MAX_EXECUTE_SQL_CHARS if operation == "execute" else MAX_SQL_CHARS
        if len(sql) > limit:
            raise DriverError(f"the SQL is longer than {limit:,} characters")
        params = arguments.get("params") or []
        if not isinstance(params, list) or len(params) > MAX_PARAMS:
            raise DriverError(f"params must be a list of at most {MAX_PARAMS} values")
        mode: Mode = "execute" if operation == "execute" else "query"
        try:
            checked = check(sql, mode, settings.policy, settings.dialect)
        except SqlRejectedError as error:
            raise DriverError(f"the SQL was not run: {error}") from None
        if operation == "export":
            if (arguments.get("format") or "csv") != "csv":
                raise DriverError("the export format must be csv")
            return await self._export(claims, connection_id, settings, checked, params, conn)
        return await self._statement(
            claims, connection_id, settings, checked, params, operation, conn
        )

    async def _collect(
        self,
        claims: RunTokenClaims,
        connection_id: uuid.UUID,
        settings: DbSettings,
        statement: Checked | MongoRead,
        params: Sequence[Any],
        max_rows: int,
        max_bytes: int,
        *,
        write: bool = False,
        keep_csv: bool = False,
        show: int = MAX_ROWS,
    ) -> _Rows:
        """Runs one statement (or one MongoDB read) and collects the rows."""
        if isinstance(statement, MongoRead):
            sink = RowCollector(frozenset(), max_rows, max_bytes, keep_csv=keep_csv, show=show)
            async with self._mongo_call(claims, connection_id, settings) as mongo:
                status = await mongo.read(statement, sink)
            return sink.finish(status)
        drop = statement.drop_columns
        if settings.engine == "postgres":
            async with self._transaction(
                claims, connection_id, settings, write=write
            ) as connection:
                return await self._read_rows(
                    connection,
                    statement.sql,
                    params,
                    drop,
                    max_rows,
                    max_bytes,
                    keep_csv=keep_csv,
                    show=show,
                )
        sink = RowCollector(drop, max_rows, max_bytes, keep_csv=keep_csv, show=show)
        async with self._sql_call(claims, connection_id, settings, write=write) as session:
            status = await session.rows(statement.sql, params, sink)
        return sink.finish(status)

    async def _statement(
        self,
        claims: RunTokenClaims,
        connection_id: uuid.UUID,
        settings: DbSettings,
        checked: Checked | MongoRead,
        params: Sequence[Any],
        operation: DatabaseOperation,
        conn: str,
    ) -> str:
        write = operation == "execute"
        rows = await self._collect(
            claims,
            connection_id,
            settings,
            checked,
            params,
            SPILL_MAX_ROWS,
            SPILL_MAX_BYTES,
            write=write,
        )
        path: str | None = None
        if rows.csv is not None:
            path = await self._save(self.spill, claims, f"db_{conn}_{operation}", rows.csv)
        return _render(rows, path=path, conn=conn, write=write)

    async def _export(
        self,
        claims: RunTokenClaims,
        connection_id: uuid.UUID,
        settings: DbSettings,
        checked: Checked | MongoRead,
        params: Sequence[Any],
        conn: str,
    ) -> str:
        if self.exports is None:
            raise DriverError("export needs a session sandbox, and this run has none")
        rows = await self._collect(
            claims,
            connection_id,
            settings,
            checked,
            params,
            EXPORT_MAX_ROWS,
            EXPORT_MAX_BYTES,
            keep_csv=True,
            show=0,
        )
        text = rows.csv or ""
        path = await self._save(self.exports, claims, f"db_{conn}_export", text)
        if path is None:
            raise DriverError("the export file could not be written to the sandbox")
        more = (
            ""
            if rows.complete
            else (
                f" The export stopped at {rows.count:,} rows (the limit is "
                f"{EXPORT_MAX_ROWS:,} rows or {EXPORT_MAX_BYTES // 1_000_000} MB). Use a "
                "narrower query."
            )
        )
        hidden = f" {rows.dropped} hidden column(s) are not in the file." if rows.dropped else ""
        return (
            f"Exported {rows.count:,} rows and {len(rows.columns)} columns "
            f"({len(text.encode()):,} bytes, CSV with a header row) to {path}.{more}{hidden}\n"
            f"Columns: {', '.join(rows.columns)}"
        )

    async def _read_rows(
        self,
        connection: _Connection,
        sql: str,
        params: Sequence[Any],
        drop: frozenset[str],
        max_rows: int,
        max_bytes: int,
        *,
        keep_csv: bool = False,
        show: int = MAX_ROWS,
    ) -> _Rows:
        sink = RowCollector(drop, max_rows, max_bytes, keep_csv=keep_csv, show=show)
        try:
            statement = await connection.prepare(sql)
            types = [t.name for t in statement.get_parameters()]
            args = coerce(params, types)
            attributes = statement.get_attributes()
            if not attributes:
                await statement.fetch(*args)
                return sink.finish(statement.get_statusmsg() or "")
            sink.start([a.name for a in attributes])
            async for record in statement.cursor(*args, prefetch=PREFETCH):
                if not sink.add(list(record.values())):
                    break
            status = statement.get_statusmsg() or ""
        except asyncpg.QueryCanceledError:
            raise DriverError(
                f"the statement stopped after {self.timeout_s:g} seconds (statement_timeout). "
                "Make it faster, or use LIMIT"
            ) from None
        except asyncpg.PostgresError as error:
            raise DriverError(f"the database answered: {_message(error)}") from None
        except asyncpg.InterfaceError as error:
            raise DriverError(f"the statement was not run: {error}") from None
        return sink.finish(status)

    async def _schema(
        self,
        claims: RunTokenClaims,
        connection_id: uuid.UUID,
        settings: DbSettings,
        arguments: Mapping[str, Any],
    ) -> str:
        size = 63 if settings.engine == "postgres" else 260
        schema = _optional_name(arguments.get("schema"), "schema", size)
        table = _optional_name(arguments.get("table"), "table", size)
        key = (str(connection_id), settings.fingerprint(), schema or "", table or "", True)
        now = self._clock()
        cached = self._schemas.get(key)
        if cached is not None and cached[0] > now and not arguments.get("refresh"):
            return cached[1]
        policy = settings.policy
        if settings.engine != "postgres":
            text = await self._engine_schema(claims, connection_id, settings, schema, table)
            self._schemas[key] = (now + SCHEMA_TTL_S, text)
            return text
        async with self._transaction(claims, connection_id, settings, write=False) as connection:
            try:
                rows = await connection.fetch(COLUMNS_SQL, schema, table)
                tables: dict[int, dict[str, Any]] = {}
                for row in rows:
                    if not policy.table_allowed(row["schema"], row["table"]):
                        continue
                    entry = tables.setdefault(
                        row["oid"],
                        {
                            "name": f"{row['schema']}.{row['table']}",
                            "table": row["table"],
                            "kind": KINDS.get(row["kind"], "table"),
                            "comment": row["table_comment"],
                            "columns": [],
                        },
                    )
                    if policy.column_hidden(row["table"], row["column"]):
                        continue
                    entry["columns"].append(row)
                oids = list(tables)
                constraints = await connection.fetch(CONSTRAINTS_SQL, oids) if oids else []
                indexes = await connection.fetch(INDEXES_SQL, oids) if oids else []
            except asyncpg.QueryCanceledError:
                raise DriverError(
                    "reading the catalog took too long. Give schema or table"
                ) from None
            except asyncpg.PostgresError as error:
                raise DriverError(f"the database answered: {_message(error)}") from None
        # A constraint or an index that names a hidden column (of any table) is left out.
        hidden = frozenset(column for _, column in policy.hidden)
        text = _render_schema(
            tables, constraints, indexes, hidden=hidden, filtered=bool(schema or table)
        )
        self._schemas[key] = (now + SCHEMA_TTL_S, text)
        return text

    async def _sample(
        self,
        claims: RunTokenClaims,
        connection_id: uuid.UUID,
        settings: DbSettings,
        arguments: Mapping[str, Any],
    ) -> str:
        name = arguments.get("table")
        if not isinstance(name, str) or not name.strip():
            raise DriverError("table is required")
        limit = arguments.get("limit") or 5
        bad = not isinstance(limit, int) or isinstance(limit, bool)
        if bad or not 0 < limit <= MAX_SAMPLE_ROWS:
            raise DriverError(f"limit must be 1 to {MAX_SAMPLE_ROWS}")
        if settings.engine != "postgres":
            return await self._engine_sample(claims, connection_id, settings, name, limit)
        parts = [p.strip() for p in name.strip().split(".")]
        if len(parts) > 2 or not all(parts):
            raise DriverError("table must be schema.table or table")
        policy = settings.policy
        regclass = ".".join(quote_ident(p) for p in parts)
        async with self._transaction(claims, connection_id, settings, write=False) as connection:
            try:
                found = await connection.fetchrow(RELATION_SQL, regclass)
                if found is None:
                    raise DriverError(f"the table {name} does not exist (or you cannot read it)")
                if not policy.table_allowed(found["schema"], found["table"]):
                    raise DriverError(f"{name} is not an allowed table on this connection")
                columns = [
                    r["column"]
                    for r in await connection.fetch(SAMPLE_COLUMNS_SQL, found["oid"])
                    if not policy.column_hidden(found["table"], r["column"])
                ]
                if not columns:
                    raise DriverError(f"all columns of {name} are hidden")
                # Each name is quoted, and the limit is an int.
                sql = (
                    f"SELECT {', '.join(quote_ident(c) for c in columns)} FROM "  # noqa: S608
                    f"{quote_ident(found['schema'])}.{quote_ident(found['table'])} LIMIT {limit}"
                )
            except asyncpg.PostgresError as error:
                raise DriverError(f"the database answered: {_message(error)}") from None
            rows = await self._read_rows(connection, sql, [], frozenset(), limit, SPILL_MAX_BYTES)
        hidden = frozenset(c for t, c in policy.hidden if t in (None, found["table"].lower()))
        return _render(
            rows,
            path=None,
            conn="",
            write=False,
            title=f"{found['schema']}.{found['table']}",
            note_hidden=bool(hidden),
        )

    async def _engine_schema(
        self,
        claims: RunTokenClaims,
        connection_id: uuid.UUID,
        settings: DbSettings,
        schema: str | None,
        table: str | None,
    ) -> str:
        """The ``schema`` tool of the other SQL engines."""
        policy = settings.policy
        spec = SPECS[settings.engine]
        async with self._sql_call(claims, connection_id, settings, write=False) as session:
            found = await session.tables(schema, table)
        tables = [
            t
            for t in found
            if t.schema.lower() not in settings.dialect.system_schemas
            and policy.table_allowed(t.schema, t.name)
        ]
        return render_tables(
            tables,
            hidden=policy.column_hidden,
            filtered=bool(schema or table),
            word=spec.schema_word,
        )

    async def _engine_sample(
        self,
        claims: RunTokenClaims,
        connection_id: uuid.UUID,
        settings: DbSettings,
        name: str,
        limit: int,
    ) -> str:
        """The ``sample`` tool of the other SQL engines."""
        policy = settings.policy
        parts = [p.strip().strip('`"[]') for p in name.strip().split(".")]
        if len(parts) > 2 or not all(parts):
            raise DriverError("table must be schema.table or table")
        sink = RowCollector(frozenset(), limit, SPILL_MAX_BYTES)
        async with self._sql_call(claims, connection_id, settings, write=False) as session:
            found = await session.locate(parts)
            if found is None:
                raise DriverError(f"the table {name} does not exist (or you cannot read it)")
            schema, table, all_columns = found
            if schema.lower() in settings.dialect.system_schemas or not policy.table_allowed(
                schema, table
            ):
                raise DriverError(f"{name} is not an allowed table on this connection")
            columns = [c for c in all_columns if not policy.column_hidden(table, c)]
            if not columns:
                raise DriverError(f"all columns of {name} are hidden")
            status = await session.rows(session.sample_sql(schema, table, columns, limit), [], sink)
        return _render(
            sink.finish(status),
            path=None,
            conn="",
            write=False,
            title=f"{schema}.{table}",
            note_hidden=len(columns) < len(all_columns),
        )

    async def _mongo(
        self,
        claims: RunTokenClaims,
        connection_id: uuid.UUID,
        settings: DbSettings,
        operation: DatabaseOperation,
        arguments: Mapping[str, Any],
        conn: str,
    ) -> str:
        """The tools of a MongoDB connection (reads only)."""
        policy = settings.policy
        if operation == "schema":
            table = _optional_name(arguments.get("table"), "table", 120)
            if table is not None:
                collection_of(table, policy)
            key = (str(connection_id), settings.fingerprint(), "", table or "", True)
            now = self._clock()
            cached = self._schemas.get(key)
            if cached is not None and cached[0] > now and not arguments.get("refresh"):
                return cached[1]
            async with self._mongo_call(claims, connection_id, settings) as mongo:
                found = await mongo.collections(table)
            text = render_tables(
                [t for t in found if policy.table_allowed(None, t.name)],
                hidden=policy.column_hidden,
                filtered=table is not None,
                word="database",
            )
            self._schemas[key] = (now + SCHEMA_TTL_S, text)
            return text
        if operation == "sample":
            limit = arguments.get("limit") or 5
            if not isinstance(limit, int) or isinstance(limit, bool):
                raise DriverError(f"limit must be 1 to {MAX_SAMPLE_ROWS}")
            read = read_of(
                {"collection": arguments.get("table"), "limit": limit},
                policy,
                limit_max=MAX_SAMPLE_ROWS,
            )
            rows = await self._collect(
                claims, connection_id, settings, read, [], limit, SPILL_MAX_BYTES
            )
            return _render(
                rows,
                path=None,
                conn="",
                write=False,
                title=read.collection,
                note_hidden=bool(read.hidden),
            )
        if operation == "export":
            if (arguments.get("format") or "csv") != "csv":
                raise DriverError("the export format must be csv")
            read = read_of(
                {k: v for k, v in arguments.items() if k != "format"},
                policy,
                limit_max=EXPORT_MAX_ROWS,
            )
            return await self._export(claims, connection_id, settings, read, [], conn)
        if operation != "query":
            raise DriverError("a MongoDB connection is read only")
        read = read_of(arguments, policy, limit_max=SPILL_MAX_ROWS)
        return await self._statement(claims, connection_id, settings, read, [], operation, conn)

    # -- connections -----------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _transaction(
        self,
        claims: RunTokenClaims,
        connection_id: uuid.UUID,
        settings: DbSettings,
        *,
        write: bool,
    ) -> AsyncIterator[_Connection]:
        """A connection in a transaction with the limits of the connection. A read
        transaction is read only and always rolled back."""
        async with self._org_limit(claims), self._route(str(connection_id), settings) as target:
            entry = await self._borrow(connection_id, settings, target)
            try:
                async with self._in_transaction(entry.pool, settings, write=write) as connection:
                    yield connection
            finally:
                await self._give_back(entry)

    @contextlib.asynccontextmanager
    async def _org_limit(self, claims: RunTokenClaims) -> AsyncIterator[None]:
        """At most ``per_org`` calls at a time for each org."""
        limit = self._limits.setdefault(claims.org_id, asyncio.Semaphore(self._per_org))
        async with limit:
            yield

    @contextlib.asynccontextmanager
    async def _route(self, key: str, settings: DbSettings) -> AsyncIterator[Target]:
        """Where the driver connects: the checked address of the host, or the local end of
        the SSH tunnel of the connection (Section 10.6). BigQuery and Snowflake send HTTPS
        requests through ``OutboundClient``, which checks the host itself."""
        if not SPECS[settings.engine].network:
            yield Target(settings.host, settings.port)
            return
        ssh = settings.ssh
        if ssh is None:
            yield Target(str(await self._address(settings.host, settings.port)), settings.port)
            return
        destination = _tunnel_destination(settings.host)

        async def opener() -> tuple[Any, Any]:
            address = await self._address(ssh.host, ssh.port)
            connection = await open_ssh(ssh, str(address))
            try:
                listener = await forward(connection, destination, settings.port)
            except BaseException:
                connection.close()
                raise
            return connection, listener

        async with self._tunnels.route(key, settings.fingerprint(), opener) as target:
            yield target

    @contextlib.asynccontextmanager
    async def _sql_call(
        self,
        claims: RunTokenClaims,
        connection_id: uuid.UUID,
        settings: DbSettings,
        *,
        write: bool,
    ) -> AsyncIterator[SqlSession]:
        """A session of an SQL engine (not PostgreSQL) for one call, with the limits."""
        async with self._org_limit(claims), self._route(str(connection_id), settings) as target:
            try:
                async with (
                    asyncio.timeout(CONNECT_TIMEOUT_S + self.timeout_s + 5),
                    open_sql(settings, target, self._engine, write=write) as session,
                ):
                    yield session
            except TimeoutError:
                raise DriverError(
                    f"the statement stopped after {self.timeout_s:g} seconds"
                ) from None
            except (OSError, ssl.SSLError) as error:
                raise DriverUnavailableError(_network(error, settings)) from None

    @contextlib.asynccontextmanager
    async def _mongo_call(
        self, claims: RunTokenClaims, connection_id: uuid.UUID, settings: DbSettings
    ) -> AsyncIterator[MongoSession]:
        async with (
            self._org_limit(claims),
            self._mongo_session(str(connection_id), settings) as session,
        ):
            yield session

    @contextlib.asynccontextmanager
    async def _mongo_session(self, key: str, settings: DbSettings) -> AsyncIterator[MongoSession]:
        async with self._route(key, settings) as target:
            try:
                async with (
                    asyncio.timeout(CONNECT_TIMEOUT_S + self.timeout_s + 5),
                    open_mongo(settings, target, self._engine) as session,
                ):
                    yield session
            except TimeoutError:
                raise DriverError(f"the read stopped after {self.timeout_s:g} seconds") from None
            except (OSError, ssl.SSLError) as error:
                raise DriverUnavailableError(_network(error, settings)) from None

    @contextlib.asynccontextmanager
    async def _in_transaction(
        self, pool: asyncpg.Pool, settings: DbSettings, *, write: bool
    ) -> AsyncIterator[_Connection]:
        try:
            connection = await asyncio.wait_for(pool.acquire(), CONNECT_TIMEOUT_S)
        except asyncpg.InvalidAuthorizationSpecificationError as error:
            raise DriverError(f"the database refused the login: {_message(error)}") from None
        except asyncpg.PostgresError as error:
            raise DriverError(f"the database answered: {_message(error)}") from None
        except (OSError, ssl.SSLError, TimeoutError, asyncpg.InterfaceError) as error:
            raise DriverError(_network(error, settings)) from None
        broken = False
        try:
            async with asyncio.timeout(self.timeout_s + 5):
                transaction = connection.transaction(readonly=not write)
                await transaction.start()
                try:
                    await connection.execute(
                        f"SET LOCAL statement_timeout = {int(self.timeout_s * 1000)}"
                    )
                    await connection.execute("SET LOCAL lock_timeout = 5000")
                    path = settings.policy.search_path()
                    if path:
                        await connection.execute(
                            "SET LOCAL search_path = " + ", ".join(map(quote_ident, path))
                        )
                    yield connection
                except BaseException as error:
                    # After a cancel, the connection is ended below, not rolled back.
                    if not isinstance(error, asyncio.CancelledError) and (
                        not connection.is_closed()
                    ):
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(transaction.rollback(), 5)
                    raise
                if write:
                    await transaction.commit()
                else:
                    await transaction.rollback()
        except TimeoutError:
            broken = True
            raise DriverError(f"the statement stopped after {self.timeout_s:g} seconds") from None
        except asyncio.CancelledError:
            broken = True
            raise
        except asyncpg.PostgresError as error:
            raise DriverError(f"the database answered: {_message(error)}") from None
        finally:
            if broken and not connection.is_closed():
                connection.terminate()
            await pool.release(connection)

    async def _borrow(
        self, connection_id: uuid.UUID, settings: DbSettings, target: Target
    ) -> _Pool:
        """The pool of the connection, marked as in use. A pool that is idle for more than
        ``POOL_IDLE_S``, or that has other settings or another target (a new tunnel port), is
        retired: it closes when its last borrower gives it back. Give the pool back with
        ``_give_back``."""
        key = str(connection_id)
        fingerprint = f"{settings.fingerprint()}:{target.host}:{target.port}"
        now = self._clock()
        stale: list[asyncpg.Pool] = []
        async with self._lock:
            for other, idle in list(self._pools.items()):
                expired = now - idle.used_at > POOL_IDLE_S and idle.borrowers == 0
                if expired or (other == key and idle.fingerprint != fingerprint):
                    entry = self._pools.pop(other)
                    entry.retired = True
                    if entry.borrowers == 0:
                        stale.append(entry.pool)
            found = self._pools.get(key)
            if found is None:
                pool = asyncpg.create_pool(
                    **self._connect_args(settings, target.host, target.port),
                    min_size=0,
                    max_size=POOL_SIZE,
                    max_inactive_connection_lifetime=60.0,
                )
                await pool  # min_size 0: this opens no connection.
                found = _Pool(fingerprint, pool, now)
                self._pools[key] = found
            found.used_at = now
            found.borrowers += 1
        try:
            for old in stale:
                # The close goes on after a cancel; the pool is not in the map any more.
                closing = asyncio.ensure_future(_close(old))
                self._closing.add(closing)
                closing.add_done_callback(self._closing.discard)
                await asyncio.shield(closing)
        except BaseException:
            await asyncio.shield(self._give_back(found))
            raise
        return found

    async def _give_back(self, entry: _Pool) -> None:
        async with self._lock:
            entry.borrowers -= 1
            entry.used_at = self._clock()
            close = entry.retired and entry.borrowers == 0
        if close:
            await _close(entry.pool)

    async def _address(self, host: str, port: int) -> IPAddress:
        """The checked address to connect to. Every address of the host must be public."""
        allowed = self._allow_private is True or (
            isinstance(self._allow_private, frozenset) and host in self._allow_private
        )
        local = host in METADATA_HOSTS or host == "localhost" or host.endswith(".localhost")
        if local and not allowed:
            raise DriverError(f"{host} is not a public host")
        try:
            addresses: Sequence[IPAddress] = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT_S):
                    addresses = await self._resolve(host, port)
            except (OSError, UnicodeError, TimeoutError):
                raise DriverUnavailableError(f"{host} cannot be resolved") from None
        if not addresses:
            raise DriverUnavailableError(f"{host} cannot be resolved")
        if not allowed:
            for address in addresses:
                problem = address_problem(address)
                if problem is not None:
                    raise DriverError(
                        f"{host} resolves to {problem}. sama connects only to public addresses"
                    )
        return addresses[0]

    def _connect_args(self, settings: DbSettings, host: str, port: int) -> dict[str, Any]:
        tls: ssl.SSLContext | bool = pinned_tls(settings.host, settings.sslmode)
        if not self._require_tls or settings.sslmode == "disable":
            tls = False
        return {
            "host": host,
            "port": port,
            "user": settings.user,
            "password": settings.password,
            "database": settings.database,
            "ssl": tls,
            "timeout": CONNECT_TIMEOUT_S,
            # Transaction poolers (Supabase, Neon) do not keep named statements between
            # transactions. Each statement is prepared in its own transaction.
            "statement_cache_size": 0,
            "server_settings": {"application_name": "sama"},
        }

    async def _save(
        self, sink: Spill | None, claims: RunTokenClaims, stem: str, text: str
    ) -> str | None:
        if sink is None:
            return None
        name = f"{stem}-{uuid.uuid4().hex[:12]}.csv"
        try:
            async with asyncio.timeout(FILE_TIMEOUT_S):
                # Redact first, so a secret never reaches the sandbox.
                return await sink.save(claims, name, redact(text).encode("utf-8"))
        except Exception as error:  # a file that fails does not fail the call
            log.warning(
                "database file failed: run=%s error=%s", claims.run_id, type(error).__name__
            )
            return None


# -- text for the model ---------------------------------------------------------------------


def _render(
    rows: _Rows,
    *,
    path: str | None,
    conn: str,
    write: bool,
    title: str | None = None,
    note_hidden: bool = False,
) -> str:
    if not rows.columns:
        return f"Done: {rows.status}." if write else f"Done: {rows.status or 'no result'}."
    counted = f"{rows.count:,}" + ("" if rows.complete else "+")
    head = f"{title}: " if title else ""
    lines = [f"{head}{counted} row(s). Columns: {', '.join(rows.columns)}."]
    if write:
        lines[0] = f"Done{': ' + rows.status if rows.status else ''}. Returned " + lines[0]
    lines.append(json.dumps(rows.columns, ensure_ascii=False, separators=(",", ":")))
    lines.extend(rows.shown)
    notes: list[str] = []
    if len(rows.shown) < rows.count or not rows.complete:
        where = (
            f"The full result ({rows.count:,} rows, CSV with a header row) is in the file "
            f"{path}. Read that file for the other rows."
            if path is not None
            else "The other rows were not kept."
        )
        notes.append(f"[Shown: {len(rows.shown):,} of {counted} rows. {where}")
        if not rows.complete:
            notes.append(
                f"The result has more rows than were read. Use db_{conn}_export for a large result."
            )
        notes[-1] += "]"
    if rows.cut:
        where = f"the file {path}" if path is not None else "no file (the file was not written)"
        notes.append(
            f"[Values longer than {MAX_CELL_CHARS:,} characters end with '...[cut]'. The full "
            f"values are in {where}.]"
        )
    if rows.dropped or note_hidden:
        notes.append("[Hidden columns of this connection are not shown.]")
    return "\n".join(lines + notes)


def _render_schema(
    tables: Mapping[int, Mapping[str, Any]],
    constraints: Sequence[Any],
    indexes: Sequence[Any],
    *,
    hidden: frozenset[str],
    filtered: bool,
) -> str:
    if not tables:
        return "No tables match (or the user cannot read them)."
    by_table: dict[int, list[str]] = {}
    for row in constraints:
        if hidden and mentions(row["definition"], hidden):
            continue
        label = {"p": "primary key", "u": "unique", "f": "foreign key"}[row["kind"]]
        by_table.setdefault(row["oid"], []).append(f"  {label}: {row['definition']}")
    for row in indexes:
        if hidden and mentions(row["definition"], hidden):
            continue
        by_table.setdefault(row["oid"], []).append(f"  index: {row['definition']}")
    out: list[str] = []
    used = 0
    shown = 0
    for oid, table in tables.items():
        block = [f"{table['name']} ({table['kind']})"]
        if table["comment"]:
            block[0] += f" -- {table['comment'][:300]}"
        for column in table["columns"]:
            line = f"  {column['column']} {column['type']}"
            if column["not_null"]:
                line += " not null"
            if column["column_comment"]:
                line += f" -- {column['column_comment'][:200]}"
            block.append(line)
        block.extend(by_table.get(oid, []))
        text = "\n".join(block)
        if used + len(text.encode()) > MAX_MODEL_BYTES - 500:
            break
        out.append(text)
        used += len(text.encode()) + 1
        shown += 1
    if shown < len(tables):
        hint = "" if filtered else " Give schema or table to see the others."
        out.append(f"[Shown: {shown} of {len(tables)} tables.{hint}]")
    return "\n".join(out)


def _check_card(arguments: Mapping[str, Any]) -> None:
    card = json.dumps(dict(arguments), ensure_ascii=False, indent=1, default=str)
    if len(card) > MAX_CARD_CHARS:
        raise DriverError(
            f"the SQL and params are longer than {MAX_CARD_CHARS:,} characters, so the "
            "approval card cannot show them in full. Split the change"
        )


def _optional_name(value: Any, label: str, size: int = 63) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > size:
        raise DriverError(f"{label} must be a name")
    return value


def _message(error: asyncpg.PostgresError) -> str:
    """The message and the SQLSTATE of a database error. The detail is left out: it can hold
    row values (for example the key of a unique violation)."""
    message = " ".join(str(getattr(error, "message", "") or error).split())[:400]
    state = getattr(error, "sqlstate", None)
    return f"{message} (SQLSTATE {state})" if state else message


def _tunnel_destination(host: str) -> str:
    """The database host as the bastion sees it. It can be private (that is the reason for
    the tunnel), but not a cloud metadata host or a link-local address."""
    if host in METADATA_HOSTS:
        raise DriverError(f"{host} is a cloud metadata host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if address.is_link_local or address.is_multicast or address.is_unspecified:
        raise DriverError(f"{host} is not a database address")
    return host


def _network(error: BaseException, settings: DbSettings) -> str:
    name = type(error).__name__
    if isinstance(error, TimeoutError):
        return f"the database at {settings.host}:{settings.port} did not answer in time"
    if isinstance(error, ssl.SSLError):
        return f"the TLS connection to {settings.host} failed ({name})"
    text = " ".join(str(error).split())[:200]
    return f"the database at {settings.host}:{settings.port} cannot be reached: {name} {text}"


async def _close(pool: asyncpg.Pool) -> None:
    try:
        await asyncio.wait_for(pool.close(), 10)
    except Exception:  # a pool that does not close is ended
        pool.terminate()
