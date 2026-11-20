"""The Snowflake engine of the database gateway (docs/architecture.md, Section 10.4).

The engine uses the Snowflake SQL API (version 2) over HTTPS, with key-pair authentication
(a signed JWT). It needs no driver. Every request goes through ``OutboundClient``, which
checks the address of the host, so this engine needs no checked ``Target``.

- One call of the gateway is one ``SnowflakeSession``. It makes one JWT and uses it for all
  requests of the session.
- A statement that runs long gives status 202. The engine polls it until the time limit of
  the gateway, and then cancels it.
- A big result has partitions. The engine reads them in order, and stops when the collector
  does not accept a row.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import hashlib
import json
import math
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from sama.connectors.dbcore import (
    ColumnInfo,
    RowCollector,
    SqlSession,
    TableInfo,
    Target,
    check_params,
    scalar,
)
from sama.connectors.dbsettings import DbSettings
from sama.connectors.engines import EngineContext
from sama.connectors.http_api import DriverError, DriverUnavailableError
from sama.tools.outbound import BlockedUrlError, FetchError, OutboundClient, UrlPolicy

MAX_ANSWER_BYTES: Final = 25 * 1024 * 1024
"""The most bytes of one answer of the API."""
JWT_LIFETIME_S: Final = 1_800
"""The life of one JWT. Snowflake accepts at most one hour."""
POLL_FIRST_S: Final = 0.25
POLL_LAST_S: Final = 2.0
"""The wait before the first poll of a statement that runs, and the longest wait."""
QUERY_TAG: Final = "sama"
_EPOCH: Final = dt.datetime(1970, 1, 1)
_BINDING_TYPES: Final[dict[type, str]] = {bool: "BOOLEAN", int: "FIXED", float: "REAL"}


@dataclass(frozen=True, slots=True)
class _Column:
    name: str
    convert: Callable[[str], Any]


@contextlib.asynccontextmanager
async def open_session(
    settings: DbSettings, target: Target, context: EngineContext, *, write: bool
) -> AsyncIterator[SqlSession]:
    """A Snowflake session for one call. ``target`` is not used: ``OutboundClient`` checks
    the address of the API host."""
    token = _token(settings)
    client = OutboundClient(
        UrlPolicy(
            allow_hosts=("snowflakecomputing.com",),
            max_bytes=MAX_ANSWER_BYTES,
            hop_timeout_s=context.timeout_s + 10,
            total_timeout_s=context.timeout_s + 15,
        ),
        resolver=context.resolver,
        transport=context.transport,
    )
    try:
        yield SnowflakeSession(settings, client, token, context.timeout_s, write=write)
    finally:
        await client.aclose()


def _token(settings: DbSettings) -> str:
    """The JWT for the key-pair login of the user (the claims are in the Snowflake docs)."""
    try:
        key = serialization.load_pem_private_key(settings.password.encode(), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise DriverError(
            "the private key cannot be read (it must be an RSA key in PEM form, not encrypted)"
        ) from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise DriverError("the private key must be an RSA key")
    public = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    fingerprint = base64.b64encode(hashlib.sha256(public).digest()).decode()
    qualified = f"{_account(settings)}.{settings.user.upper()}"
    now = int(time.time())
    claims = {
        "iss": f"{qualified}.SHA256:{fingerprint}",
        "sub": qualified,
        "iat": now,
        "exp": now + JWT_LIFETIME_S,
    }
    return jwt.encode(claims, key, algorithm="RS256")


def _account(settings: DbSettings) -> str:
    """The account name for the JWT: upper case, without the region part."""
    name = settings.options.get("account", "").upper()
    if ".GLOBAL" in name:
        return name.partition("-")[0]
    return name.partition(".")[0]


class SnowflakeSession:
    """The ``SqlSession`` of Snowflake. Each statement is one request of the SQL API."""

    def __init__(
        self,
        settings: DbSettings,
        client: OutboundClient,
        token: str,
        timeout_s: float,
        *,
        write: bool,
    ) -> None:
        self._settings = settings
        self._client = client
        self._headers = {
            "Authorization": f"Bearer {token}",
            "X-Snowflake-Authorization-Token-Type": "KEYPAIR_JWT",
            "Accept": "application/json",
        }
        self._timeout_s = timeout_s
        self._write = write
        options = settings.options
        self._database = options.get("database") or settings.database
        self._schema = options.get("schema", "")

    # -- the SqlSession methods ---------------------------------------------------------

    async def rows(self, sql: str, params: Sequence[Any], sink: RowCollector) -> str:
        check_params(sql, "snowflake", params)
        bindings = _bindings(params)

        def take(columns: list[_Column]) -> None:
            sink.start([c.name for c in columns])

        return await self._execute(sql, bindings, take, sink.add)

    async def tables(self, schema: str | None, table: str | None) -> list[TableInfo]:
        sql = (
            "SELECT c.TABLE_SCHEMA, c.TABLE_NAME, t.TABLE_TYPE, t.COMMENT, c.COLUMN_NAME, "  # noqa: S608
            "c.DATA_TYPE, c.NUMERIC_PRECISION, c.NUMERIC_SCALE, c.IS_NULLABLE, c.COMMENT "
            f"FROM {_quote(self._database)}.INFORMATION_SCHEMA.COLUMNS c "
            f"JOIN {_quote(self._database)}.INFORMATION_SCHEMA.TABLES t "
            "ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME "
            "WHERE c.TABLE_SCHEMA <> 'INFORMATION_SCHEMA' AND c.TABLE_SCHEMA = ?"
        )
        values: list[Any] = [_fold_unquoted(schema) if schema else self._schema]
        if table:
            sql += " AND c.TABLE_NAME = ?"
            values.append(_fold_unquoted(table))
        sql += " ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.ORDINAL_POSITION"
        found: dict[tuple[str, str], TableInfo] = {}
        for row in await self._select(sql, values):
            owner, name, kind, comment, column, kind_of, precision, scale, nullable, note = row
            info = found.get((owner, name))
            if info is None:
                info = found[(owner, name)] = TableInfo(
                    schema=owner,
                    name=name,
                    kind=_kind(kind),
                    comment=comment or None,
                )
            info.columns.append(
                ColumnInfo(
                    name=column,
                    type=_type_name(kind_of, precision, scale),
                    not_null=str(nullable).upper() == "NO",
                    comment=note or None,
                )
            )
        return list(found.values())

    async def locate(self, parts: Sequence[str]) -> tuple[str, str, list[str]] | None:
        if not 1 <= len(parts) <= 2 or not all(parts):
            return None
        schema = _fold_unquoted(parts[0]) if len(parts) == 2 else self._schema
        table = _fold_unquoted(parts[-1])
        rows = await self._select(
            f"SELECT COLUMN_NAME FROM {_quote(self._database)}.INFORMATION_SCHEMA.COLUMNS "  # noqa: S608
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
            [schema, table],
        )
        if not rows:
            return None
        return schema, table, [str(row[0]) for row in rows]

    def sample_sql(self, schema: str, table: str, columns: Sequence[str], limit: int) -> str:
        names = ", ".join(_quote(c) for c in columns)
        path = ".".join(_quote(n) for n in (self._database, schema, table))
        return f"SELECT {names} FROM {path} LIMIT {int(limit)}"  # noqa: S608

    # -- statements ---------------------------------------------------------------------

    async def _select(self, sql: str, values: Sequence[Any]) -> list[list[Any]]:
        """All rows of a catalog query, as a list."""
        out: list[list[Any]] = []

        def give(row: Sequence[Any]) -> bool:
            out.append(list(row))
            return True

        await self._execute(sql, _bindings(values), lambda _columns: None, give)
        return out

    async def _execute(
        self,
        sql: str,
        bindings: dict[str, dict[str, Any]],
        take: Callable[[list[_Column]], None],
        give: Callable[[list[Any]], bool],
    ) -> str:
        """Runs one statement. Calls ``take`` with the columns, and ``give`` for each row
        until it returns ``False``. Returns the status."""
        deadline = time.monotonic() + self._timeout_s
        settings = self._settings
        options = settings.options
        body: dict[str, Any] = {
            "statement": sql,
            "timeout": max(1, math.ceil(self._timeout_s)),
            "warehouse": options.get("warehouse", ""),
            "database": self._database,
            "schema": self._schema,
            "parameters": {"MULTI_STATEMENT_COUNT": "1", "QUERY_TAG": QUERY_TAG},
        }
        if options.get("role"):
            body["role"] = options["role"]
        if bindings:
            body["bindings"] = bindings
        status, answer = await self._send(
            "POST", "/api/v2/statements", params={"requestId": str(uuid.uuid4())}, body=body
        )
        handle = str(answer.get("statementHandle") or "")
        wait = POLL_FIRST_S
        while status == 202:
            if not handle:
                raise DriverError("Snowflake sent no statement handle")
            if time.monotonic() + wait > deadline:
                await self._cancel(handle)
                raise DriverError(f"the statement stopped after {self._timeout_s:g} seconds")
            await asyncio.sleep(wait)
            wait = min(wait * 2, POLL_LAST_S)
            status, answer = await self._send("GET", f"/api/v2/statements/{handle}")
        return await self._read(handle, answer, take, give)

    async def _read(
        self,
        handle: str,
        answer: Mapping[str, Any],
        take: Callable[[list[_Column]], None],
        give: Callable[[list[Any]], bool],
    ) -> str:
        meta = _dict(answer.get("resultSetMetaData"))
        stats = _dict(answer.get("stats")) or _dict(meta.get("stats"))
        changed = _changed(stats)
        if changed is not None:
            # A statement that changes rows gives a count, not rows.
            return f"{changed} row(s) changed"
        columns = [_column(item) for item in _list(meta.get("rowType"))]
        take(columns)
        parts = max(1, len(_list(meta.get("partitionInfo"))))
        for number in range(parts):
            if number:
                _, answer = await self._send(
                    "GET", f"/api/v2/statements/{handle}", params={"partition": str(number)}
                )
            for raw in _list(answer.get("data")):
                if not isinstance(raw, list) or len(raw) != len(columns):
                    raise DriverError("Snowflake sent a row that cannot be read")
                row = [
                    None if v is None else c.convert(v) for c, v in zip(columns, raw, strict=True)
                ]
                if not give(row):
                    return ""
        return ""

    async def _cancel(self, handle: str) -> None:
        """Asks Snowflake to stop the statement. A failure here does not matter."""
        with contextlib.suppress(DriverError):
            await self._send("POST", f"/api/v2/statements/{handle}/cancel")

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: object = None,
    ) -> tuple[int, dict[str, Any]]:
        """One request. Returns the status (200 or 202) and the JSON answer."""
        url = f"https://{self._settings.host}{path}"
        try:
            result = await self._client.request(
                method, url, headers=self._headers, params=params, json=body
            )
        except BlockedUrlError as error:
            raise DriverError(f"the Snowflake address is not allowed: {error}") from None
        except FetchError as error:
            raise DriverUnavailableError(f"Snowflake cannot be reached: {error}") from None
        if result.truncated:
            raise DriverError(
                "an answer of Snowflake is too large: select fewer columns or fewer rows"
            )
        code = result.status_code
        text = result.content.decode("utf-8", errors="replace")
        try:
            parsed: Any = _json(text)
        except ValueError:
            parsed = None
        answer = parsed if isinstance(parsed, dict) else {}
        if code in (200, 202):
            if parsed is None:
                raise DriverError("Snowflake sent an answer that cannot be read")
            return code, answer
        raise _failure(code, answer, text)


def _json(text: str) -> Any:
    return json.loads(text) if text.strip() else {}


def _failure(code: int, answer: Mapping[str, Any], text: str) -> DriverError:
    """The error for an answer that is not 200 or 202."""
    message = " ".join(str(answer.get("message") or "").split())[:400]
    state = str(answer.get("sqlState") or "")
    if code in (408, 429) or code >= 500:
        return DriverUnavailableError(f"Snowflake answered {code}: {message or 'try again'}")
    if not message:
        message = " ".join(text.split())[:200] or f"status {code}"
    suffix = f" (SQLSTATE {state})" if state else ""
    return DriverError(f"Snowflake answered: {message}{suffix}")


# -- values -------------------------------------------------------------------------------


def _bindings(params: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """The ``bindings`` of the SQL API. Each value is a text, with its type."""
    out: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(params):
        value = scalar(raw, index)
        if value is None:
            out[str(index + 1)] = {"type": "TEXT", "value": None}
            continue
        kind = _BINDING_TYPES.get(type(value), "TEXT")
        if kind == "BOOLEAN":
            text = "true" if value else "false"
        elif kind == "REAL" and not math.isfinite(value):
            raise DriverError(f"params[{index}] must be a finite number")
        else:
            text = str(value)
        out[str(index + 1)] = {"type": kind, "value": text}
    return out


def _column(item: Any) -> _Column:
    info = _dict(item)
    name = str(info.get("name") or "")
    kind = str(info.get("type") or "").lower()
    scale = info.get("scale")
    return _Column(name, _converter(kind, scale if isinstance(scale, int) else 0))


def _converter(kind: str, scale: int) -> Callable[[str], Any]:
    """The function that makes a Python value of the text of one cell. A text that does not
    fit the type stays a text."""
    if kind == "fixed":
        return _guard(int if scale == 0 else Decimal)
    if kind == "real":
        return _guard(float)
    if kind == "boolean":
        return lambda v: str(v).lower() in ("true", "1")
    if kind == "date":
        return _guard(lambda v: (_EPOCH + dt.timedelta(days=int(v))).date())
    if kind == "time":
        return _guard(lambda v: (_EPOCH + _seconds(v)).time())
    if kind == "timestamp_ntz":
        return _guard(lambda v: _EPOCH + _seconds(v))
    if kind == "timestamp_ltz":
        return _guard(lambda v: (_EPOCH + _seconds(v)).replace(tzinfo=dt.UTC))
    if kind == "timestamp_tz":
        return _guard(_zoned)
    return lambda v: v


def _guard(function: Callable[[str], Any]) -> Callable[[str], Any]:
    def convert(value: str) -> Any:
        try:
            return function(value)
        except (ValueError, InvalidOperation, OverflowError):
            return value

    return convert


def _seconds(text: str) -> dt.timedelta:
    """The seconds of a Snowflake value (``12345.678900000``), with the exact fraction."""
    return dt.timedelta(microseconds=int(Decimal(text) * 1_000_000))


def _zoned(text: str) -> dt.datetime:
    """``<seconds> <offset>``: the offset is the minutes from UTC, plus 1440."""
    seconds, _, offset = text.partition(" ")
    zone = dt.timezone(dt.timedelta(minutes=int(offset) - 1440)) if offset else dt.UTC
    return (_EPOCH + _seconds(seconds)).replace(tzinfo=dt.UTC).astimezone(zone)


def _changed(stats: Mapping[str, Any]) -> int | None:
    """The count of rows that a statement changed, or ``None`` if it is not a change."""
    names = ("numRowsInserted", "numRowsUpdated", "numRowsDeleted")
    if not any(n in stats for n in names):
        return None
    total = 0
    for name in names:
        value = stats.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
    return total


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# -- names --------------------------------------------------------------------------------


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _fold_unquoted(name: str) -> str:
    """The name as Snowflake sees it: a quoted name keeps its case, another becomes upper
    case."""
    name = name.strip()
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        return name[1:-1].replace('""', '"')
    return name.upper()


def _kind(table_type: Any) -> str:
    text = str(table_type or "table").lower()
    return "table" if text == "base table" else text


def _type_name(kind: Any, precision: Any, scale: Any) -> str:
    name = str(kind or "").lower()
    if name == "number" and precision is not None and scale is not None:
        return f"number({precision},{scale})"
    return name
