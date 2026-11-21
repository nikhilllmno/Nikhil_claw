"""The MongoDB engine (docs/architecture.md, Section 10.4, "What M4.13 builds").

Reads only: ``find`` and ``aggregate`` (``sama.connectors.mongoquery`` checks them). The
driver (pymongo) connects to a local relay (``relay``), and the relay connects to the checked
address with TLS. One client for each call, with ``directConnection``: the client does not
find other members of a replica set, so it connects to no other host. A ``mongodb+srv://``
string is not supported (the form has a host and a port).

Each row of a result is one document as relaxed extended JSON, in the column ``document``.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Mapping
from typing import Any, Final

from bson import json_util
from bson.json_util import RELAXED_JSON_OPTIONS
from pymongo import AsyncMongoClient
from pymongo.errors import (
    ConnectionFailure,
    ExecutionTimeout,
    OperationFailure,
    PyMongoError,
    ServerSelectionTimeoutError,
)

from sama.connectors.dbcore import ColumnInfo, RowCollector, TableInfo, Target, message_of
from sama.connectors.dbsettings import DbSettings
from sama.connectors.engines import EngineContext
from sama.connectors.engines.relay import relay
from sama.connectors.http_api import DriverError, DriverUnavailableError
from sama.connectors.mongoquery import MongoRead, drop_hidden

COLUMN: Final = "document"
MAX_COLLECTIONS: Final = 100
SAMPLE_DOCUMENTS: Final = 20


class MongoSession:
    """One MongoDB client for one call."""

    def __init__(
        self, client: AsyncMongoClient[dict[str, Any]], settings: DbSettings, timeout_s: float
    ) -> None:
        self._client = client
        self._db = client[settings.database]
        self._settings = settings
        self._max_ms = int(timeout_s * 1000)

    async def ping(self) -> None:
        try:
            await self._db.command("ping")
            await self._db.list_collection_names(filter={"name": "sama-test-only"})
        except PyMongoError as error:
            raise _error(error) from None

    async def read(self, read: MongoRead, sink: RowCollector) -> str:
        """Runs the read and gives each document to ``sink``. Returns the status."""
        collection = self._db[read.collection]
        try:
            if read.pipeline is not None:
                pipeline = [*read.pipeline, {"$limit": read.limit}]
                cursor: Any = await collection.aggregate(
                    pipeline, maxTimeMS=self._max_ms, allowDiskUse=False
                )
            else:
                cursor = collection.find(
                    dict(read.filter or {}),
                    dict(read.projection) if read.projection else None,
                    sort=list((read.sort or {}).items()) or None,
                    limit=read.limit,
                    max_time_ms=self._max_ms,
                )
            sink.start([COLUMN])
            try:
                async for document in cursor:
                    clean = drop_hidden(document, read.hidden)
                    text = json_util.dumps(clean, json_options=RELAXED_JSON_OPTIONS)
                    if not sink.add([text]):
                        break
            finally:
                await cursor.close()
        except PyMongoError as error:
            raise _error(error) from None
        return ""

    async def collections(self, name: str | None) -> list[TableInfo]:
        """The collections, the fields of some documents, and the indexes."""
        try:
            query: dict[str, Any] = {"name": name} if name else {}
            infos = [
                info
                async for info in await self._db.list_collections(filter=query)
                if not str(info.get("name", "")).startswith("system.")
            ]
            infos.sort(key=lambda info: str(info["name"]))
            out: list[TableInfo] = []
            for info in infos[:MAX_COLLECTIONS]:
                out.append(await self._collection(info))
        except PyMongoError as error:
            raise _error(error) from None
        return out

    async def _collection(self, info: Mapping[str, Any]) -> TableInfo:
        name = str(info["name"])
        kind = "view" if info.get("type") == "view" else "collection"
        collection = self._db[name]
        fields: dict[str, set[str]] = {}
        cursor = collection.find({}, limit=SAMPLE_DOCUMENTS, max_time_ms=self._max_ms)
        async for document in cursor:
            for key, value in document.items():
                fields.setdefault(str(key), set()).add(_type_name(value))
        details: list[str] = []
        comment = None
        if kind == "collection":
            count = await collection.estimated_document_count(maxTimeMS=self._max_ms)
            comment = f"about {count:,} documents"
            async for index in await collection.list_indexes():
                keys = ", ".join(f"{k} ({v})" for k, v in dict(index.get("key", {})).items())
                unique = "unique " if index.get("unique") else ""
                details.append(f"{unique}index: {keys}")
        columns = [ColumnInfo(k, " | ".join(sorted(v))) for k, v in fields.items()]
        return TableInfo(self._settings.database, name, kind, comment, columns, details)


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__.lower()


def _error(error: PyMongoError) -> DriverError:
    if isinstance(error, ExecutionTimeout):
        return DriverError("the read stopped after the time limit (maxTimeMS). Use a narrower read")
    if isinstance(error, ServerSelectionTimeoutError | ConnectionFailure):
        return DriverUnavailableError(f"MongoDB cannot be reached: {message_of(error, 200)}")
    if isinstance(error, OperationFailure):
        code = f" (code {error.code})" if error.code is not None else ""
        details = error.details or {}
        text = details.get("errmsg") if isinstance(details, Mapping) else None
        return DriverError(f"MongoDB answered: {message_of(text or error)}{code}")
    return DriverError(f"MongoDB answered: {message_of(error)}")


@contextlib.asynccontextmanager
async def open_session(
    settings: DbSettings, target: Target, context: EngineContext
) -> AsyncIterator[MongoSession]:
    async with relay(target, context.tls(settings), settings.host) as port:
        login: dict[str, Any] = {}
        if settings.user:
            login = {
                "username": settings.user,
                "password": settings.password,
                "authSource": settings.options.get("auth_source", "admin"),
            }
        client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
            host="127.0.0.1",
            port=port,
            directConnection=True,
            tls=False,
            serverSelectionTimeoutMS=10_000,
            connectTimeoutMS=10_000,
            socketTimeoutMS=int((context.timeout_s + 5) * 1000),
            appname="sama",
            retryReads=False,
            retryWrites=False,
            maxPoolSize=2,
            readPreference="secondaryPreferred",
            document_class=dict,
            **login,
        )
        try:
            yield MongoSession(client, settings, context.timeout_s)
        finally:
            await client.close()
