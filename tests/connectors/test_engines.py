"""The checks of the M4.13 engines without a server (docs/architecture.md, Section 10.4, "What
M4.13 builds"): the SQL dialects, the MongoDB reads, the ClickHouse parameters and errors, and
the SSH tunnel pool (Section 10.6)."""

# ruff: noqa: S608  (the tests build SQL text for the check; nothing runs it)

from __future__ import annotations

import asyncio
import ssl
from typing import Any

import aiomysql
import asyncssh
import pytest
from pymysql.constants import CLIENT

from sama.connectors.dbcore import Target
from sama.connectors.dbsettings import DbSettings
from sama.connectors.engines import EngineContext
from sama.connectors.engines.clickhouse import _error, bind
from sama.connectors.engines.mysql import _Connection as MySqlConnection
from sama.connectors.engines.mysql import open_session as mysql_session
from sama.connectors.http_api import DriverError
from sama.connectors.mongoquery import drop_hidden, read_of
from sama.connectors.sql import (
    BIGQUERY,
    CLICKHOUSE,
    MYSQL,
    SNOWFLAKE,
    TSQL,
    SqlRejectedError,
    check,
    policy_of,
)
from sama.connectors.tunnel import Tunnels, host_key_matches

MYSQL_POLICY = policy_of(fixed_path=("shop",))
CLICKHOUSE_POLICY = policy_of(fixed_path=("shop",))
TSQL_POLICY = policy_of(fixed_path=(), catalog="shop")
HIDDEN_TSQL = policy_of(fixed_path=(), catalog="shop", hidden_columns=["ssn"])
BIGQUERY_POLICY = policy_of(fixed_path=("sales",), catalog="proj")
SNOWFLAKE_POLICY = policy_of(fixed_path=("PUBLIC",), catalog="SHOP")
ALL = [
    (MYSQL, MYSQL_POLICY),
    (TSQL, TSQL_POLICY),
    (CLICKHOUSE, CLICKHOUSE_POLICY),
    (BIGQUERY, BIGQUERY_POLICY),
    (SNOWFLAKE, SNOWFLAKE_POLICY),
]


# -- SQL dialects ---------------------------------------------------------------------------


@pytest.mark.parametrize(("dialect", "policy"), ALL)
@pytest.mark.parametrize(
    "sql",
    ["insert into users values (1)", "delete from users", "explain select 1", "drop table users"],
)
def test_query_mode_runs_only_select_in_every_dialect(dialect: Any, policy: Any, sql: str) -> None:
    with pytest.raises(SqlRejectedError):
        check(sql, "query", policy, dialect)


@pytest.mark.parametrize(("dialect", "policy"), ALL)
def test_query_mode_takes_a_select_in_every_dialect(dialect: Any, policy: Any) -> None:
    assert check("select 1", "query", policy, dialect).kind == "select"


@pytest.mark.parametrize(("dialect", "policy"), ALL)
@pytest.mark.parametrize(
    "sql",
    ["select 1 /* c */", "select 1 -- c", "select 1 /*! , ssn */ from users"],
)
def test_comments_are_refused_in_every_dialect(dialect: Any, policy: Any, sql: str) -> None:
    """A server can read a comment as SQL (MySQL ``/*! */``, MariaDB ``/*M! */``, hints)."""
    with pytest.raises(SqlRejectedError, match="comments"):
        check(sql, "query", policy, dialect)


@pytest.mark.parametrize(
    "sql",
    [
        "select 1 /*! , ssn as leaked */ from users",
        "select 1 /*M! , ssn */ from users",
        "select 1 /*+ SET_VAR(max_execution_time=0) */ from users",
        "select 1 # c\n from users",
        "select 1 /*! ; commit */",
    ],
)
def test_mysql_executable_comments_and_hints_are_refused(sql: str) -> None:
    hidden = policy_of(fixed_path=("shop",), hidden_columns=["ssn"])
    with pytest.raises(SqlRejectedError, match="comments"):
        check(sql, "query", hidden, MYSQL)
    # A comment-like text inside a string is a value, not a comment.
    check("select '/*! x */' from users", "query", hidden, MYSQL)


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        (SNOWFLAKE, "select EMAIL from IDENTIFIER('PRIVATE.USERS')"),
        (SNOWFLAKE, "select IDENTIFIER('SSN') from USERS"),
        (SNOWFLAKE, "select * from TABLE(TO_QUERY('select SSN from PRIVATE.USERS'))"),
        (SNOWFLAKE, "select * from TABLE(RESULT_SCAN(LAST_QUERY_ID()))"),
        (CLICKHOUSE, "select * from deltaLakeS3('https://example.com/x')"),
        (CLICKHOUSE, "select * from deltaLakeCluster('c', 'https://example.com/x')"),
        (CLICKHOUSE, "select * from someNewTableFunction(1)"),
        (TSQL, "select * from sys.fn_xe_file_target_read_file('a', null, null, null)"),
        (TSQL, "select * from dbo.t cross apply sys.dm_exec_sql_text(t.h) q"),
        (MYSQL, "select * from some_table_function(1) f"),
    ],
)
def test_dynamic_names_and_unlisted_table_functions_are_refused(dialect: Any, sql: str) -> None:
    """A table function or a name from a text can read what the policy does not see."""
    policy = dict(ALL)[dialect]
    with pytest.raises(SqlRejectedError):
        check(sql, "query", policy, dialect)


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        (SNOWFLAKE, "select f.value from T, lateral flatten(input => T.X) f"),
        (SNOWFLAKE, "select * from TABLE(GENERATOR(ROWCOUNT => 3))"),
        (CLICKHOUSE, "select * from numbers(3)"),
        (TSQL, "select * from openjson('[1]')"),
        (MYSQL, "select * from json_table('[1]', '$[*]' columns (a int path '$')) j"),
        (BIGQUERY, "select * from unnest([1, 2])"),
    ],
)
def test_listed_table_functions_are_allowed(dialect: Any, sql: str) -> None:
    check(sql, "query", dict(ALL)[dialect], dialect)


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        (TSQL, "select next value for dbo.order_seq"),
        (SNOWFLAKE, "select ORDER_SEQ.NEXTVAL"),
        (SNOWFLAKE, "select GETNEXTVAL(ORDER_SEQ)"),
        (MYSQL, "select nextval(order_seq)"),
        (MYSQL, "select setval(order_seq, 1)"),
    ],
)
def test_query_mode_refuses_sequence_changes(dialect: Any, sql: str) -> None:
    """A sequence value is not rolled back, so it is a write."""
    with pytest.raises(SqlRejectedError):
        check(sql, "query", dict(ALL)[dialect], dialect)


def test_bigquery_select_as_struct_is_refused_when_columns_are_hidden() -> None:
    hidden = policy_of(fixed_path=("sales",), catalog="proj", hidden_columns=["email"])
    check("select as struct * from users", "query", BIGQUERY_POLICY, BIGQUERY)
    for sql in ("select as struct * from users", "select as value u from users u"):
        with pytest.raises(SqlRejectedError):
            check(sql, "query", hidden, BIGQUERY)


def test_snowflake_column_positions_are_refused_when_columns_are_hidden() -> None:
    hidden = policy_of(fixed_path=("PUBLIC",), catalog="SHOP", hidden_columns=["email"])
    check("select $2 from USERS", "query", SNOWFLAKE_POLICY, SNOWFLAKE)
    with pytest.raises(SqlRejectedError, match="position"):
        check("select $2 from USERS", "query", hidden, SNOWFLAKE)


@pytest.mark.parametrize(("dialect", "default"), [(MYSQL, "shop"), (CLICKHOUSE, "shop")])
def test_a_table_name_alone_is_checked_in_the_default_schema(dialect: Any, default: str) -> None:
    """``tables=orders`` with ``schemas=dbo``: a name alone is in the default schema, which
    must be an allowed schema."""
    outside = policy_of(schemas=["other"], tables=["orders"], fixed_path=(default,))
    with pytest.raises(SqlRejectedError):
        check("select * from orders", "query", outside, dialect)
    inside = policy_of(schemas=[default], tables=["orders"], fixed_path=(default,))
    check("select * from orders", "query", inside, dialect)


def test_sql_server_needs_the_schema_with_a_schema_list() -> None:
    policy = policy_of(schemas=["dbo"], tables=["orders"], fixed_path=(), catalog="shop")
    with pytest.raises(SqlRejectedError, match="give the schema"):
        check("select * from orders", "query", policy, TSQL)
    check("select * from dbo.orders", "query", policy, TSQL)
    # Only a table list (every schema): the name alone is allowed in each schema.
    any_schema = policy_of(tables=["orders"], fixed_path=(), catalog="shop")
    check("select * from orders", "query", any_schema, TSQL)


@pytest.mark.parametrize(
    "sql",
    [
        "select * from users format CSV",
        "select columns('ssn') from users",
        "select dictGet('d', 'a', 1)",
        "select joinGet('j', 'a', 1)",
    ],
)
def test_clickhouse_refuses_format_and_hidden_reads(sql: str) -> None:
    with pytest.raises(SqlRejectedError):
        check(sql, "query", CLICKHOUSE_POLICY, CLICKHOUSE)


def test_for_json_is_refused_when_columns_are_hidden() -> None:
    check("select id from users for json path", "query", TSQL_POLICY, TSQL)
    with pytest.raises(SqlRejectedError, match="FOR JSON"):
        check("select id from users for json path", "query", HIDDEN_TSQL, TSQL)


def test_a_star_in_a_value_subquery_is_refused_when_columns_are_hidden() -> None:
    hidden = policy_of(fixed_path=("shop",), hidden_columns=["ssn"])
    with pytest.raises(SqlRejectedError):
        check("select (select u.* limit 1) from users u", "query", hidden, MYSQL)


@pytest.mark.parametrize(
    ("sql", "allowed"),
    [
        ("select * from dbo.orders", True),
        ("select * from shop.dbo.orders", True),
        ("select * from master.dbo.spt_values", False),
        ("select * from orders", False),
    ],
)
def test_a_restricted_sql_server_connection_needs_the_schema(sql: str, allowed: bool) -> None:
    restricted = policy_of(fixed_path=(), catalog="shop", tables=["dbo.orders"])
    if allowed:
        check(sql, "query", restricted, TSQL)
    else:
        with pytest.raises(SqlRejectedError):
            check(sql, "query", restricted, TSQL)


# -- MongoDB reads --------------------------------------------------------------------------


MONGO_HIDDEN = policy_of(hidden_columns=["users.email", "ssn"], tables=["users", "orders"])


def test_a_find_read_is_checked() -> None:
    read = read_of(
        {"collection": "users", "filter": {"age": {"$gt": 3}}, "sort": {"name": 1}, "limit": 5},
        MONGO_HIDDEN,
        limit_max=1_000,
    )
    assert read.collection == "users"
    assert read.limit == 5
    assert read.hidden == frozenset({"email", "ssn"})


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        ({"collection": "system.users"}, "not a collection"),
        ({"collection": "secrets"}, "allowed"),
        ({"collection": "users", "limit": 5_000}, "limit"),
        ({"collection": "users", "filter": {"$where": "1"}}, "not allowed"),
        ({"collection": "users", "pipeline": [{"$out": "x"}]}, "not allowed"),
        ({"collection": "users", "pipeline": [{"$merge": "x"}]}, "not allowed"),
        ({"collection": "users", "pipeline": [{"$unionWith": "secrets"}]}, "allowed"),
        ({"collection": "users", "filter": {"email": "a"}}, "hidden"),
        ({"collection": "users", "pipeline": [{"$project": {"a": "$$ROOT"}}]}, "not allowed"),
        ({"collection": "users", "pipeline": [{"$match": {}}], "filter": {"a": 1}}, "not both"),
    ],
)
def test_a_mongodb_read_is_refused(arguments: dict[str, Any], reason: str) -> None:
    with pytest.raises(DriverError, match=reason):
        read_of(arguments, MONGO_HIDDEN, limit_max=1_000)


def test_hidden_fields_are_dropped_at_every_depth() -> None:
    value = {"name": "a", "SSN": 1, "p": {"ssn": 2, "x": [{"email": 3, "y": 4}]}}
    assert drop_hidden(value, frozenset({"ssn", "email"})) == {"name": "a", "p": {"x": [{"y": 4}]}}


# -- ClickHouse -----------------------------------------------------------------------------


def test_clickhouse_binds_typed_parameters() -> None:
    sql, query = bind("select ? , ?, ?, ?, ? from t where s = '?'", [None, True, 7, 1.5, "a\tb"])
    assert sql == "select {p0:Nullable(String)} , {p1:Bool}, {p2:Int64}, {p3:Float64}, " + (
        "{p4:String} from t where s = '?'"
    )
    assert query == {
        "param_p0": "\\N",
        "param_p1": "true",
        "param_p2": "7",
        "param_p3": "1.5",
        "param_p4": "a\\tb",
    }


def test_clickhouse_errors_have_fixed_texts() -> None:
    assert "time limit" in str(_error("159", "Code: 159. DB::Exception: Timeout exceeded"))
    assert "refused the login" in str(_error(None, "Code: 516. DB::Exception: bad password"))
    error = _error(None, '["x"]\n{"exception": "Code: 62. DB::Exception: Syntax error"}')
    assert str(error) == "the database answered: Code: 62. DB::Exception: Syntax error"


# -- SSH tunnels ----------------------------------------------------------------------------


def test_a_host_key_matches_by_key_or_by_fingerprint() -> None:
    key = asyncssh.generate_private_key("ssh-ed25519")
    other = asyncssh.generate_private_key("ssh-ed25519")
    public = key.export_public_key().decode().strip()
    assert host_key_matches(public, key)
    assert host_key_matches(key.get_fingerprint("sha256"), key)
    assert not host_key_matches(public, other)
    assert not host_key_matches(other.get_fingerprint("sha256"), key)
    assert not host_key_matches("ssh-ed25519 not-base64", key)


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _Listener:
    def __init__(self, port: int) -> None:
        self.port = port
        self.closed = False

    def get_port(self) -> int:
        return self.port

    def close(self) -> None:
        self.closed = True


class _Opener:
    def __init__(self) -> None:
        self.opened: list[tuple[_Connection, _Listener]] = []

    async def __call__(self) -> tuple[Any, Any]:
        pair = (_Connection(), _Listener(40_000 + len(self.opened)))
        self.opened.append(pair)
        return pair


async def test_a_tunnel_is_kept_and_closes_when_idle() -> None:
    now = [0.0]
    tunnels = Tunnels(clock=lambda: now[0], idle_s=10)
    opener = _Opener()
    async with tunnels.route("a", "f1", opener) as target:
        assert (target.host, target.port) == ("127.0.0.1", 40_000)
        # While it is used, the tunnel does not close, also after the idle time.
        now[0] = 100
        async with tunnels.route("b", "f1", opener):
            pass
        assert not opener.opened[0][0].closed
    async with tunnels.route("a", "f1", opener) as again:
        assert again.port == 40_000
    assert len(opener.opened) == 2
    now[0] = 200
    async with tunnels.route("c", "f1", opener):
        pass
    assert opener.opened[0][0].closed
    assert opener.opened[0][1].closed
    assert opener.opened[1][0].closed
    assert tunnels.open_count() == 1
    await tunnels.aclose()
    assert tunnels.open_count() == 0


async def test_a_changed_form_opens_a_new_tunnel() -> None:
    tunnels = Tunnels()
    opener = _Opener()
    async with tunnels.route("a", "f1", opener):
        pass
    async with tunnels.route("a", "f2", opener) as target:
        assert target.port == 40_001
    assert opener.opened[0][0].closed
    await tunnels.discard("a")
    assert opener.opened[1][0].closed
    assert tunnels.open_count() == 0


# -- MySQL TLS ------------------------------------------------------------------------------


async def test_mysql_does_not_send_the_login_without_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    """aiomysql sends the login in plain text when the server does not offer TLS. The
    gateway connection stops first, and it does not ask for multi-statements."""
    tls = ssl.create_default_context()
    connection = MySqlConnection(host="db.example.com", user="u", password="p", ssl=tls)
    connection.server_capabilities = 0
    with pytest.raises(DriverError, match="does not offer TLS"):
        await connection._request_authentication()
    sent: list[int] = []

    async def fake_auth(self: Any) -> None:
        sent.append(self.client_flag)

    monkeypatch.setattr(aiomysql.Connection, "_request_authentication", fake_auth)
    plain = MySqlConnection(host="db.example.com", user="u", password="p")
    plain.server_capabilities = 0
    assert plain.client_flag & CLIENT.MULTI_STATEMENTS
    await plain._request_authentication()
    assert sent == [plain.client_flag]
    assert not plain.client_flag & CLIENT.MULTI_STATEMENTS


def _handshake(capabilities: int) -> bytes:
    """A MySQL server greeting (protocol 10) with the given capability flags."""
    body = (
        b"\x0a8.0.0-fake\x00"
        + (1).to_bytes(4, "little")
        + b"abcdefgh\x00"
        + (capabilities & 0xFFFF).to_bytes(2, "little")
        + b"\x2d"
        + b"\x02\x00"
        + (capabilities >> 16).to_bytes(2, "little")
        + b"\x15"
        + b"\x00" * 10
        + b"ijklmnopqrst\x00"
        + b"mysql_native_password\x00"
    )
    return len(body).to_bytes(3, "little") + b"\x00" + body


async def test_a_mysql_server_without_tls_gets_no_login() -> None:
    """The gateway stops at the greeting of a server that does not offer TLS: the server
    gets no bytes, so the password does not go out in plain text."""
    received: list[bytes] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        no_tls = CLIENT.PROTOCOL_41 | CLIENT.SECURE_CONNECTION | CLIENT.PLUGIN_AUTH
        writer.write(_handshake(no_tls))
        await writer.drain()
        try:
            received.append(await asyncio.wait_for(reader.read(4096), 2))
        except TimeoutError:
            received.append(b"")
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    settings = DbSettings(
        host="db.example.com",
        port=port,
        database="shop",
        user="u",
        password="secret-password",
        sslmode="require",
        writes=False,
        policy=MYSQL_POLICY,
        engine="mysql",
    )
    async with server:
        with pytest.raises(DriverError, match="does not offer TLS"):
            async with mysql_session(
                settings, Target("127.0.0.1", port), EngineContext(timeout_s=5), write=False
            ):
                pass
        for _ in range(100):
            if received:
                break
            await asyncio.sleep(0.01)
    assert received == [b""]
