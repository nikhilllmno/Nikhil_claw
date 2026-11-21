"""The ClickHouse engine (docs/architecture.md, Section 10.4, "What M4.13 builds").

The gateway uses the HTTP interface of ClickHouse (port 8443 with TLS) with httpx. Each
statement is one request to the checked target, with the real host name in the ``Host``
header and for TLS (SNI and the certificate).

- A read has ``readonly=1``, after the other settings of the request: the server refuses
  each write and each change of a setting (also a ``SETTINGS`` clause).
- Each statement stops after the time limit (``max_execution_time``). A read stops on the
  server when the gateway closes the request (for example after the row limit).
- ClickHouse has no transactions: a write is done at once.
- A ``?`` parameter becomes a ClickHouse parameter (``{p0:String}``), with the type of the
  JSON value.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Final

import httpx

from sama.connectors.dbcore import (
    ColumnInfo,
    RowCollector,
    TableInfo,
    Target,
    check_params,
    locate_by_tables,
    scalar,
)
from sama.connectors.dbsettings import DbSettings
from sama.connectors.engines import EngineContext
from sama.connectors.http_api import DriverError, DriverUnavailableError

FORMAT: Final = "JSONCompactEachRowWithNamesAndTypes"
_TIMEOUT_CODES: Final = frozenset({"159", "160", "209"})
_LOGIN_CODES: Final = frozenset({"192", "193", "516", "81"})

_COLUMNS_SQL: Final = """
SELECT c.database, c.table, t.engine, t.comment, c.name, c.type, c.comment,
       t.primary_key, t.sorting_key
FROM system.columns AS c
INNER JOIN system.tables AS t ON t.database = c.database AND t.name = c.table
WHERE c.database = {db:String}
  AND ({tb:Nullable(String)} IS NULL OR c.table = {tb:Nullable(String)})
ORDER BY c.table, c.position
"""


def quote(name: str) -> str:
    return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"


def _param(value: Any) -> tuple[str, str]:
    """The ClickHouse type and the text of one JSON value."""
    if value is None:
        return "Nullable(String)", "\\N"
    if isinstance(value, bool):
        return "Bool", "true" if value else "false"
    if isinstance(value, int):
        return "Int64", str(value)
    if isinstance(value, float):
        return "Float64", repr(value)
    text = str(value).replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")
    return "String", text


def bind(sql: str, params: Sequence[Any]) -> tuple[str, dict[str, str]]:
    """The statement with ``{pN:Type}`` for each ``?``, and the request parameters."""
    values = [scalar(v, i) for i, v in enumerate(params)]
    marks = check_params(sql, "clickhouse", values)
    out: list[str] = []
    query: dict[str, str] = {}
    last = 0
    for index, ((start, end), value) in enumerate(zip(marks, values, strict=True)):
        kind, text = _param(value)
        out.append(sql[last:start])
        out.append(f"{{p{index}:{kind}}}")
        query[f"param_p{index}"] = text
        last = end
    out.append(sql[last:])
    return "".join(out), query


class _Keep:
    """A sink that keeps all rows (for the catalog queries of the gateway)."""

    def __init__(self) -> None:
        self.rows: list[list[Any]] = []

    def start(self, names: Sequence[str]) -> None:
        del names

    def add(self, row: Sequence[Any]) -> bool:
        if len(self.rows) >= 100_000:
            raise DriverError("the catalog of the database is too large")
        self.rows.append(list(row))
        return True


class ClickHouseSession:
    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        settings: DbSettings,
        write: bool,
        timeout_s: float,
    ) -> None:
        self._client = client
        self._url = url
        self._settings = settings
        self._write = write
        self._timeout_s = timeout_s

    def _query(self, extra: Mapping[str, str]) -> dict[str, str]:
        query = {
            "database": self._settings.database,
            "default_format": FORMAT,
            "max_execution_time": str(max(1, math.ceil(self._timeout_s))),
            "output_format_json_quote_64bit_integers": "0",
            "wait_end_of_query": "0",
        }
        query.update(extra)
        if not self._write:
            # The settings above apply first. After readonly=1, the server refuses each
            # change of a setting, also a SETTINGS clause in the statement.
            query["cancel_http_readonly_queries_on_client_close"] = "1"
            query["readonly"] = "1"
        return query

    async def rows(self, sql: str, params: Sequence[Any], sink: RowCollector) -> str:
        text, extra = bind(sql, params)
        return await self._stream(text, extra, sink)

    async def _stream(self, sql: str, extra: Mapping[str, str], sink: RowCollector | _Keep) -> str:
        request = self._client.build_request(
            "POST",
            self._url,
            params=self._query(extra),
            content=sql.encode(),
            extensions=self._extensions(),
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.TimeoutException:
            raise DriverError(f"the statement stopped after {self._timeout_s:g} seconds") from None
        except httpx.HTTPError as error:
            raise DriverUnavailableError(
                f"ClickHouse at {self._settings.host} cannot be reached ({type(error).__name__})"
            ) from None
        try:
            if response.status_code != 200:
                body = (await response.aread())[:2000].decode("utf-8", "replace")
                raise _error(response.headers.get("x-clickhouse-exception-code"), body)
            lines = response.aiter_lines()
            names: list[str] | None = None
            async for line in lines:
                if not line:
                    continue
                try:
                    values = json.loads(line)
                except ValueError:
                    # Not a row: an error after the first rows. Only a "Code: <n>." text is
                    # shown, never the line (it can have the values of a row).
                    found = _CODE.search(line)
                    raise _error(None, found.group(0) if found else "") from None
                if names is None:
                    names = [str(n) for n in values]
                    await anext(lines, None)  # The types.
                    sink.start(names)
                    continue
                if not sink.add(values):
                    break
            if names is None:
                return _summary(response.headers.get("x-clickhouse-summary"))
        except httpx.TimeoutException:
            raise DriverError(f"the statement stopped after {self._timeout_s:g} seconds") from None
        except httpx.HTTPError as error:
            raise DriverUnavailableError(
                f"ClickHouse at {self._settings.host} closed the connection "
                f"({type(error).__name__})"
            ) from None
        finally:
            await response.aclose()
        return ""

    def _extensions(self) -> dict[str, Any]:
        if self._url.startswith("https:"):
            return {"sni_hostname": self._settings.host}
        return {}

    async def _fetch(self, sql: str, extra: Mapping[str, str]) -> list[list[Any]]:
        keep = _Keep()
        await self._stream(sql, extra, keep)
        return keep.rows

    async def tables(self, schema: str | None, table: str | None) -> list[TableInfo]:
        database = schema or self._settings.database
        extra = {"param_db": database, "param_tb": table if table is not None else "\\N"}
        found: dict[str, TableInfo] = {}
        for row in await self._fetch(_COLUMNS_SQL, extra):
            info = found.get(row[1])
            if info is None:
                kind = "view" if "View" in str(row[2]) else f"table, {row[2]}"
                info = found[row[1]] = TableInfo(row[0], row[1], kind, row[3] or None)
                if row[7]:
                    info.details.append(f"primary key: ({row[7]})")
                if row[8] and row[8] != row[7]:
                    info.details.append(f"order by: ({row[8]})")
            info.columns.append(
                ColumnInfo(row[4], row[5], not str(row[5]).startswith("Nullable"), row[6] or None)
            )
        return list(found.values())

    async def locate(self, parts: Sequence[str]) -> tuple[str, str, list[str]] | None:
        return await locate_by_tables(self, parts)

    def sample_sql(self, schema: str, table: str, columns: Sequence[str], limit: int) -> str:
        names = ", ".join(quote(c) for c in columns)
        return f"SELECT {names} FROM {quote(schema)}.{quote(table)} LIMIT {int(limit)}"  # noqa: S608


def _summary(header: str | None) -> str:
    try:
        summary = json.loads(header or "{}")
        written = int(summary.get("written_rows", 0))
    except (ValueError, TypeError, AttributeError):
        return ""
    return f"{written} row(s) changed" if written else ""


_CODE: Final = re.compile(r"Code: (\d+)\. (.*)")


def _error(code: str | None, body: str) -> DriverError:
    """The error of a ClickHouse answer. The text starts at ``Code: <n>.`` (the server can
    put it in a row of the output format)."""
    text = " ".join(body.split())
    found = _CODE.search(text)
    if found is not None:
        code = code or found.group(1)
        text = f"Code: {found.group(1)}. {found.group(2)}".rstrip('"]} ')
    text = text[:400]
    if code in _TIMEOUT_CODES:
        return DriverError(
            "the statement stopped after the time limit (max_execution_time). Make it faster, "
            "or use LIMIT"
        )
    if code in _LOGIN_CODES:
        return DriverError(f"the database refused the login: {text}")
    return DriverError(f"the database answered: {text}")


@contextlib.asynccontextmanager
async def open_session(
    settings: DbSettings, target: Target, context: EngineContext, *, write: bool
) -> AsyncIterator[ClickHouseSession]:
    tls = context.tls(settings)
    scheme = "https" if tls is not None else "http"
    host = f"[{target.host}]" if ":" in target.host else target.host
    url = f"{scheme}://{host}:{target.port}/"
    headers = {
        "Host": settings.host if settings.port in (80, 443) else f"{settings.host}:{settings.port}",
        "X-ClickHouse-User": settings.user,
        "X-ClickHouse-Key": settings.password,
        "Accept-Encoding": "identity",
        "User-Agent": "sama",
    }
    async with httpx.AsyncClient(
        verify=tls if tls is not None else False,
        headers=headers,
        timeout=httpx.Timeout(context.timeout_s + 5, connect=10),
        trust_env=False,
        follow_redirects=False,
    ) as client:
        yield ClickHouseSession(client, url, settings, write, context.timeout_s)
