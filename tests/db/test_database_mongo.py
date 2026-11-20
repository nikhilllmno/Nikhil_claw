"""The gateway tests for MongoDB, against a real server in docker (docs/architecture.md,
Section 10.4, "What M4.13 builds").

Runs only with ``SAMA_TEST_MONGO_URL``, for example
``mongodb://root:rootpw@localhost:57017`` (``scripts/test-databases.sh`` sets it).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import pytest

from sama.connectors.database import MAX_ROWS, DatabaseGateway
from sama.connectors.http_api import DriverError
from sama.connectors.manifest import Catalog, Manifest
from sama.tools.output import MAX_OUTPUT_BYTES, untrusted
from tests.connectors.support import CATALOG
from tests.db.test_database_engines import CLAIMS
from tests.db.test_database_gateway import Sink

ROWS = 1_500


def mongodb() -> Manifest:
    found = Catalog.load(CATALOG).get("mongodb")
    assert found is not None
    return found


@dataclass
class Live:
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
        found = mongodb()
        tool = next(t for t in found.tools if t.name == f"db_mongodb_{operation}")
        return await (gw or self.gw).run(
            CLAIMS, uuid.uuid4(), found, tool, values or self.values, arguments
        )


@pytest.fixture
async def live() -> AsyncIterator[Live]:
    url = os.environ.get("SAMA_TEST_MONGO_URL")
    if not url:
        pytest.skip("set SAMA_TEST_MONGO_URL (scripts/test-databases.sh)")
    from pymongo import AsyncMongoClient

    database = f"sama_{uuid.uuid4().hex[:10]}"
    client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(url)
    db = client[database]
    await db.users.insert_many(
        [
            {
                "_id": i,
                "name": f"n{i}",
                "email": f"e{i}@hidden.example",
                "ssn": f"ssn-{i}",
                "profile": {"city": "c", "ssn": f"ssn-{i}"},
                "tags": [{"ssn": f"ssn-{i}", "t": 1}],
            }
            for i in range(1, ROWS + 1)
        ]
    )
    await db.orders.insert_many([{"_id": 1, "user": 1, "total": 9.5}, {"_id": 2, "user": 2}])
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
        yield Live(values, gw, spill, exports)
    finally:
        await gw.aclose()
        await client.drop_database(database)
        await client.close()


def documents(answer: str) -> list[dict[str, Any]]:
    """The documents of an answer (each row is ``["<json>"]``)."""
    out = []
    for line in answer.splitlines():
        if line.startswith('["{'):
            out.append(json.loads(json.loads(line)[0]))
    return out


async def test_find_and_aggregate(live: Live) -> None:
    found = documents(
        await live.run(
            "query",
            collection="users",
            filter={"_id": {"$lte": 3}},
            projection={"name": 1},
            sort={"_id": -1},
        )
    )
    assert found == [{"_id": 3, "name": "n3"}, {"_id": 2, "name": "n2"}, {"_id": 1, "name": "n1"}]
    grouped = documents(
        await live.run(
            "query",
            collection="orders",
            pipeline=[
                {
                    "$lookup": {
                        "from": "users",
                        "localField": "user",
                        "foreignField": "_id",
                        "as": "u",
                    }
                },
                {"$group": {"_id": None, "n": {"$sum": 1}}},
            ],
        )
    )
    assert grouped == [{"_id": None, "n": 2}]


@pytest.mark.parametrize(
    "arguments",
    [
        {"pipeline": [{"$out": "copy"}]},
        {"pipeline": [{"$merge": {"into": "copy"}}]},
        {"pipeline": [{"$currentOp": {}}]},
        {"filter": {"$where": "sleep(100) || true"}},
        {
            "pipeline": [
                {"$match": {"$expr": {"$function": {"body": "1", "args": [], "lang": "js"}}}}
            ]
        },
        {"pipeline": [{"$lookup": {"from": "system.users", "as": "x", "pipeline": []}}]},
    ],
)
async def test_a_write_or_code_is_refused(live: Live, arguments: dict[str, Any]) -> None:
    with pytest.raises(DriverError):
        await live.run("query", collection="users", **arguments)
    assert "copy" not in await live.run("schema")


async def test_a_mongodb_connection_is_read_only(live: Live) -> None:
    with pytest.raises(DriverError, match="read only"):
        await live.run("query", {**live.values, "writes": "on"}, collection="users")
    assert "db_mongodb_execute" not in {t.name for t in mongodb().tools}


async def test_the_model_gets_1000_rows_and_the_rest_spills(live: Live) -> None:
    answer = await live.run("query", collection="users", projection={"name": 1}, limit=ROWS)
    assert f"Shown: 1,000 of {ROWS:,} rows" in answer
    assert len(documents(answer)) == MAX_ROWS
    assert len(untrusted(answer, source="connector:mongodb").encode()) < MAX_OUTPUT_BYTES
    ((path, text),) = live.spill.files.items()
    assert path.startswith("/workdir/.sama/spill/db_mongodb_query-")
    assert len(text.splitlines()) == ROWS + 1


async def test_hidden_fields_never_appear(live: Live) -> None:
    values = {**live.values, "hidden_columns": "users.email, ssn"}
    texts = [
        await live.run("schema", values),
        await live.run("sample", values, table="users", limit=5),
        await live.run("query", values, collection="users", limit=10),
        await live.run(
            "query",
            values,
            collection="orders",
            pipeline=[
                {
                    "$lookup": {
                        "from": "users",
                        "localField": "user",
                        "foreignField": "_id",
                        "as": "u",
                    }
                }
            ],
        ),
        await live.run("export", values, collection="users"),
    ]
    files = list(live.spill.files.values()) + list(live.exports.files.values())
    assert len(live.exports.files) == 1
    for text in texts + files:
        assert "hidden.example" not in text
        assert "ssn-" not in text
    assert "name" in texts[0]
    assert "city" in texts[2]
    for arguments in (
        {"filter": {"email": "e1@hidden.example"}},
        {"filter": {"profile.ssn": {"$exists": True}}},
        {"projection": {"email": 1}},
        {"sort": {"ssn": 1}},
        {"pipeline": [{"$project": {"x": "$email"}}]},
        {"pipeline": [{"$project": {"x": "$$ROOT"}}]},
        {"pipeline": [{"$project": {"x": {"$objectToArray": "$profile"}}}]},
        {"pipeline": [{"$group": {"_id": "$profile.ssn"}}]},
        {
            "pipeline": [
                {"$unwind": {"path": "$tags"}},
                {"$replaceRoot": {"newRoot": "$tags"}},
                {"$project": {"v": {"$getField": "ssn"}}},
            ]
        },
    ):
        with pytest.raises(DriverError, match=r"hidden|not allowed"):
            await live.run("query", values, collection="users", **arguments)


async def test_only_the_allowed_collections_show(live: Live) -> None:
    values = {**live.values, "tables": "orders"}
    described = await live.run("schema", values)
    assert "orders" in described
    assert "users" not in described
    assert len(documents(await live.run("query", values, collection="orders"))) == 2
    for arguments in (
        {"collection": "users"},
        {"collection": "orders", "pipeline": [{"$unionWith": "users"}]},
        {
            "collection": "orders",
            "pipeline": [{"$lookup": {"from": "users", "as": "u", "pipeline": []}}],
        },
    ):
        with pytest.raises(DriverError, match="allowed"):
            await live.run("query", values, **arguments)
    with pytest.raises(DriverError, match="allowed"):
        await live.run("sample", values, table="users")


async def test_a_read_stops_after_the_timeout(live: Live) -> None:
    gw = DatabaseGateway(allow_private=True, require_tls=False, timeout_s=1)
    slow = [
        {"$lookup": {"from": "users", "as": "a", "pipeline": [{"$project": {"name": 1}}]}},
        {"$unwind": "$a"},
        {"$lookup": {"from": "users", "as": "b", "pipeline": [{"$project": {"name": 1}}]}},
        {"$unwind": "$b"},
        {"$count": "n"},
    ]
    try:
        started = time.monotonic()
        with pytest.raises(DriverError, match=r"time limit|stopped after"):
            await live.run("query", gw=gw, collection="users", pipeline=slow)
        assert time.monotonic() - started < 8
    finally:
        await gw.aclose()


async def test_the_connection_test_uses_the_gateway(live: Live) -> None:
    await live.gw.test(mongodb(), live.values)
    with pytest.raises(DriverError, match=r"refused the login|answered|authentication"):
        await live.gw.test(mongodb(), {**live.values, "password": "wrong"})
    with pytest.raises(DriverError, match="public"):
        await DatabaseGateway().test(mongodb(), live.values)
