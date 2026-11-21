"""The M3.11 gateway tests for the M4.13 SQL engines, against real servers in docker
(docs/architecture.md, Section 10.4, "What M4.13 builds").

Each engine runs only when its URL is set, for example
``SAMA_TEST_MYSQL_URL=mysql://root:rootpw@localhost:53306``. ``scripts/test-databases.sh``
starts the containers and sets the URLs. The CI job "Database engines (docker)" runs it.
BigQuery and Snowflake use fakes (tests/connectors/test_engine_bigquery.py and
test_engine_snowflake.py).
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from sama.connectors.database import DatabaseGateway
from sama.connectors.dbcore import MAX_ROWS
from sama.connectors.http_api import DriverError
from sama.connectors.manifest import Catalog, Manifest
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.output import MAX_OUTPUT_BYTES, untrusted
from tests.connectors.support import CATALOG
from tests.db.test_database_gateway import Sink

ROWS = 1_500
SECRET_EMAIL = "e1@hidden.example"

CLAIMS = RunTokenClaims(
    org_id=str(uuid.uuid4()),
    workspace_id=str(uuid.uuid4()),
    session_id=str(uuid.uuid4()),
    run_id=str(uuid.uuid4()),
    token_id="t",
    expires_at=2_000_000_000,
)


def _users(start: int, end: int) -> str:
    return ", ".join(
        f"({i}, 'n{i}', 'e{i}@hidden.example', 'ssn-{i}', '{'b' * 40}')" for i in range(start, end)
    )


def _seed(create_users: str, create_orders: str) -> list[str]:
    statements = [create_users, create_orders]
    for start in range(1, ROWS + 1, 500):
        rows = _users(start, min(start + 500, ROWS + 1))
        statements.append(f"INSERT INTO users VALUES {rows}")  # noqa: S608
    statements.append("INSERT INTO orders VALUES (1, 1, 9.50), (2, 2, 3.25)")
    return statements


@dataclass(frozen=True)
class Engine:
    connector: str
    env: str
    create: Callable[[str, str], Awaitable[None]]
    """Makes the database with the users and orders tables."""
    drop: Callable[[str, str], Awaitable[None]]
    sleep_sql: str
    """A read that runs longer than 1 second."""
    write_status: str
    """The status of an update of 1 row."""
    wide_sql: str
    """A read with a long value in each row."""
    row_object_sql: str
    """A read that reads a hidden column through the row (not through the column name)."""
    key_error: str
    """The text of the error of a duplicate primary key."""


# -- MySQL and MariaDB ---------------------------------------------------------------------


async def _mysql(url: str, statements: list[str], database: str | None = None) -> None:
    import aiomysql

    parts = urlsplit(url)
    connection = await aiomysql.connect(
        host=parts.hostname,
        port=parts.port or 3306,
        user=parts.username,
        password=parts.password or "",
        db=database,
        autocommit=True,
    )
    try:
        async with connection.cursor() as cursor:
            for statement in statements:
                await cursor.execute(statement)
    finally:
        connection.close()


async def _mysql_create(url: str, database: str) -> None:
    await _mysql(url, [f"CREATE DATABASE {database}"])
    await _mysql(
        url,
        _seed(
            "CREATE TABLE users (id int PRIMARY KEY, name varchar(50), "
            "email varchar(100) UNIQUE, ssn varchar(20), bio text)",
            "CREATE TABLE orders (id int PRIMARY KEY, user_id int, total decimal(10,2))",
        ),
        database,
    )


async def _mysql_drop(url: str, database: str) -> None:
    await _mysql(url, [f"DROP DATABASE IF EXISTS {database}"])


MYSQL = Engine(
    connector="mysql",
    env="SAMA_TEST_MYSQL_URL",
    create=_mysql_create,
    drop=_mysql_drop,
    sleep_sql="select count(*) from users a, users b, users c where a.id + b.id + c.id > 0",
    write_status="1 row(s) changed",
    wide_sql="select id, repeat(name, 30) as wide from users",
    row_object_sql="select json_object('a', u.ssn) from users u",
    key_error="1062",
)
MARIADB = Engine(**{**MYSQL.__dict__, "connector": "mariadb", "env": "SAMA_TEST_MARIADB_URL"})


# -- SQL Server -----------------------------------------------------------------------------


def _mssql_sync(url: str, statements: list[str], database: str | None) -> None:
    import pytds

    parts = urlsplit(url)
    with pytds.connect(
        dsn=parts.hostname,
        port=parts.port or 1433,
        user=parts.username,
        password=parts.password or "",
        database=database or "master",
        autocommit=True,
        login_timeout=10,
    ) as connection:
        cursor = connection.cursor()
        for statement in statements:
            cursor.execute(statement)


async def _mssql_create(url: str, database: str) -> None:
    await asyncio.to_thread(_mssql_sync, url, [f"CREATE DATABASE {database}"], None)
    await asyncio.to_thread(
        _mssql_sync,
        url,
        _seed(
            "CREATE TABLE users (id int PRIMARY KEY, name nvarchar(50), "
            "email nvarchar(100) UNIQUE, ssn nvarchar(20), bio nvarchar(max))",
            "CREATE TABLE orders (id int PRIMARY KEY, user_id int, total decimal(10,2))",
        ),
        database,
    )


async def _mssql_drop(url: str, database: str) -> None:
    await asyncio.to_thread(
        _mssql_sync,
        url,
        [
            f"ALTER DATABASE {database} SET SINGLE_USER WITH ROLLBACK IMMEDIATE",
            f"DROP DATABASE {database}",
        ],
        None,
    )


MSSQL = Engine(
    connector="sqlserver",
    env="SAMA_TEST_MSSQL_URL",
    create=_mssql_create,
    drop=_mssql_drop,
    sleep_sql="select count_big(*) from users a, users b, users c where a.id + b.id + c.id > 0",
    write_status="1 row(s) changed",
    wide_sql="select id, replicate(name, 30) as wide from users",
    row_object_sql="select * from users for json path",
    key_error="2627",
)


# -- ClickHouse -----------------------------------------------------------------------------


async def _clickhouse(url: str, statements: list[str], database: str | None = None) -> None:
    parts = urlsplit(url)
    base = f"http://{parts.hostname}:{parts.port or 8123}/"
    async with httpx.AsyncClient(timeout=30) as client:
        for statement in statements:
            response = await client.post(
                base,
                params={"database": database} if database else {},
                content=statement.encode(),
                headers={
                    "X-ClickHouse-User": parts.username or "default",
                    "X-ClickHouse-Key": parts.password or "",
                },
            )
            assert response.status_code == 200, response.text


async def _clickhouse_create(url: str, database: str) -> None:
    await _clickhouse(url, [f"CREATE DATABASE {database}"])
    await _clickhouse(
        url,
        _seed(
            "CREATE TABLE users (id Int32, name String, email String, ssn String, bio String) "
            "ENGINE = MergeTree ORDER BY id",
            "CREATE TABLE orders (id Int32, user_id Int32, total Decimal(10,2)) "
            "ENGINE = MergeTree ORDER BY id",
        ),
        database,
    )


async def _clickhouse_drop(url: str, database: str) -> None:
    await _clickhouse(url, [f"DROP DATABASE IF EXISTS {database}"])


CLICKHOUSE = Engine(
    connector="clickhouse",
    env="SAMA_TEST_CLICKHOUSE_URL",
    create=_clickhouse_create,
    drop=_clickhouse_drop,
    sleep_sql="select sum(sleepEachRow(0.25)) from numbers(10)",
    write_status="",
    wide_sql="select id, repeat(name, 30) as wide from users",
    row_object_sql="select tuple(u.*) from users u",
    key_error="",
)

ENGINES = [MYSQL, MARIADB, MSSQL, CLICKHOUSE]


def manifest(connector: str) -> Manifest:
    found = Catalog.load(CATALOG).get(connector)
    assert found is not None
    return found


@dataclass
class Live:
    engine: Engine
    values: dict[str, str]
    gw: DatabaseGateway
    spill: Sink
    exports: Sink

    async def run(
        self,
        operation: str,
        values: Mapping[str, str] | None = None,
        gw: DatabaseGateway | None = None,
        **arguments: Any,
    ) -> str:
        found = manifest(self.engine.connector)
        tool = next(t for t in found.tools if t.name == f"db_{self.engine.connector}_{operation}")
        return await (gw or self.gw).run(
            CLAIMS, uuid.uuid4(), found, tool, values or self.values, arguments
        )


@pytest.fixture(params=ENGINES, ids=lambda e: e.connector)
async def live(request: pytest.FixtureRequest) -> AsyncIterator[Live]:
    engine: Engine = request.param
    url = os.environ.get(engine.env)
    if not url:
        pytest.skip(f"set {engine.env} (scripts/test-databases.sh)")
    database = f"sama_{uuid.uuid4().hex[:10]}"
    await engine.create(url, database)
    parts = urlsplit(url)
    values = {
        "host": parts.hostname or "localhost",
        "port": str(parts.port),
        "database": database,
        "user": parts.username or "",
        "password": parts.password or "",
    }
    spill, exports = Sink(), Sink(".sama/exports")
    gw = DatabaseGateway(spill=spill, exports=exports, allow_private=True, require_tls=False)
    try:
        yield Live(engine, values, gw, spill, exports)
    finally:
        await gw.aclose()
        await engine.drop(url, database)


async def test_query_is_read_only(live: Live) -> None:
    for sql in (
        "insert into orders values (5, 1, 1)",
        "update orders set total = 0",
        "delete from orders",
        "select 1; delete from orders",
        "create table t (a int)",
    ):
        with pytest.raises(DriverError, match="the SQL was not run"):
            await live.run("query", sql=sql)
    assert "[2]" in await live.run("query", sql="select count(*) as n from orders")


async def test_ddl_is_denied_by_default(live: Live) -> None:
    writes = {**live.values, "writes": "on"}
    with pytest.raises(DriverError, match="DDL is off"):
        await live.run("execute", writes, sql="drop table orders")
    with pytest.raises(DriverError, match="writes are off"):
        await live.run("execute", sql="update orders set total = 1 where id = 1")


async def test_execute_changes_rows_with_parameters(live: Live) -> None:
    if live.engine.connector == "clickhouse":
        sql = "alter table orders update total = ? where id = ? settings mutations_sync = 1"
        with pytest.raises(DriverError, match="DDL is off"):
            await live.run("execute", {**live.values, "writes": "on"}, sql=sql, params=[1.25, 1])
        await live.run(
            "execute", {**live.values, "writes": "on", "ddl": "on"}, sql=sql, params=[1.25, 1]
        )
    else:
        done = await live.run(
            "execute",
            {**live.values, "writes": "on"},
            sql="update orders set total = ? where id = ?",
            params=["1.25", 1],
        )
        assert live.engine.write_status in done
        with pytest.raises(DriverError, match=live.engine.key_error) as caught:
            await live.run(
                "execute",
                {**live.values, "writes": "on"},
                sql="insert into users (id, name) values (?, 'dup')",
                params=[7],
            )
        # The key value is not in the error (it can be a row value).
        assert "'7'" not in str(caught.value)
        assert "(7)" not in str(caught.value)
    shown = await live.run("query", sql="select total from orders where id = ?", params=[1])
    assert "1.25" in shown


async def test_a_query_stops_after_the_timeout(live: Live) -> None:
    gw = DatabaseGateway(allow_private=True, require_tls=False, timeout_s=1)
    try:
        started = time.monotonic()
        with pytest.raises(DriverError, match=r"stopped after|time limit"):
            await live.run("query", gw=gw, sql=live.engine.sleep_sql)
        assert time.monotonic() - started < 8
        assert "[1]" in await live.run("query", gw=gw, sql="select 1 as one")
    finally:
        await gw.aclose()


async def test_the_model_gets_1000_rows_and_the_rest_spills(live: Live) -> None:
    answer = await live.run("query", sql="select id, name from users order by id")
    rows = [line for line in answer.splitlines() if line.startswith("[") and "Shown" not in line]
    assert len(rows) == MAX_ROWS + 1
    assert f"Shown: 1,000 of {ROWS:,} rows" in answer
    prefix = f"db_{live.engine.connector}"
    assert len(untrusted(answer, source=f"connector:{prefix}").encode()) < MAX_OUTPUT_BYTES
    ((path, text),) = live.spill.files.items()
    assert path.startswith(f"/workdir/.sama/spill/{prefix}_query-")
    lines = list(csv.reader(io.StringIO(text)))
    assert lines[0] == ["id", "name"]
    assert len(lines) == ROWS + 1
    assert lines[-1] == [str(ROWS), f"n{ROWS}"]


async def test_the_model_gets_at_most_50_kb(live: Live) -> None:
    answer = await live.run("query", sql=live.engine.wide_sql)
    assert len(untrusted(answer, source="connector:x").encode()) < MAX_OUTPUT_BYTES
    shown = int(answer.split("Shown: ")[1].split(" ")[0].replace(",", ""))
    assert shown < MAX_ROWS


async def test_hidden_columns_never_appear(live: Live) -> None:
    values = {**live.values, "hidden_columns": "users.email, ssn"}
    texts = [
        await live.run("schema", values),
        await live.run("sample", values, table="users", limit=5),
        await live.run("query", values, sql="select * from users order by id"),
        await live.run("query", values, sql="select u.* from users u where id < 3"),
        await live.run("export", values, sql="select * from users"),
    ]
    files = list(live.spill.files.values()) + list(live.exports.files.values())
    assert len(live.exports.files) == 1
    for text in texts + files:
        assert "hidden.example" not in text
        assert "ssn-" not in text
        assert "email" not in text.lower().replace("hidden columns", "")
    assert "bio" in texts[0]
    assert "name" in texts[2]
    for sql in (
        "select email from users",
        f"select id from users where email = '{SECRET_EMAIL}'",  # noqa: S608
        live.engine.row_object_sql,
        "select (select ssn from users where id = 1)",
    ):
        with pytest.raises(DriverError, match="the SQL was not run"):
            await live.run("query", values, sql=sql)


async def test_only_the_allowed_tables_show(live: Live) -> None:
    values = {**live.values, "tables": "orders"}
    described = await live.run("schema", values)
    assert "orders" in described
    assert "users" not in described
    assert "[1]\n[2]" in await live.run("query", values, sql="select id from orders order by id")
    with pytest.raises(DriverError, match="allowed"):
        await live.run("query", values, sql="select * from users")
    with pytest.raises(DriverError, match="allowed"):
        await live.run("sample", values, table="users")


async def test_only_the_allowed_schemas_show(live: Live) -> None:
    sqlserver = live.engine.connector == "sqlserver"
    # MySQL and ClickHouse: a schema is a database.
    values = {**live.values, "schemas": "dbo" if sqlserver else live.values["database"]}
    assert "users" in await live.run("schema", values)
    # SQL Server: the gateway does not know the default schema of the user, so a name needs
    # its schema on a restricted connection.
    orders = "dbo.orders" if sqlserver else "orders"
    assert "[2]" in await live.run("query", values, sql=f"select count(*) from {orders}")  # noqa: S608
    for sql in ("select * from information_schema.tables", "select * from other.orders"):
        with pytest.raises(DriverError, match="allowed"):
            await live.run("query", values, sql=sql)
    if sqlserver:
        # The database part of a name must be the database of the connection.
        with pytest.raises(DriverError, match="another database"):
            await live.run("query", sql="select * from master.dbo.spt_values")


async def test_export_writes_a_csv_file(live: Live) -> None:
    answer = await live.run("export", sql="select id, name from users")
    ((path, text),) = live.exports.files.items()
    assert path.startswith(f"/workdir/.sama/exports/db_{live.engine.connector}_export-")
    assert f"Exported {ROWS:,} rows" in answer
    assert len(text.splitlines()) == ROWS + 1


async def test_the_connection_test_uses_the_gateway(live: Live) -> None:
    found = manifest(live.engine.connector)
    await live.gw.test(found, live.values)
    with pytest.raises(DriverError, match=r"refused the login|answered"):
        await live.gw.test(found, {**live.values, "password": "wrong"})
    public_only = DatabaseGateway()
    with pytest.raises(DriverError, match="public"):
        await public_only.test(found, live.values)
