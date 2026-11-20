"""The Snowflake engine (docs/architecture.md, Section 10.4), with a fake SQL API: no account."""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import hashlib
import ipaddress
import json
from collections.abc import AsyncIterator, Callable, Sequence
from decimal import Decimal
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from sama.connectors.dbcore import RowCollector, SqlSession, Target
from sama.connectors.dbsettings import DbSettings
from sama.connectors.engines import EngineContext, open_sql, snowflake
from sama.connectors.http_api import DriverError, DriverUnavailableError
from sama.connectors.manifest import parse
from sama.tools.outbound import IPAddress
from tests.support import wire_transport

HOST = "myorg-acct.snowflakecomputing.com"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
MANIFEST = parse(
    """
name: snow
title: Snow
description: A Snowflake warehouse.
kind: database
database:
  engine: snowflake
"""
)
VALUES = {
    "account": "myorg-acct",
    "user": "agent",
    "private_key": PEM,
    "warehouse": "wh",
    "database": "sales",
    "schema": "public",
    "role": "analyst",
}
TARGET = Target("127.0.0.1", 0)


def settings(**changes: str) -> DbSettings:
    return DbSettings.parse(MANIFEST, {**VALUES, **changes})


async def public(host: str, port: int) -> Sequence[IPAddress]:
    return [ipaddress.ip_address("52.1.2.3")]


def reply(status: int, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, json=body)


def meta(*columns: tuple[str, str, int], parts: int = 1) -> dict[str, Any]:
    return {
        "rowType": [{"name": n, "type": t, "scale": s} for n, t, s in columns],
        "partitionInfo": [{"rowCount": 1} for _ in range(parts)],
    }


class Fake:
    """A fake SQL API. ``handler`` answers a request that the fake does not know."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["host"] == HOST
        return self.handler(request)

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]


@contextlib.asynccontextmanager
async def session(
    fake: Fake, *, write: bool = False, timeout_s: float = 5.0, **changes: str
) -> AsyncIterator[SqlSession]:
    context = EngineContext(timeout_s=timeout_s, resolver=public, transport=wire_transport(fake))
    async with snowflake.open_session(settings(**changes), TARGET, context, write=write) as s:
        yield s


def collector() -> RowCollector:
    return RowCollector(frozenset(), 100, 1_000_000, keep_csv=True)


def body_of(request: httpx.Request) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(request.content)
    return parsed


async def test_the_jwt_has_the_claims_of_snowflake() -> None:
    fake = Fake(lambda r: reply(200, {"resultSetMetaData": meta(), "data": []}))
    async with session(fake) as s:
        await s.rows("SELECT 1", [], collector())
    request = fake.requests[0]
    assert request.headers["x-snowflake-authorization-token-type"] == "KEYPAIR_JWT"
    assert request.headers["accept"] == "application/json"
    bearer = request.headers["authorization"]
    assert bearer.startswith("Bearer ")
    public_der = KEY.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    fingerprint = base64.b64encode(hashlib.sha256(public_der).digest()).decode()
    claims = jwt.decode(bearer[7:], KEY.public_key(), algorithms=["RS256"])
    assert claims["iss"] == f"MYORG-ACCT.AGENT.SHA256:{fingerprint}"
    assert claims["sub"] == "MYORG-ACCT.AGENT"
    assert 0 < claims["exp"] - claims["iat"] <= 3_600


def test_the_account_has_no_region_part_in_the_jwt() -> None:
    assert snowflake._account(settings(account="xy12345.eu-west-1.aws")) == "XY12345"
    assert snowflake._account(settings(account="org-acct")) == "ORG-ACCT"


async def test_a_key_that_is_not_rsa_is_refused() -> None:
    other = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fake = Fake(lambda r: reply(200, {}))
    with pytest.raises(DriverError, match="RSA"):
        async with session(fake, private_key=other.decode()) as _:
            pass
    assert fake.requests == []


async def test_a_query_sends_the_context_and_the_bindings_and_converts_the_values() -> None:
    answer = {
        "resultSetMetaData": meta(
            ("ID", "fixed", 0),
            ("PRICE", "fixed", 2),
            ("RATIO", "real", 0),
            ("OK", "boolean", 0),
            ("DAY", "date", 0),
            ("AT", "timestamp_ntz", 9),
            ("AT_TZ", "timestamp_tz", 9),
            ("NAME", "text", 0),
        ),
        "data": [
            ["7", "12.50", "0.5", "true", "19000", "1700000000.123456000", None, "a"],
            ["8", None, None, "false", None, None, "1700000000.000000000 1500", None],
        ],
    }
    fake = Fake(lambda r: reply(200, answer))
    sink = collector()
    async with session(fake) as s:
        status = await s.rows(
            "SELECT * FROM t WHERE a = ? AND b = ? AND c = ? AND d = ? AND e = ?",
            ["x", 3, 1.5, True, None],
            sink,
        )
    assert status == ""
    request = fake.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/api/v2/statements"
    assert request.url.params["requestId"]
    body = body_of(request)
    assert body["statement"].startswith("SELECT * FROM t")
    assert (body["warehouse"], body["database"], body["schema"], body["role"]) == (
        "WH",
        "SALES",
        "PUBLIC",
        "ANALYST",
    )
    assert body["timeout"] == 5
    assert body["parameters"] == {"MULTI_STATEMENT_COUNT": "1", "QUERY_TAG": "sama"}
    assert body["bindings"] == {
        "1": {"type": "TEXT", "value": "x"},
        "2": {"type": "FIXED", "value": "3"},
        "3": {"type": "REAL", "value": "1.5"},
        "4": {"type": "BOOLEAN", "value": "true"},
        "5": {"type": "TEXT", "value": None},
    }
    rows = sink.finish("")
    assert rows.columns == ["ID", "PRICE", "RATIO", "OK", "DAY", "AT", "AT_TZ", "NAME"]
    assert rows.count == 2
    assert json.loads(rows.shown[0]) == [
        7,
        "12.50",
        0.5,
        True,
        "2022-01-08",
        "2023-11-14T22:13:20.123456",
        None,
        "a",
    ]
    assert json.loads(rows.shown[1])[6] == "2023-11-14T23:13:20+01:00"


async def test_the_role_is_left_out_when_it_is_empty() -> None:
    fake = Fake(lambda r: reply(200, {"resultSetMetaData": meta(), "data": []}))
    async with session(fake, role="") as s:
        await s.rows("SELECT 1", [], collector())
    assert "role" not in body_of(fake.requests[0])
    assert "bindings" not in body_of(fake.requests[0])


async def test_the_params_must_match_the_marks_and_be_scalar() -> None:
    fake = Fake(lambda r: reply(200, {}))
    async with session(fake) as s:
        with pytest.raises(DriverError, match="1 parameters"):
            await s.rows("SELECT ?", [], collector())
        with pytest.raises(DriverError, match="params\\[0\\]"):
            await s.rows("SELECT ?", [{"a": 1}], collector())
    assert fake.requests == []


async def test_a_statement_that_runs_is_polled_until_it_is_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(snowflake, "POLL_FIRST_S", 0.01)
    polls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return reply(202, {"statementHandle": "h1", "message": "running"})
        polls.append(1)
        if len(polls) < 3:
            return reply(202, {"statementHandle": "h1", "message": "running"})
        return reply(
            200,
            {
                "statementHandle": "h1",
                "resultSetMetaData": meta(("N", "fixed", 0)),
                "data": [["1"]],
            },
        )

    fake = Fake(handler)
    sink = collector()
    async with session(fake) as s:
        await s.rows("SELECT 1", [], sink)
    assert len(fake.calls("GET", "/api/v2/statements/h1")) == 3
    assert json.loads(sink.finish("").shown[0]) == [1]


async def test_a_statement_over_the_time_limit_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(snowflake, "POLL_FIRST_S", 0.05)
    monkeypatch.setattr(snowflake, "POLL_LAST_S", 0.05)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cancel"):
            return reply(200, {"message": "canceled"})
        return reply(202, {"statementHandle": "h2"})

    fake = Fake(handler)
    async with session(fake, timeout_s=0.2) as s:
        with pytest.raises(DriverError, match=r"stopped after 0\.2 seconds"):
            await s.rows("SELECT SLOW()", [], collector())
    assert len(fake.calls("POST", "/api/v2/statements/h2/cancel")) == 1
    assert body_of(fake.requests[0])["timeout"] == 1


async def test_a_failed_cancel_does_not_hide_the_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(snowflake, "POLL_FIRST_S", 0.05)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cancel"):
            return reply(500, {})
        return reply(202, {"statementHandle": "h3"})

    async with session(Fake(handler), timeout_s=0.1) as s:
        with pytest.raises(DriverError, match="stopped after"):
            await s.rows("SELECT SLOW()", [], collector())


async def test_the_partitions_are_read_in_order() -> None:
    first = {
        "statementHandle": "h4",
        "resultSetMetaData": meta(("N", "fixed", 0), parts=3),
        "data": [["1"], ["2"]],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return reply(200, first)
        return reply(200, {"data": [[request.url.params["partition"] * 2]]})

    fake = Fake(handler)
    sink = collector()
    async with session(fake) as s:
        await s.rows("SELECT n FROM t", [], sink)
    rows = sink.finish("")
    assert [json.loads(r) for r in rows.shown] == [[1], [2], [11], [22]]
    assert [r.url.params["partition"] for r in fake.requests[1:]] == ["1", "2"]


async def test_the_engine_stops_when_the_collector_is_full() -> None:
    first = {
        "statementHandle": "h5",
        "resultSetMetaData": meta(("N", "fixed", 0), parts=3),
        "data": [["1"], ["2"], ["3"]],
    }
    fake = Fake(lambda r: reply(200, first))
    sink = RowCollector(frozenset(), 2, 1_000_000)
    async with session(fake) as s:
        await s.rows("SELECT n FROM t", [], sink)
    assert sink.count == 2
    assert not sink.complete
    assert len(fake.requests) == 1


async def test_an_error_answer_has_the_message_and_the_sqlstate() -> None:
    body = {
        "code": "002003",
        "sqlState": "42S02",
        "message": "SQL compilation error:\nObject 'T' does not exist.",
    }
    fake = Fake(lambda r: reply(422, body))
    async with session(fake) as s:
        with pytest.raises(DriverError) as caught:
            await s.rows("SELECT * FROM t", [], collector())
    assert not isinstance(caught.value, DriverUnavailableError)
    assert str(caught.value) == (
        "Snowflake answered: SQL compilation error: Object 'T' does not exist. (SQLSTATE 42S02)"
    )


@pytest.mark.parametrize("status", [401, 403])
async def test_a_login_error_is_a_driver_error_not_unavailable(status: int) -> None:
    fake = Fake(lambda r: httpx.Response(status, text="JWT token is invalid."))
    async with session(fake) as s:
        with pytest.raises(DriverError, match="JWT token is invalid") as caught:
            await s.rows("SELECT 1", [], collector())
    assert not isinstance(caught.value, DriverUnavailableError)


@pytest.mark.parametrize("status", [429, 503])
async def test_a_busy_service_is_unavailable(status: int) -> None:
    fake = Fake(lambda r: reply(status, {"message": "busy"}))
    async with session(fake) as s:
        with pytest.raises(DriverUnavailableError):
            await s.rows("SELECT 1", [], collector())


async def test_a_network_error_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async with session(Fake(handler)) as s:
        with pytest.raises(DriverUnavailableError, match="cannot be reached"):
            await s.rows("SELECT 1", [], collector())


async def test_a_host_that_resolves_to_a_private_address_is_refused() -> None:
    async def private(host: str, port: int) -> Sequence[IPAddress]:
        return [ipaddress.ip_address("10.0.0.5")]

    fake = Fake(lambda r: reply(200, {}))
    context = EngineContext(timeout_s=5, resolver=private, transport=wire_transport(fake))
    async with snowflake.open_session(settings(), TARGET, context, write=False) as s:
        with pytest.raises(DriverError, match="not allowed"):
            await s.rows("SELECT 1", [], collector())
    assert fake.requests == []


async def test_a_statement_that_changes_rows_gives_a_count() -> None:
    answer = {
        "resultSetMetaData": meta(("number of rows inserted", "fixed", 0)),
        "data": [["3"]],
        "stats": {"numRowsInserted": 2, "numRowsUpdated": 1, "numRowsDeleted": 0},
    }
    fake = Fake(lambda r: reply(200, answer))
    sink = collector()
    async with session(fake, write=True) as s:
        status = await s.rows("INSERT INTO t VALUES (1)", [], sink)
    assert status == "3 row(s) changed"
    assert sink.finish(status).count == 0


async def test_a_big_answer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snowflake, "MAX_ANSWER_BYTES", 50)
    fake = Fake(lambda r: reply(200, {"data": [["x" * 200]]}))
    async with session(fake) as s:
        with pytest.raises(DriverError, match="too large"):
            await s.rows("SELECT 1", [], collector())


async def test_sample_sql_quotes_each_name() -> None:
    fake = Fake(lambda r: reply(200, {}))
    async with session(fake) as s:
        sql = s.sample_sql("PUBLIC", 'Odd"Table', ["ID", 'we"ird', "a b"], 5)
    assert sql == 'SELECT "ID", "we""ird", "a b" FROM "SALES"."PUBLIC"."Odd""Table" LIMIT 5'


async def test_tables_reads_the_information_schema_of_the_database() -> None:
    columns = [
        ["PUBLIC", "ORDERS", "BASE TABLE", "The orders", "ID", "NUMBER", "38", "0", "NO", None],
        ["PUBLIC", "ORDERS", "BASE TABLE", "The orders", "NOTE", "TEXT", None, None, "YES", "Free"],
        ["PUBLIC", "V_ORDERS", "VIEW", None, "ID", "NUMBER", "10", "2", "YES", None],
    ]
    answer = {
        "resultSetMetaData": meta(*[(f"C{i}", "text", 0) for i in range(10)]),
        "data": columns,
    }
    fake = Fake(lambda r: reply(200, answer))
    async with session(fake) as s:
        tables = await s.tables(None, None)
        await s.tables("sales_raw", "orders")
        await s.tables('"Mixed"', '"Case"')
    assert [(t.schema, t.name, t.kind, t.comment) for t in tables] == [
        ("PUBLIC", "ORDERS", "table", "The orders"),
        ("PUBLIC", "V_ORDERS", "view", None),
    ]
    first = tables[0].columns
    assert [(c.name, c.type, c.not_null, c.comment) for c in first] == [
        ("ID", "number(38,0)", True, None),
        ("NOTE", "text", False, "Free"),
    ]
    one, two, three = (body_of(r) for r in fake.requests)
    assert 'FROM "SALES".INFORMATION_SCHEMA.COLUMNS' in one["statement"]
    assert "INFORMATION_SCHEMA.TABLES" in one["statement"]
    assert one["bindings"] == {"1": {"type": "TEXT", "value": "PUBLIC"}}
    assert two["bindings"] == {
        "1": {"type": "TEXT", "value": "SALES_RAW"},
        "2": {"type": "TEXT", "value": "ORDERS"},
    }
    assert three["bindings"]["1"]["value"] == "Mixed"
    assert three["bindings"]["2"]["value"] == "Case"


async def test_locate_folds_names_and_finds_the_columns() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        found = body_of(request)["bindings"]["2"]["value"] == "ORDERS"
        data = [["ID"], ["NOTE"]] if found else []
        return reply(200, {"resultSetMetaData": meta(("COLUMN_NAME", "text", 0)), "data": data})

    fake = Fake(handler)
    async with session(fake) as s:
        assert await s.locate(["orders"]) == ("PUBLIC", "ORDERS", ["ID", "NOTE"])
        assert await s.locate(["raw", "orders"]) == ("RAW", "ORDERS", ["ID", "NOTE"])
        assert await s.locate(["missing"]) is None
        assert await s.locate(["a", "b", "c"]) is None
    assert [body_of(r)["bindings"]["1"]["value"] for r in fake.requests] == [
        "PUBLIC",
        "RAW",
        "PUBLIC",
    ]


async def test_the_gateway_opens_the_engine_by_its_name() -> None:
    fake = Fake(lambda r: reply(200, {"resultSetMetaData": meta(), "data": []}))
    context = EngineContext(timeout_s=5, resolver=public, transport=wire_transport(fake))
    async with open_sql(settings(), TARGET, context, write=False) as s:
        await s.rows("SELECT 1", [], collector())
    assert len(fake.requests) == 1


def test_the_time_values_have_python_types() -> None:
    convert = snowflake._converter
    assert convert("date", 0)("0") == dt.date(1970, 1, 1)
    assert convert("time", 9)("3661.500000000") == dt.time(1, 1, 1, 500_000)
    ltz = convert("timestamp_ltz", 9)("86400.000000000")
    assert ltz == dt.datetime(1970, 1, 2, tzinfo=dt.UTC)
    assert convert("fixed", 2)("1.50") == Decimal("1.50")
    assert convert("real", 0)("nan") != convert("real", 0)("nan")
    assert convert("fixed", 0)("not a number") == "not a number"
    assert convert("variant", 0)('{"a":1}') == '{"a":1}'
