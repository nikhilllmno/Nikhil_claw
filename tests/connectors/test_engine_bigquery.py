"""The BigQuery engine with a fake API: the token exchange, the query request, paging, the
time limit, errors, and the schema tools (no Google account)."""

from __future__ import annotations

import ipaddress
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from sama.connectors.dbcore import RowCollector, SqlSession, Target
from sama.connectors.dbsettings import TOKEN_URI, DbSettings
from sama.connectors.engines import EngineContext, bigquery
from sama.connectors.http_api import DriverError, DriverUnavailableError
from sama.connectors.manifest import parse
from tests.support import wire_transport

MANIFEST = """
name: warehouse
title: Warehouse
description: The BigQuery warehouse.
kind: database
database:
  engine: bigquery
"""
EMAIL = "agent@my-project-1.iam.gserviceaccount.com"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
PUBLIC = KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)
TARGET = Target("bigquery.googleapis.com", 443)
JOB = {"projectId": "my-project-1", "jobId": "job_1", "location": "EU"}
SCHEMA = {
    "fields": [
        {"name": "id", "type": "INTEGER"},
        {"name": "name", "type": "STRING"},
    ]
}


def settings(**extra: str) -> DbSettings:
    key = {
        "type": "service_account",
        "client_email": EMAIL,
        "private_key": PRIVATE,
        "private_key_id": "key-1",
    }
    values = {
        "project": "my-project-1",
        "dataset": "sales",
        "location": "EU",
        "service_account_json": json.dumps(key),
        "writes": "true",
        **extra,
    }
    return DbSettings.parse(parse(MANIFEST), values)


async def resolver(host: str, port: int) -> list[Any]:
    return [ipaddress.ip_address("142.250.1.1")]


class Fake:
    """The Google API. ``answers`` maps ``METHOD path`` to a function of the request body."""

    def __init__(self, answers: dict[str, Callable[[dict[str, Any], httpx.Request], Any]]) -> None:
        self.answers = answers
        self.seen: list[tuple[str, str, str, dict[str, Any]]] = []
        self.auth: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.headers["host"]
        body = json.loads(request.content) if request.content else {}
        key = f"{request.method} {request.url.path}"
        self.seen.append((host, key, request.url.query.decode(), body))
        self.auth.append(request.headers.get("authorization", ""))
        if host == "oauth2.googleapis.com" and key == "POST /token":
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        assert host == "bigquery.googleapis.com", host
        answer = self.answers.get(key)
        if answer is None:
            return httpx.Response(404, json={"error": {"message": f"no route {key}"}})
        result = answer(body, request)
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, json=result)

    def calls(self, key: str) -> list[dict[str, Any]]:
        return [body for _host, k, _query, body in self.seen if k == key]

    def queries(self, key: str) -> list[dict[str, str]]:
        return [dict(httpx.QueryParams(q)) for _host, k, q, _body in self.seen if k == key]


@asynccontextmanager
async def session(
    fake: Fake, *, timeout_s: float = 5, config: DbSettings | None = None
) -> AsyncIterator[SqlSession]:
    context = EngineContext(
        timeout_s=timeout_s, resolver=resolver, transport=wire_transport(fake.handle)
    )
    async with bigquery.open_session(config or settings(), TARGET, context, write=False) as opened:
        yield opened


QUERIES = "POST /bigquery/v2/projects/my-project-1/queries"
RESULTS = "GET /bigquery/v2/projects/my-project-1/queries/job_1"
CANCEL = "POST /bigquery/v2/projects/my-project-1/jobs/job_1/cancel"


def collector() -> RowCollector:
    sink = RowCollector(frozenset(), 100, 100_000)
    return sink


def row(*values: Any) -> dict[str, Any]:
    return {"f": [{"v": v} for v in values]}


async def test_the_token_is_a_jwt_that_the_public_key_checks() -> None:
    fake = Fake({QUERIES: lambda body, req: {"jobComplete": True, "schema": SCHEMA}})
    async with session(fake) as opened:
        await opened.rows("SELECT 1", [], collector())
    host, key, _query, body = fake.seen[0]
    assert (host, key) == ("oauth2.googleapis.com", "POST /token")
    assert body["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    claims = jwt.decode(body["assertion"], PUBLIC, algorithms=["RS256"], audience=TOKEN_URI)
    assert claims["iss"] == claims["sub"] == EMAIL
    assert claims["scope"] == "https://www.googleapis.com/auth/bigquery"
    assert claims["exp"] - claims["iat"] == 3600
    assert jwt.get_unverified_header(body["assertion"])["kid"] == "key-1"
    assert fake.auth[0] == ""
    assert fake.auth[1] == "Bearer tok-1"


async def test_a_query_sends_the_limits_and_typed_parameters() -> None:
    fake = Fake(
        {
            QUERIES: lambda body, req: {
                "jobComplete": True,
                "jobReference": JOB,
                "schema": {
                    "fields": [
                        {"name": "id", "type": "INTEGER"},
                        {"name": "ratio", "type": "FLOAT"},
                        {"name": "ok", "type": "BOOLEAN"},
                        {"name": "price", "type": "NUMERIC"},
                        {"name": "at", "type": "TIMESTAMP"},
                        {"name": "tags", "type": "STRING", "mode": "REPEATED"},
                        {
                            "name": "owner",
                            "type": "RECORD",
                            "fields": [{"name": "n", "type": "INT64"}],
                        },
                        {"name": "note", "type": "STRING"},
                    ]
                },
                "rows": [
                    row(
                        "7",
                        "0.5",
                        "true",
                        "12.50",
                        "1.7E9",
                        [{"v": "a"}, {"v": "b"}],
                        {"f": [{"v": "3"}]},
                        None,
                    )
                ],
            }
        }
    )
    sink = collector()
    async with session(fake) as opened:
        status = await opened.rows(
            "SELECT * FROM t WHERE a = ? AND b = ? AND c = ? AND d = ? AND e = ?",
            ["x", 3, 1.5, True, None],
            sink,
        )
    assert status == ""
    body = fake.calls(QUERIES)[0]
    assert body["useLegacySql"] is False
    assert body["dryRun"] is False
    assert body["maximumBytesBilled"] == settings().options["max_bytes_billed"]
    assert body["defaultDataset"] == {"projectId": "my-project-1", "datasetId": "sales"}
    assert body["location"] == "EU"
    assert body["timeoutMs"] == 5000
    assert body["parameterMode"] == "POSITIONAL"
    assert [
        (p["parameterType"]["type"], p["parameterValue"]["value"]) for p in body["queryParameters"]
    ] == [
        ("STRING", "x"),
        ("INT64", "3"),
        ("FLOAT64", "1.5"),
        ("BOOL", "true"),
        ("STRING", None),
    ]
    result = sink.finish("")
    assert result.columns == ["id", "ratio", "ok", "price", "at", "tags", "owner", "note"]
    assert json.loads(result.shown[0]) == [
        7,
        0.5,
        True,
        "12.50",
        "2023-11-14T22:13:20+00:00",
        ["a", "b"],
        {"n": 3},
        None,
    ]


async def test_the_values_have_python_types() -> None:
    fields = [
        {"name": "i", "type": "INT64"},
        {"name": "n", "type": "BIGNUMERIC"},
        {"name": "f", "type": "FLOAT64"},
        {"name": "b", "type": "BOOL"},
    ]
    fake = Fake(
        {
            QUERIES: lambda body, req: {
                "jobComplete": True,
                "schema": {"fields": fields},
                "rows": [row("1", "1.25", "NaN", "false")],
            }
        }
    )
    seen: list[Any] = []

    class Spy(RowCollector):
        def add(self, row: Any) -> bool:
            seen.extend(row)
            return super().add(row)

    sink = Spy(frozenset(), 10, 10_000)
    async with session(fake) as opened:
        await opened.rows("SELECT 1", [], sink)
    assert seen[0] == 1
    assert seen[1] == Decimal("1.25")
    assert seen[2] != seen[2]
    assert seen[3] is False


async def test_a_parameter_count_that_does_not_match_is_refused() -> None:
    fake = Fake({})
    async with session(fake) as opened:
        with pytest.raises(DriverError, match="1 parameters"):
            await opened.rows("SELECT ?", [], collector())
        with pytest.raises(DriverError, match="params\\[0\\]"):
            await opened.rows("SELECT ?", [{"a": 1}], collector())
    assert fake.calls(QUERIES) == []


async def test_the_pages_are_read_until_the_end() -> None:
    pages: dict[str, dict[str, Any]] = {
        "": {"pageToken": "p2", "rows": [row("1", "a"), row("2", "b")]},
        "p2": {"pageToken": "p3", "rows": [row("3", "c")]},
        "p3": {"rows": [row("4", "d")]},
    }

    def first(body: dict[str, Any], req: httpx.Request) -> Any:
        return {"jobComplete": True, "jobReference": JOB, "schema": SCHEMA, **pages[""]}

    def next_page(body: dict[str, Any], req: httpx.Request) -> Any:
        return {
            "jobComplete": True,
            "jobReference": JOB,
            "schema": SCHEMA,
            **pages[req.url.params["pageToken"]],
        }

    fake = Fake({QUERIES: first, RESULTS: next_page})
    sink = collector()
    async with session(fake) as opened:
        await opened.rows("SELECT id, name FROM t", [], sink)
    assert [json.loads(r) for r in sink.finish("").shown] == [
        [1, "a"],
        [2, "b"],
        [3, "c"],
        [4, "d"],
    ]
    assert [q["pageToken"] for q in fake.queries(RESULTS)] == ["p2", "p3"]
    assert fake.queries(RESULTS)[0]["location"] == "EU"


async def test_the_reading_stops_when_the_sink_is_full() -> None:
    def first(body: dict[str, Any], req: httpx.Request) -> Any:
        return {
            "jobComplete": True,
            "jobReference": JOB,
            "schema": SCHEMA,
            "pageToken": "more",
            "rows": [row("1", "a"), row("2", "b"), row("3", "c")],
        }

    fake = Fake({QUERIES: first})
    sink = RowCollector(frozenset(), 2, 10_000)
    async with session(fake) as opened:
        await opened.rows("SELECT id, name FROM t", [], sink)
    result = sink.finish("")
    assert result.count == 2
    assert not result.complete
    assert fake.queries(RESULTS) == []


async def test_a_job_that_is_not_complete_is_polled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bigquery, "POLL_S", 0.01)
    polls = {"n": 0}

    def poll(body: dict[str, Any], req: httpx.Request) -> Any:
        polls["n"] += 1
        if polls["n"] < 3:
            return {"jobComplete": False, "jobReference": JOB}
        return {"jobComplete": True, "jobReference": JOB, "schema": SCHEMA, "rows": [row("9", "z")]}

    fake = Fake(
        {
            QUERIES: lambda body, req: {"jobComplete": False, "jobReference": JOB},
            RESULTS: poll,
        }
    )
    sink = collector()
    async with session(fake) as opened:
        await opened.rows("SELECT id, name FROM t", [], sink)
    assert polls["n"] == 3
    assert [json.loads(r) for r in sink.finish("").shown] == [[9, "z"]]
    assert all(int(q["timeoutMs"]) > 0 for q in fake.queries(RESULTS))
    assert fake.calls(CANCEL) == []


async def test_a_job_over_the_time_limit_is_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bigquery, "POLL_S", 0.01)
    fake = Fake(
        {
            QUERIES: lambda body, req: {"jobComplete": False, "jobReference": JOB},
            RESULTS: lambda body, req: {"jobComplete": False, "jobReference": JOB},
            CANCEL: lambda body, req: {"job": {}},
        }
    )
    async with session(fake, timeout_s=0.1) as opened:
        with pytest.raises(DriverError, match=r"the statement stopped after 0\.1 seconds"):
            await opened.rows("SELECT 1", [], collector())
    assert len(fake.calls(CANCEL)) == 1
    assert fake.queries(CANCEL)[0] == {"location": "EU"}


async def test_the_bytes_billed_limit_error_comes_through() -> None:
    message = "Query exceeded limit for bytes billed: 10485760. 20971520 or higher required."
    error = {
        "error": {
            "code": 400,
            "message": message,
            "errors": [{"reason": "bytesBilledLimitExceeded"}],
        }
    }
    fake = Fake({QUERIES: lambda body, req: httpx.Response(400, json=error)})
    async with session(fake) as opened:
        with pytest.raises(DriverError) as caught:
            await opened.rows("SELECT * FROM big", [], collector())
    assert not isinstance(caught.value, DriverUnavailableError)
    assert str(caught.value) == f"BigQuery answered: {message}"


async def test_a_server_error_or_a_network_error_is_unavailable() -> None:
    fake = Fake(
        {QUERIES: lambda body, req: httpx.Response(503, json={"error": {"message": "busy"}})}
    )
    async with session(fake) as opened:
        with pytest.raises(DriverUnavailableError, match="BigQuery answered: busy"):
            await opened.rows("SELECT 1", [], collector())

    def broken(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok"})
        raise httpx.ConnectError("down")

    context = EngineContext(timeout_s=5, resolver=resolver, transport=wire_transport(broken))
    async with bigquery.open_session(settings(), TARGET, context, write=False) as opened:
        with pytest.raises(DriverUnavailableError, match="cannot be reached"):
            await opened.rows("SELECT 1", [], collector())


async def test_a_refused_sign_in_is_an_error_without_the_key() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "Invalid signature"}
        )

    context = EngineContext(timeout_s=5, resolver=resolver, transport=wire_transport(refuse))
    with pytest.raises(DriverError, match="invalid_grant: Invalid signature") as caught:
        async with bigquery.open_session(settings(), TARGET, context, write=False):
            pass
    assert "PRIVATE KEY" not in str(caught.value)


async def test_a_request_to_a_private_address_is_blocked() -> None:
    async def private(host: str, port: int) -> list[Any]:
        return [ipaddress.ip_address("10.0.0.5")]

    context = EngineContext(
        timeout_s=5, resolver=private, transport=wire_transport(Fake({}).handle)
    )
    with pytest.raises(DriverError, match="blocked"):
        async with bigquery.open_session(settings(), TARGET, context, write=False):
            pass


async def test_a_dml_statement_gives_the_changed_rows() -> None:
    fake = Fake(
        {
            QUERIES: lambda body, req: {
                "jobComplete": True,
                "jobReference": JOB,
                "numDmlAffectedRows": "3",
            }
        }
    )
    sink = collector()
    async with session(fake) as opened:
        status = await opened.rows("DELETE FROM t WHERE id > ?", [1], sink)
    assert status == "3 row(s) changed"
    assert sink.finish(status).columns == []


async def test_a_connection_without_a_dataset_or_location_sends_neither() -> None:
    fake = Fake({QUERIES: lambda body, req: {"jobComplete": True, "schema": SCHEMA}})
    config = settings(dataset="", location="", max_bytes_billed="20971520")
    async with session(fake, config=config) as opened:
        await opened.rows("SELECT 1", [], collector())
    body = fake.calls(QUERIES)[0]
    assert "defaultDataset" not in body
    assert "location" not in body
    assert "queryParameters" not in body
    assert body["maximumBytesBilled"] == "20971520"


async def test_sample_sql_quotes_the_names() -> None:
    fake = Fake({})
    async with session(fake) as opened:
        assert (
            opened.sample_sql("sales", "orders", ["id", "na`me", "back\\slash"], 5)
            == "SELECT `id`, `na\\`me`, `back\\\\slash` FROM `my-project-1.sales.orders` LIMIT 5"
        )
        assert opened.sample_sql("a`b", "t", ["c"], 1) == (
            "SELECT `c` FROM `my-project-1.a\\`b.t` LIMIT 1"
        )


async def test_tables_lists_the_columns_of_the_dataset() -> None:
    def info(body: dict[str, Any], req: httpx.Request) -> Any:
        query = body["query"]
        assert "`my-project-1." in query
        if "INFORMATION_SCHEMA.TABLES" in query:
            return {
                "jobComplete": True,
                "schema": {"fields": [{"name": "table_name"}, {"name": "table_type"}]},
                "rows": [row("orders", "BASE TABLE"), row("recent", "VIEW")],
            }
        assert "INFORMATION_SCHEMA.COLUMNS" in query
        return {
            "jobComplete": True,
            "schema": {"fields": [{"name": n} for n in ("t", "c", "d", "n")]},
            "rows": [
                row("orders", "id", "INT64", "NO"),
                row("orders", "note", "STRING", "YES"),
                row("recent", "id", "INT64", "YES"),
            ],
        }

    fake = Fake({QUERIES: info})
    async with session(fake) as opened:
        found = await opened.tables(None, None)
        assert [(t.schema, t.name, t.kind) for t in found] == [
            ("sales", "orders", "table"),
            ("sales", "recent", "view"),
        ]
        assert [(c.name, c.type, c.not_null) for c in found[0].columns] == [
            ("id", "INT64", True),
            ("note", "STRING", False),
        ]
        await opened.tables("other", "orders")
    last = fake.calls(QUERIES)[-1]
    assert "`my-project-1.other`.INFORMATION_SCHEMA.COLUMNS" in last["query"]
    assert last["queryParameters"][0]["parameterValue"]["value"] == "orders"
    assert last["defaultDataset"]["datasetId"] == "sales"


async def test_tables_needs_a_dataset() -> None:
    fake = Fake({})
    async with session(fake, config=settings(dataset="")) as opened:
        with pytest.raises(DriverError, match="give schema"):
            await opened.tables(None, None)
        with pytest.raises(DriverError, match="not a valid name"):
            await opened.tables("a`; DROP", None)
    assert fake.calls(QUERIES) == []


async def test_locate_finds_a_table_in_the_default_or_a_given_dataset() -> None:
    def columns(body: dict[str, Any], req: httpx.Request) -> Any:
        dataset = "other" if "my-project-1.other" in body["query"] else "sales"
        name = body["queryParameters"][0]["parameterValue"]["value"]
        rows = (
            [row("id"), row("note")]
            if (dataset, name) in {("sales", "orders"), ("other", "t")}
            else []
        )
        return {"jobComplete": True, "schema": {"fields": [{"name": "column_name"}]}, "rows": rows}

    fake = Fake({QUERIES: columns})
    async with session(fake) as opened:
        assert await opened.locate(["orders"]) == ("sales", "orders", ["id", "note"])
        assert await opened.locate(["other", "t"]) == ("other", "t", ["id", "note"])
        assert await opened.locate(["missing"]) is None
        assert await opened.locate(["a", "b", "c"]) is None
        assert await opened.locate(["bad`name", "t"]) is None
    async with session(fake, config=settings(dataset="")) as opened:
        assert await opened.locate(["orders"]) is None
