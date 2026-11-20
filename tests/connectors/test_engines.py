"""The checks of the M4.13 engines without a server (docs/architecture.md, Section 10.4, "What
M4.13 builds"): the SQL dialects, the MongoDB reads, the ClickHouse parameters and errors, and
the SSH tunnel pool (Section 10.6)."""

# ruff: noqa: S608  (the tests build SQL text for the check; nothing runs it)

from __future__ import annotations

from typing import Any

import asyncssh
import pytest

from sama.connectors.engines.clickhouse import _error, bind
from sama.connectors.http_api import DriverError
from sama.connectors.mongoquery import drop_hidden, read_of
from sama.connectors.sql import (
    CLICKHOUSE,
    MYSQL,
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


# -- SQL dialects ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dialect", "policy"),
    [(MYSQL, MYSQL_POLICY), (TSQL, TSQL_POLICY), (CLICKHOUSE, CLICKHOUSE_POLICY)],
)
@pytest.mark.parametrize(
    "sql",
    ["insert into users values (1)", "delete from users", "explain select 1", "drop table users"],
)
def test_query_mode_runs_only_select_in_every_dialect(dialect: Any, policy: Any, sql: str) -> None:
    with pytest.raises(SqlRejectedError):
        check(sql, "query", policy, dialect)


def test_query_mode_takes_a_select_in_every_dialect() -> None:
    assert check("select 1", "query", MYSQL_POLICY, MYSQL).kind == "select"
    assert check("select 1", "query", TSQL_POLICY, TSQL).kind == "select"
    assert check("select 1", "query", CLICKHOUSE_POLICY, CLICKHOUSE).kind == "select"


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
