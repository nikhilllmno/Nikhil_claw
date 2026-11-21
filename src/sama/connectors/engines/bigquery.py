"""The BigQuery engine of the database gateway (docs/architecture.md, Section 10.4).

BigQuery has no wire protocol: this engine calls the REST API (``jobs.query`` and
``jobs.getQueryResults``) through ``OutboundClient``, so the address of every request is
checked. A service account key gives a signed JWT, and the token endpoint of Google changes
it for an access token. A session keeps no token after the call.

The cost limit is ``maximumBytesBilled``: BigQuery refuses a query that would read more. The
rule that a read cannot write is in the SQL check (``sama.connectors.sql``), not here.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import re
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Protocol
from urllib.parse import quote

import jwt

from sama.connectors.dbcore import (
    ColumnInfo,
    RowCollector,
    SqlSession,
    TableInfo,
    Target,
    check_params,
    message_of,
    scalar,
)
from sama.connectors.dbsettings import TOKEN_URI, DbSettings
from sama.connectors.engines import EngineContext
from sama.connectors.http_api import DriverError, DriverUnavailableError
from sama.tools.outbound import BlockedUrlError, FetchError, FetchResult, OutboundClient, UrlPolicy

SCOPE: Final = "https://www.googleapis.com/auth/bigquery"
GRANT: Final = "urn:ietf:params:oauth:grant-type:jwt-bearer"
MAX_ANSWER_BYTES: Final = 25 * 1024 * 1024
"""The most bytes of one answer of the API."""
PAGE_ROWS: Final = 5_000
"""The most rows that BigQuery sends in one answer."""
POLL_S: Final = 0.2
"""The wait between two polls of a job that is not complete."""
MAX_POLL_MS: Final = 10_000
"""The most time that one poll waits at the server."""
CALL_MARGIN_S: Final = 5.0
"""The time that one poll can take after the time limit of its ``timeoutMs`` wait."""
CANCEL_S: Final = 5.0
"""The longest time for the cancel of a job."""
MAX_LIST_ROWS: Final = 100_000
UNAVAILABLE_CODES: Final = frozenset({408, 429})
"""Client codes that mean "try again later". Every 5xx code also means that."""
_DATASET: Final = re.compile(r"^[A-Za-z0-9_]{1,1024}$")
_EPOCH: Final = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_KINDS: Final = {"BASE TABLE": "table"}


class _Sink(Protocol):
    def start(self, names: Sequence[str]) -> None: ...

    def add(self, row: Sequence[Any]) -> bool: ...


class _Rows:
    """Keeps all rows of a small result (the schema tool)."""

    def __init__(self) -> None:
        self.names: list[str] = []
        self.rows: list[Sequence[Any]] = []

    def start(self, names: Sequence[str]) -> None:
        self.names = list(names)

    def add(self, row: Sequence[Any]) -> bool:
        if len(self.rows) >= MAX_LIST_ROWS:
            return False
        self.rows.append(row)
        return True


def _quote(name: str) -> str:
    """A name in backticks. A backslash and a backtick have a backslash before them."""
    return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"


def _error_text(data: object, status: int) -> str:
    """The message of an error answer of Google (the API or the token endpoint)."""
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return message_of(Exception(error["message"]))
        if isinstance(error, str):
            detail = data.get("error_description")
            return message_of(Exception(f"{error}: {detail}" if detail else error))
    return f"status {status}"


def _json(result: FetchResult) -> Any:
    if result.truncated:
        raise DriverError("the answer of BigQuery is too large")
    try:
        return json.loads(result.content)
    except ValueError:
        return None


def _timestamp(text: str) -> str:
    """An ISO text for the seconds since the epoch that BigQuery sends (for example
    ``1.7E9``). A value that does not fit stays as it is."""
    try:
        micros = int(Decimal(text) * 1_000_000)
        return (_EPOCH + dt.timedelta(microseconds=micros)).isoformat()
    except (InvalidOperation, OverflowError, ValueError):
        return text


def _cell(field: Mapping[str, Any], value: Any) -> Any:
    """The Python value of one cell. ``field`` is the schema field, and ``value`` is the
    ``v`` of the cell."""
    if value is None:
        return None
    if field.get("mode") == "REPEATED":
        return [_one(field, item.get("v")) for item in value if isinstance(item, dict)]
    return _one(field, value)


def _one(field: Mapping[str, Any], value: Any) -> Any:
    if value is None:
        return None
    kind = str(field.get("type", "STRING")).upper()
    try:
        if kind in ("RECORD", "STRUCT"):
            subs = field.get("fields") or []
            cells = value.get("f") or []
            return {
                str(sub.get("name")): _cell(sub, cell.get("v"))
                for sub, cell in zip(subs, cells, strict=False)
            }
        if kind in ("INTEGER", "INT64"):
            return int(value)
        if kind in ("FLOAT", "FLOAT64"):
            return float(value)
        if kind in ("BOOLEAN", "BOOL"):
            return value in ("true", True)
        if kind in ("NUMERIC", "BIGNUMERIC"):
            return Decimal(value)
        if kind == "TIMESTAMP":
            return _timestamp(str(value))
    except (ArithmeticError, AttributeError, TypeError, ValueError):
        return value
    return value


def _parameter(value: Any, index: int) -> dict[str, Any]:
    """One query parameter, typed from the JSON value."""
    value = scalar(value, index)
    if value is None:
        return {"parameterType": {"type": "STRING"}, "parameterValue": {"value": None}}
    if isinstance(value, bool):
        kind, text = "BOOL", "true" if value else "false"
    elif isinstance(value, int):
        kind, text = "INT64", str(value)
    elif isinstance(value, float):
        kind, text = "FLOAT64", repr(value)
    else:
        kind, text = "STRING", value
    return {"parameterType": {"type": kind}, "parameterValue": {"value": text}}


class BigQuerySession:
    """One call of the gateway: one access token and the requests of the call."""

    def __init__(
        self, settings: DbSettings, context: EngineContext, client: OutboundClient, token: str
    ) -> None:
        self._project = settings.database
        self._dataset = settings.options.get("dataset", "")
        self._location = settings.options.get("location", "")
        self._max_bytes = settings.options.get("max_bytes_billed", "")
        self._timeout_s = context.timeout_s
        self._client = client
        self._headers = {"authorization": f"Bearer {token}"}
        self._base = f"https://{settings.host}/bigquery/v2/projects/{quote(self._project, safe='')}"

    # -- requests -----------------------------------------------------------------------------

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: object = None,
    ) -> dict[str, Any]:
        return await _send(
            self._client, method, self._base + path, self._headers, params=params, body=body
        )

    def _wrong_time(self) -> DriverError:
        return DriverError(f"the statement stopped after {self._timeout_s:g} seconds")

    async def _cancel(self, job: Mapping[str, Any]) -> None:
        """Asks BigQuery to stop a job. A failure here is not important."""
        job_id = job.get("jobId")
        if not isinstance(job_id, str):
            return
        params = {"location": str(job["location"])} if job.get("location") else None
        with contextlib.suppress(DriverError):
            await self._call("POST", f"/jobs/{quote(job_id, safe='')}/cancel", params=params)

    async def _stop(self, job: Mapping[str, Any]) -> None:
        """Cancels an unfinished job in a bounded time, also when the call is cancelled."""
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(self._cancel(job)), CANCEL_S)

    async def _query(self, sql: str, params: Sequence[Any], sink: _Sink) -> str:
        """Runs one statement and gives its rows to ``sink``. Returns the status."""
        check_params(sql, "bigquery", params)
        deadline = time.monotonic() + self._timeout_s
        body: dict[str, Any] = {
            "query": sql,
            "useLegacySql": False,
            "dryRun": False,
            "timeoutMs": max(1, int(self._timeout_s * 1000)),
            # BigQuery tries to stop the job at the time limit, also when the gateway cannot
            # cancel it (for example when the call stops before the answer to this request
            # gives the job ID). ``maximumBytesBilled`` limits the cost of such a job.
            "jobTimeoutMs": str(max(1000, int(self._timeout_s * 1000))),
            "maxResults": PAGE_ROWS,
        }
        if self._max_bytes:
            body["maximumBytesBilled"] = self._max_bytes
        if self._dataset:
            body["defaultDataset"] = {"projectId": self._project, "datasetId": self._dataset}
        if self._location:
            body["location"] = self._location
        if params:
            body["parameterMode"] = "POSITIONAL"
            body["queryParameters"] = [_parameter(v, i) for i, v in enumerate(params)]
        data = await self._call("POST", "/queries", body=body)
        job = data.get("jobReference")
        job = job if isinstance(job, dict) else {}
        if not job.get("location") and self._location:
            job = {**job, "location": self._location}
        try:
            while not data.get("jobComplete", True):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self._wrong_time()
                await asyncio.sleep(min(POLL_S, remaining))
                remaining = max(deadline - time.monotonic(), 0.001)
                wait = max(1, int(min(remaining * 1000, MAX_POLL_MS)))
                data = await asyncio.wait_for(
                    self._results(job, {"timeoutMs": str(wait)}), remaining + CALL_MARGIN_S
                )
        except BaseException:
            # The time limit, a network error, or a cancel of the call: the job does not
            # run on without the gateway.
            await self._stop(job)
            raise
        status = ""
        fields: list[Mapping[str, Any]] | None = None
        while True:
            if fields is None and isinstance(data.get("schema"), dict):
                fields = data["schema"].get("fields") or []
                sink.start([str(f.get("name", "")) for f in fields])
            if data.get("numDmlAffectedRows") is not None:
                status = f"{data['numDmlAffectedRows']} row(s) changed"
            for row in data.get("rows") or []:
                cells = [_cell(f, c.get("v")) for f, c in zip(fields or [], row["f"], strict=False)]
                if not sink.add(cells):
                    return status
            token = data.get("pageToken")
            if not token or not isinstance(job.get("jobId"), str):
                return status
            if time.monotonic() > deadline:
                raise self._wrong_time()
            data = await self._results(job, {"pageToken": str(token), "maxResults": str(PAGE_ROWS)})

    async def _results(self, job: Mapping[str, Any], params: dict[str, str]) -> dict[str, Any]:
        if job.get("location"):
            params["location"] = str(job["location"])
        return await self._call(
            "GET", f"/queries/{quote(str(job['jobId']), safe='')}", params=params
        )

    # -- the SqlSession protocol --------------------------------------------------------------

    async def rows(self, sql: str, params: Sequence[Any], sink: RowCollector) -> str:
        return await self._query(sql, params, sink)

    def _dataset_of(self, schema: str | None) -> str:
        dataset = schema or self._dataset
        if not dataset:
            raise DriverError("give schema (the dataset)")
        if not _DATASET.match(dataset):
            raise DriverError("the dataset is not a valid name")
        return dataset

    def _info(self, dataset: str, view: str) -> str:
        return f"{_quote(f'{self._project}.{dataset}')}.INFORMATION_SCHEMA.{view}"

    async def tables(self, schema: str | None, table: str | None) -> list[TableInfo]:
        dataset = self._dataset_of(schema)
        marks = " WHERE table_name = ?" if table else ""
        params = [table] if table else []
        kinds = _Rows()
        sql = f"SELECT table_name, table_type FROM {self._info(dataset, 'TABLES')}{marks}"  # noqa: S608
        await self._query(sql, params, kinds)
        found: dict[str, TableInfo] = {}
        for name, kind in kinds.rows:
            text = str(kind).upper()
            found[str(name)] = TableInfo(dataset, str(name), _KINDS.get(text, text.lower()))
        columns = _Rows()
        sql = (
            "SELECT table_name, column_name, data_type, is_nullable"  # noqa: S608
            f" FROM {self._info(dataset, 'COLUMNS')}{marks}"
            " ORDER BY table_name, ordinal_position"
        )
        await self._query(sql, params, columns)
        for name, column, kind, nullable in columns.rows:
            info = found.get(str(name))
            if info is not None:
                info.columns.append(ColumnInfo(str(column), str(kind), str(nullable) == "NO"))
        return sorted(found.values(), key=lambda t: t.name)

    async def locate(self, parts: Sequence[str]) -> tuple[str, str, list[str]] | None:
        if len(parts) == 1 and self._dataset:
            dataset, name = self._dataset, parts[0]
        elif len(parts) == 2:
            dataset, name = parts
        else:
            return None
        if not _DATASET.match(dataset):
            return None
        sink = _Rows()
        sql = (
            f"SELECT column_name FROM {self._info(dataset, 'COLUMNS')}"  # noqa: S608
            " WHERE table_name = ? ORDER BY ordinal_position"
        )
        await self._query(sql, [name], sink)
        if not sink.rows:
            return None
        return dataset, name, [str(row[0]) for row in sink.rows]

    def sample_sql(self, schema: str, table: str, columns: Sequence[str], limit: int) -> str:
        names = ", ".join(_quote(c) for c in columns) or "*"
        full = _quote(f"{self._project}.{schema}.{table}")
        return f"SELECT {names} FROM {full} LIMIT {int(limit)}"  # noqa: S608


async def _send(
    client: OutboundClient,
    method: str,
    url: str,
    headers: Mapping[str, str],
    *,
    params: Mapping[str, str] | None = None,
    body: object = None,
) -> dict[str, Any]:
    """One request. Returns the JSON object of the answer. An error answer is a
    ``DriverError``, and a network error is a ``DriverUnavailableError``."""
    try:
        result = await client.request(method, url, headers=headers, params=params, json=body)
    except BlockedUrlError as error:
        raise DriverError(f"the request was blocked: {message_of(error)}") from None
    except FetchError as error:
        raise DriverUnavailableError(f"BigQuery cannot be reached: {message_of(error)}") from None
    data = _json(result)
    if result.status_code >= 400:
        text = f"BigQuery answered: {_error_text(data, result.status_code)}"
        if result.status_code in UNAVAILABLE_CODES or result.status_code >= 500:
            raise DriverUnavailableError(text)
        raise DriverError(text)
    if not isinstance(data, dict):
        raise DriverError("BigQuery sent an answer that is not JSON")
    return data


def _assertion(settings: DbSettings, now: float) -> str:
    """The signed JWT of the service account."""
    claims = {
        "iss": settings.user,
        "sub": settings.user,
        "aud": TOKEN_URI,
        "scope": SCOPE,
        "iat": int(now),
        "exp": int(now) + 3600,
    }
    key_id = settings.options.get("key_id")
    headers = {"kid": key_id} if key_id else None
    try:
        return jwt.encode(claims, settings.password, algorithm="RS256", headers=headers)
    except (ValueError, TypeError, jwt.PyJWTError):
        raise DriverError("the service account private key is not valid") from None


async def _token(client: OutboundClient, settings: DbSettings) -> str:
    assertion = _assertion(settings, time.time())
    body = {"grant_type": GRANT, "assertion": assertion}
    try:
        data = await _send(client, "POST", TOKEN_URI, {}, body=body)
    except DriverUnavailableError:
        raise
    except DriverError as error:
        raise DriverError(f"Google refused the sign-in of the service account: {error}") from None
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        raise DriverError("Google sent no access token")
    return token


@contextlib.asynccontextmanager
async def open_session(
    settings: DbSettings, target: Target, context: EngineContext, *, write: bool
) -> AsyncIterator[SqlSession]:
    """A BigQuery session for one call. ``target`` is not used: every request goes through
    ``OutboundClient``, which checks the address itself. ``write`` is not used: the SQL check
    keeps a read from changing data."""
    del target, write
    policy = UrlPolicy(
        allow_hosts=("googleapis.com",),
        max_redirects=0,
        max_bytes=MAX_ANSWER_BYTES,
        hop_timeout_s=context.timeout_s + 10,
        total_timeout_s=context.timeout_s + 15,
    )
    client = OutboundClient(policy, resolver=context.resolver, transport=context.transport)
    try:
        token = await _token(client, settings)
        yield BigQuerySession(settings, context, client, token)
    finally:
        await client.aclose()


__all__ = ["BigQuerySession", "open_session"]
