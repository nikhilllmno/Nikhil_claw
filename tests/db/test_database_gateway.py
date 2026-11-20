"""The database gateway with a real PostgreSQL server (docs/architecture.md, Section 10.4).

The gateway connects to the test server as an outside database: the tables are in a schema
of their own. The test server is on localhost without TLS, so the gateway of these tests
allows a private address and no TLS. The gateway of the app does not.
"""
# ruff: noqa: S608  (the tests build SQL for a test schema with a random name)

from __future__ import annotations

import asyncio
import csv
import io
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import asyncpg
import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.connectors import database
from sama.connectors.composio import ComposioClient
from sama.connectors.database import MAX_ROWS, POOL_IDLE_S, DatabaseGateway
from sama.connectors.dbcore import Target
from sama.connectors.http_api import DriverError
from sama.connectors.manifest import Catalog, Manifest
from sama.connectors.postgres import DbSettings
from sama.connectors.service import ConnectionService, ConnectorError
from sama.gate import GateStep
from sama.gate.service import GateService
from sama.gateway.sessions import approval_text
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.deferred import DeferredCatalog
from sama.tools.output import MAX_OUTPUT_BYTES, untrusted
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import OrgKeys, SecretName, Vault
from tests.connectors.support import CATALOG, WEB, FakeComposio
from tests.db.test_connections import member
from tests.db.test_deferred_gate import grant
from tests.db.test_tool_gate import Org1, add_rule, approval
from tests.db.test_tool_gate import org as gate_org

org = gate_org

ROWS = 3_000
SECRET_EMAIL = "e1@hidden.example"


class Sink:
    """A fake sandbox: it keeps the files in memory."""

    def __init__(self, folder: str = ".sama/spill") -> None:
        self.folder = folder
        self.files: dict[str, str] = {}

    async def save(self, claims: RunTokenClaims, name: str, data: bytes) -> str | None:
        path = f"/workdir/{self.folder}/{name}"
        self.files[path] = data.decode()
        return path


@pytest.fixture
async def outside(database_url: str) -> AsyncIterator[dict[str, str]]:
    """The form values of a database with the schema ``gw_<hex>``: ``users`` (3,000 rows, with
    an email and an ssn), ``orders``, a sequence, and a function that writes."""
    schema = f"gw_{uuid.uuid4().hex[:8]}"
    connection = await asyncpg.connect(database_url)
    try:
        await connection.execute(
            f"""
            CREATE SCHEMA {schema};
            CREATE TABLE {schema}.users (
                id int PRIMARY KEY, name text, email text UNIQUE, ssn text, bio text);
            INSERT INTO {schema}.users
                SELECT i, 'n' || i, 'e' || i || '@hidden.example', 'ssn-' || i, repeat('b', 40)
                FROM generate_series(1, {ROWS}) AS i;
            CREATE TABLE {schema}.orders (id int PRIMARY KEY, user_id int, total numeric);
            INSERT INTO {schema}.orders VALUES (1, 1, 9.50), (2, 2, 3.25);
            CREATE SEQUENCE {schema}.counter;
            CREATE FUNCTION {schema}.sneaky() RETURNS int LANGUAGE sql AS
                $$ INSERT INTO {schema}.orders VALUES (99, 1, 0) RETURNING 1 $$;
            """
        )
    finally:
        await connection.close()
    url = urlsplit(database_url)
    yield {
        "host": url.hostname or "localhost",
        "port": str(url.port or 5432),
        "database": url.path.lstrip("/"),
        "user": url.username or "postgres",
        "password": url.password or "",
        "schemas": schema,
    }
    connection = await asyncpg.connect(database_url)
    try:
        await connection.execute(f"DROP SCHEMA {schema} CASCADE")
    finally:
        await connection.close()


@pytest.fixture
async def gateway() -> AsyncIterator[tuple[DatabaseGateway, Sink, Sink]]:
    spill, exports = Sink(), Sink(".sama/exports")
    gateway = DatabaseGateway(spill=spill, exports=exports, allow_private=True, require_tls=False)
    yield gateway, spill, exports
    await gateway.aclose()


def at(settings: DbSettings) -> Target:
    """The target of a connection with no SSH tunnel."""
    return Target(settings.host, settings.port)


def postgres() -> Manifest:
    found = Catalog.load(CATALOG).get("postgres")
    assert found is not None
    return found


CLAIMS = RunTokenClaims(
    org_id=str(uuid.uuid4()),
    workspace_id=str(uuid.uuid4()),
    session_id=str(uuid.uuid4()),
    run_id=str(uuid.uuid4()),
    token_id="t",
    expires_at=2_000_000_000,
)


async def run(
    gateway: DatabaseGateway,
    values: Mapping[str, str],
    operation: str,
    connection_id: uuid.UUID | None = None,
    **arguments: Any,
) -> str:
    manifest = postgres()
    tool = next(t for t in manifest.tools if t.name == f"db_postgres_{operation}")
    return await gateway.run(
        CLAIMS, connection_id or uuid.uuid4(), manifest, tool, values, arguments
    )


async def test_query_runs_in_a_read_only_transaction(outside: dict[str, str], gateway: Any) -> None:
    gw, _, _ = gateway
    schema = outside["schemas"]
    answer = await run(gw, outside, "query", sql="select current_setting('transaction_read_only')")
    assert '["on"]' in answer
    # A function that writes passes the SQL check, and the database refuses it.
    with pytest.raises(DriverError, match="read-only transaction"):
        await run(gw, outside, "query", sql=f"select {schema}.sneaky()")
    with pytest.raises(DriverError, match="read-only transaction"):
        await run(gw, outside, "query", sql=f"select nextval('{schema}.counter')")
    # Nothing changed.
    count = await run(gw, outside, "query", sql=f"select count(*) as n from {schema}.orders")
    assert "[2]" in count


@pytest.mark.parametrize(
    "sql",
    [
        "insert into {s}.orders values (5, 1, 1)",
        "update {s}.orders set total = 0",
        "delete from {s}.orders",
        "with x as (delete from {s}.orders returning *) select * from x",
        "select 1; delete from {s}.orders",
        "select 1; select 2",
        "set transaction read write",
        "create table {s}.t (a int)",
    ],
)
async def test_query_rejects_a_write_or_a_second_statement(
    outside: dict[str, str], gateway: Any, sql: str
) -> None:
    gw, _, _ = gateway
    schema = outside["schemas"]
    with pytest.raises(DriverError, match="the SQL was not run"):
        await run(gw, outside, "query", sql=sql.format(s=schema))
    count = await run(gw, outside, "query", sql=f"select count(*) as n from {schema}.orders")
    assert "[2]" in count


async def test_ddl_is_denied_by_default(outside: dict[str, str], gateway: Any) -> None:
    gw, _, _ = gateway
    schema = outside["schemas"]
    writes = {**outside, "writes": "on"}
    for sql in (f"create table {schema}.t (a int)", f"drop table {schema}.orders"):
        with pytest.raises(DriverError, match="DDL is off"):
            await run(gw, writes, "execute", sql=sql)
    done = await run(gw, {**writes, "ddl": "on"}, "execute", sql=f"create table {schema}.t (a int)")
    assert "CREATE TABLE" in done
    # Without "Allow writes", execute does nothing.
    with pytest.raises(DriverError, match="writes are off"):
        await run(gw, outside, "execute", sql=f"delete from {schema}.orders")


async def test_execute_changes_rows_and_shows_returning(
    outside: dict[str, str], gateway: Any
) -> None:
    gw, _, _ = gateway
    schema = outside["schemas"]
    writes = {**outside, "writes": "on"}
    done = await run(
        gw,
        writes,
        "execute",
        sql=f"update {schema}.orders set total = $1 where id = $2",
        params=["1.25", 1],
    )
    assert "UPDATE 1" in done
    shown = await run(gw, outside, "query", sql=f"select total from {schema}.orders where id = 1")
    assert '"1.25"' in shown
    # A database error does not show its detail (a key value can be in it).
    with pytest.raises(DriverError) as caught:
        await run(gw, writes, "execute", sql=f"insert into {schema}.orders values (1, 1, 1)")
    assert "23505" in str(caught.value)
    assert "Key (id)" not in str(caught.value)


async def test_a_query_stops_after_the_timeout(outside: dict[str, str]) -> None:
    gw = DatabaseGateway(allow_private=True, require_tls=False, timeout_s=1)
    try:
        started = time.monotonic()
        with pytest.raises(DriverError, match="stopped after 1 seconds"):
            await run(gw, outside, "query", sql="select pg_sleep(5)")
        assert time.monotonic() - started < 4
        # The pool is still good after the cancel.
        assert "[1]" in await run(gw, outside, "query", sql="select 1 as one")
    finally:
        await gw.aclose()


async def test_the_model_gets_1000_rows_and_the_rest_spills(
    outside: dict[str, str], gateway: Any
) -> None:
    gw, spill, _ = gateway
    schema = outside["schemas"]
    answer = await run(gw, outside, "query", sql=f"select id, name from {schema}.users order by id")
    rows = [line for line in answer.splitlines() if line.startswith("[") and "Shown" not in line]
    assert len(rows) == MAX_ROWS + 1  # the header and 1,000 rows
    assert f"Shown: 1,000 of {ROWS:,} rows" in answer
    assert len(untrusted(answer, source="connector:postgres").encode()) < MAX_OUTPUT_BYTES
    ((path, text),) = spill.files.items()
    assert path.startswith("/workdir/.sama/spill/db_postgres_query-")
    assert path in answer
    lines = list(csv.reader(io.StringIO(text)))
    assert lines[0] == ["id", "name"]
    assert len(lines) == ROWS + 1
    assert lines[-1] == [str(ROWS), f"n{ROWS}"]


async def test_the_model_gets_at_most_50_kb(outside: dict[str, str], gateway: Any) -> None:
    gw, spill, _ = gateway
    schema = outside["schemas"]
    answer = await run(
        gw, outside, "query", sql=f"select id, repeat(name, 30) as wide from {schema}.users"
    )
    wrapped = untrusted(answer, source="connector:postgres")
    assert len(wrapped.encode()) < MAX_OUTPUT_BYTES
    assert "Shown: " in answer
    shown = int(answer.split("Shown: ")[1].split(" ")[0].replace(",", ""))
    assert shown < MAX_ROWS
    assert len(spill.files) == 1


async def test_hidden_columns_never_appear(outside: dict[str, str], gateway: Any) -> None:
    gw, spill, exports = gateway
    schema = outside["schemas"]
    values = {**outside, "hidden_columns": "users.email, ssn", "writes": "on"}
    texts = [
        await run(gw, values, "schema"),
        await run(gw, values, "sample", table=f"{schema}.users", limit=5),
        await run(gw, values, "query", sql=f"select * from {schema}.users order by id"),
        await run(gw, values, "query", sql=f"select u.* from {schema}.users u where id < 3"),
        await run(gw, values, "export", sql=f"select * from {schema}.users"),
        await run(
            gw,
            values,
            "execute",
            sql=f"update {schema}.users set name = 'x' where id = 1 returning *",
        ),
    ]
    files = list(spill.files.values()) + list(exports.files.values())
    assert len(exports.files) == 1
    for text in texts + files:
        assert "hidden.example" not in text
        assert "ssn-" not in text
        assert "email" not in text.lower().replace("hidden columns", "")
    assert "bio" in texts[0]
    assert "name" in texts[2]
    for sql in (
        f"select email from {schema}.users",
        f"select id from {schema}.users where email = '{SECRET_EMAIL}'",
        f"select row_to_json(u) from {schema}.users u",
        f"select (select ssn from {schema}.users limit 1)",
    ):
        with pytest.raises(DriverError, match="the SQL was not run"):
            await run(gw, values, "query", sql=sql)
    with pytest.raises(DriverError, match="the SQL was not run"):
        await run(
            gw,
            values,
            "execute",
            sql=f"update {schema}.users set name = 'x' where email = '{SECRET_EMAIL}'",
        )


async def test_only_the_allowed_tables_show(outside: dict[str, str], gateway: Any) -> None:
    gw, _, _ = gateway
    schema = outside["schemas"]
    values = {**outside, "tables": f"{schema}.orders"}
    described = await run(gw, values, "schema")
    assert f"{schema}.orders" in described
    assert f"{schema}.users" not in described
    # The search path is the schema of the allowed tables.
    assert "[1]\n[2]" in await run(gw, values, "query", sql="select id from orders order by id")
    for sql in (f"select * from {schema}.users", "select * from pg_catalog.pg_class"):
        with pytest.raises(DriverError, match="allowed"):
            await run(gw, values, "query", sql=sql)
    with pytest.raises(DriverError, match="allowed"):
        await run(gw, values, "sample", table=f"{schema}.users")


async def test_names_follow_the_scopes_and_quoting_of_postgres(
    outside: dict[str, str], gateway: Any
) -> None:
    """Review round 2: aliases, CTEs, and quoted or unqualified names cannot reach a table or
    a hidden column that the connection does not allow."""
    gw, _, _ = gateway
    schema = outside["schemas"]
    hidden = {**outside, "hidden_columns": "users.email, ssn"}
    for sql in (
        f"select * from {schema}.users u(id, name, contact)",
        f"select * from {schema}.users u(id, name, contact, bio_alias, other)",
        f"select x.contact from lateral (select * from {schema}.users) as x(id, name, contact)",
    ):
        with pytest.raises(DriverError, match="column names after an alias"):
            await run(gw, hidden, "query", sql=sql)
        with pytest.raises(DriverError, match="column names after an alias"):
            await run(gw, hidden, "export", sql=sql)
    shadow = await run(
        gw,
        hidden,
        "query",
        sql=(
            f"select * from {schema}.users where id < 3 and exists "
            f"(select 1 from {schema}.orders users)"
        ),
    )
    assert "hidden.example" not in shadow
    assert "ssn-" not in shadow
    only_orders = {**outside, "tables": f"{schema}.orders"}
    with pytest.raises(DriverError, match="allowed"):
        await run(
            gw,
            only_orders,
            "query",
            sql=(
                f"select * from {schema}.users where exists "
                f"(with users as (select 1) select * from users)"
            ),
        )
    with pytest.raises(DriverError, match="allowed"):
        await run(
            gw, {**outside, "tables": f"{schema}.users"}, "query", sql='select * from "USERS"'
        )
    connection = await asyncpg.connect(
        f"postgresql://{outside['user']}:{outside['password']}@{outside['host']}:"
        f"{outside['port']}/{outside['database']}"
    )
    other = f"{schema}_b"
    try:
        await connection.execute(
            f"CREATE SCHEMA {other}; CREATE TABLE {other}.orders (secret text);"
            f"INSERT INTO {other}.orders VALUES ('other-schema-row');"
            f"CREATE TABLE {other}.users (id int);"
        )
        two = {
            **outside,
            "schemas": f"{other}, {schema}",
            "tables": f"{schema}.orders, {other}.users",
        }
        with pytest.raises(DriverError, match="schema of orders"):
            await run(gw, two, "query", sql="select * from orders")
        assert "[1]" in await run(gw, two, "query", sql=f"select id from {schema}.orders")
    finally:
        await connection.execute(f"DROP SCHEMA {other} CASCADE")
        await connection.close()


async def test_a_cut_value_keeps_the_full_value_in_a_file(
    outside: dict[str, str], gateway: Any
) -> None:
    gw, spill, _ = gateway
    answer = await run(gw, outside, "query", sql="select repeat('x', 5000) as long")
    assert "...[cut]" in answer
    ((path, text),) = spill.files.items()
    assert path in answer
    assert "full values" in answer
    assert list(csv.reader(io.StringIO(text))) == [["long"], ["x" * 5000]]


async def test_the_files_stay_in_their_byte_limits(
    outside: dict[str, str], gateway: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw, spill, exports = gateway
    monkeypatch.setattr(database, "SPILL_MAX_BYTES", 200)
    monkeypatch.setattr(database, "EXPORT_MAX_BYTES", 200)
    # Multibyte text: 3 bytes for each character.
    sql = "select i, repeat('\u20ac', 20) as euro from generate_series(1, 50) as i"
    answer = await run(gw, outside, "query", sql=f"{sql} union all select 51, repeat('x', 50000)")
    ((_, spilled),) = spill.files.items()
    assert len(spilled.encode()) <= 200
    assert "+ row(s)" in answer
    exported = await run(gw, outside, "export", sql=sql)
    ((_, text),) = exports.files.items()
    assert len(text.encode()) <= 200
    assert "The export stopped at" in exported
    one_big = await run(gw, outside, "export", sql="select repeat('x', 5000) as big")
    assert "Exported 0 rows" in one_big


async def test_an_idle_pool_closes_and_a_new_one_opens(outside: dict[str, str]) -> None:
    now = [1_000.0]
    gw = DatabaseGateway(allow_private=True, require_tls=False, clock=lambda: now[0])
    connection_id = uuid.uuid4()
    try:
        await run(gw, outside, "query", connection_id, sql="select 1")
        first = gw._pools[str(connection_id)].pool
        now[0] += POOL_IDLE_S - 1
        await run(gw, outside, "query", connection_id, sql="select 1")
        assert gw._pools[str(connection_id)].pool is first
        now[0] += POOL_IDLE_S + 1
        await run(gw, outside, "query", connection_id, sql="select 1")
        assert gw._pools[str(connection_id)].pool is not first
        assert first.is_closing()
        # Other settings: a new pool. The old pool closes when its last call ends.
        settings = DbSettings.parse(postgres(), outside)
        borrowed = await gw._borrow(connection_id, settings, at(settings))
        changed = DbSettings.parse(postgres(), {**outside, "hidden_columns": "ssn"})
        replaced = await gw._borrow(connection_id, changed, at(changed))
        assert replaced.pool is not borrowed.pool
        assert borrowed.retired
        assert not borrowed.pool.is_closing()
        await gw._give_back(borrowed)
        assert borrowed.pool.is_closing()
        await gw._give_back(replaced)
    finally:
        await gw.aclose()


async def test_a_cancel_while_an_old_pool_closes_gives_the_pool_back(
    outside: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    gw = DatabaseGateway(allow_private=True, require_tls=False)
    connection_id = uuid.uuid4()
    started, release = asyncio.Event(), asyncio.Event()
    real_close = database._close

    async def slow_close(pool: asyncpg.Pool) -> None:
        started.set()
        await release.wait()
        await real_close(pool)

    try:
        await run(gw, outside, "query", connection_id, sql="select 1")
        monkeypatch.setattr(database, "_close", slow_close)
        changed = DbSettings.parse(postgres(), {**outside, "hidden_columns": "ssn"})
        task = asyncio.create_task(gw._borrow(connection_id, changed, at(changed)))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gw._pools[str(connection_id)].borrowers == 0
        release.set()
        await asyncio.sleep(0)
    finally:
        release.set()
        await gw.aclose()


async def test_export_writes_a_csv_file(outside: dict[str, str], gateway: Any) -> None:
    gw, _, exports = gateway
    schema = outside["schemas"]
    answer = await run(gw, outside, "export", sql=f"select id, name from {schema}.users")
    ((path, text),) = exports.files.items()
    assert path.startswith("/workdir/.sama/exports/db_postgres_export-")
    assert path in answer
    assert f"Exported {ROWS:,} rows" in answer
    assert len(text.splitlines()) == ROWS + 1
    without = DatabaseGateway(allow_private=True, require_tls=False)
    with pytest.raises(DriverError, match="sandbox"):
        await run(without, outside, "export", sql="select 1")
    await without.aclose()


async def test_the_connection_test_uses_the_gateway(outside: dict[str, str], gateway: Any) -> None:
    gw, _, _ = gateway
    await gw.test(postgres(), outside)
    with pytest.raises(DriverError, match=r"refused the login|answered"):
        await gw.test(postgres(), {**outside, "password": "wrong"})
    public_only = DatabaseGateway()
    with pytest.raises(DriverError, match="public"):
        await public_only.test(postgres(), outside)


# -- with the connection service and the gate ----------------------------------------------


@pytest.fixture
async def service(engine: AsyncEngine, gateway: Any) -> AsyncIterator[ConnectionService]:
    gw, _, _ = gateway
    vault = Vault(engine, VaultKey(b"c" * KEY_BYTES))
    service = ConnectionService(
        engine,
        Catalog.load(CATALOG),
        keys=OrgKeys(vault, {SecretName.COMPOSIO_API_KEY: SecretStr("ck")}),
        vault=vault,
        composio=ComposioClient(transport=httpx.MockTransport(FakeComposio())),
        drivers={},
        web_base=WEB,
        database=gw,
    )
    yield service
    await service.aclose()


async def connect(service: ConnectionService, org: Org1, values: Mapping[str, str]) -> uuid.UUID:
    admin = member(org, "max", admin=True)
    started = await service.start(org.ids["org"], org.ids["ws"], admin, "postgres", "workspace")
    view = await service.submit_form(
        org.ids["org"], started.connection.id, dict(values), actor=admin.user_id
    )
    assert view.state == "connected", view.last_error
    assert "password" not in view.fields
    return view.id


async def test_execute_shows_only_with_writes_on(
    org: Org1, service: ConnectionService, outside: dict[str, str]
) -> None:
    claims = org.claims()
    names = [f"db_postgres_{op}" for op in ("schema", "query", "execute")]
    assert await service.usable_tools(claims, names) == frozenset()
    await connect(service, org, outside)
    assert await service.usable_tools(claims, names) == {"db_postgres_schema", "db_postgres_query"}
    answer = await service.call(claims, "db_postgres_query", {"sql": "select 41 + 1 as n"})
    assert "[42]" in answer
    assert "untrusted" in answer
    with pytest.raises(ConnectorError, match="writes are off"):
        await service.call(claims, "db_postgres_execute", {"sql": "delete from x"})
    await connect(service, org, {**outside, "writes": "on"})
    assert "db_postgres_execute" in await service.usable_tools(claims, names)


async def test_execute_needs_an_approval_that_shows_the_sql(
    engine: AsyncEngine, org: Org1, service: ConnectionService, outside: dict[str, str]
) -> None:
    await connect(service, org, {**outside, "writes": "on"})
    deferred = DeferredCatalog()
    service.register_tools(deferred)
    gate = GateService(engine, extra_toolsets=deferred)
    await grant(engine, org, "tool_search", "integrations")
    schema = outside["schemas"]
    sql = (
        f"update {schema}.orders set total = total * 2 "
        + " ".join(f"/* {i:03d} */" for i in range(80))
        + " where id = $1"
    )
    assert len(sql) > 700
    call = {"name": "db_postgres_execute", "arguments": {"sql": sql, "params": [1]}}
    verdict = await gate.check(org.claims(), "c1", "mcp__sama__tool_call", call)
    assert (verdict.action, verdict.step) == ("ask", GateStep.POLICY)
    row = await approval(engine, org, "c1")
    assert row.tool_name == "db_postgres_execute"
    assert row.arguments["sql"] == sql
    card = SimpleNamespace(
        tool_name=row.tool_name, reason=row.reason, answerers=None, arguments=row.arguments
    )
    assert sql in approval_text(card)
    # A read tool does not ask.
    read = {"name": "db_postgres_query", "arguments": {"sql": "select 1"}}
    verdict = await gate.check(org.claims(), "c2", "mcp__sama__tool_call", read)
    assert verdict.action == "allow"
    # "Always allow" writes no rule: the next call, with other SQL, asks again.
    await gate.decide_approval(
        org_id=org.ids["org"],
        run_id=org.ids["run"],
        call_id="c1",
        user_id=org.ids["mia"],
        approve=True,
        always_allow=True,
    )
    other = {"name": "db_postgres_execute", "arguments": {"sql": "delete from orders"}}
    verdict = await gate.check(org.claims(), "c3", "mcp__sama__tool_call", other)
    assert (verdict.action, verdict.step) == ("ask", GateStep.POLICY)
    # An allow rule, exact or with a prefix, at the user or the org level, does not remove
    # the ask. A deny rule still wins.
    for number, (tool, user) in enumerate(
        (("db_postgres_execute", "mia"), ("db_*", None), ("mcp__sama__tool_call", None))
    ):
        await add_rule(engine, org, tool, "allow", user)
        verdict = await gate.check(org.claims(), f"c{4 + number}", "mcp__sama__tool_call", other)
        assert (verdict.action, verdict.step) == ("ask", GateStep.POLICY), tool
    await add_rule(engine, org, "db_postgres_execute", "deny")
    verdict = await gate.check(org.claims(), "c9", "mcp__sama__tool_call", other)
    assert verdict.action == "deny"
