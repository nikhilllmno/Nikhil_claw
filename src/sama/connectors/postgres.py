"""The PostgreSQL side of the database gateway (docs/architecture.md, Section 10.4).

- ``DbSettings`` (from ``sama.connectors.dbsettings``): the form values of a connection.
- ``pinned_tls``: a TLS context that sends the real host name (SNI, certificate check) while
  the socket goes to the checked IP address.
- ``to_json`` and ``to_csv``: the cell values for the model and for the files.
- ``coerce``: the JSON parameters as the types that the statement expects.
- The catalog queries of the ``schema`` tool.

Supabase and Neon are PostgreSQL: the same code connects to them.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import ssl
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import certifi

from sama.connectors.dbsettings import DEFAULT_PORT, DbSettings, TlsMode, writes_on
from sama.connectors.http_api import DriverError

MAX_CELL_CHARS: Final = 1_000
"""A longer cell is cut in the model text. The files keep the full value."""

_ON: Final = frozenset({"on", "yes", "true", "1"})
_OFF: Final = frozenset({"", "off", "no", "false", "0"})


class _PinnedContext(ssl.SSLContext):
    """A client TLS context with a fixed server host name. The connection goes to a checked
    IP address, so asyncpg gives the IP as the host name. This context sends the real name
    instead: SNI works (Neon routes by SNI), and ``verify-full`` checks the real name."""

    pinned_hostname: str | None = None

    def wrap_bio(
        self,
        incoming: ssl.MemoryBIO,
        outgoing: ssl.MemoryBIO,
        server_side: bool = False,
        server_hostname: str | bytes | None = None,
        session: ssl.SSLSession | None = None,
    ) -> ssl.SSLObject:
        return super().wrap_bio(
            incoming,
            outgoing,
            server_side=server_side,
            server_hostname=self.pinned_hostname or server_hostname,
            session=session,
        )


def pinned_tls(hostname: str, mode: TlsMode) -> ssl.SSLContext:
    """The TLS context for a connection to ``hostname`` at a pinned address. ``require``
    encrypts and does not check the certificate (as libpq ``sslmode=require``);
    ``verify-full`` checks the certificate chain and the host name."""
    context = _PinnedContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.pinned_hostname = hostname
    if mode == "verify-full":
        context.load_verify_locations(certifi.where())
        context.load_default_certs()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
    else:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


# -- values -------------------------------------------------------------------------------


def to_json(value: Any) -> Any:
    """A JSON value for a cell. Exact numbers stay exact as text."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(value).hex()
    if isinstance(value, Mapping):
        return {str(k): to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    if hasattr(value, "items") and callable(value.items):  # asyncpg Record
        return [to_json(v) for _, v in value.items()]
    return str(value)


CUT_MARK: Final = "...[cut]"


def model_cell(value: Any) -> Any:
    """``to_json``, with a long value cut to ``MAX_CELL_CHARS`` and ``CUT_MARK`` at the end.
    The gateway then keeps the full result in a file."""
    cell = to_json(value)
    if isinstance(cell, str):
        return cell if len(cell) <= MAX_CELL_CHARS else cell[:MAX_CELL_CHARS] + CUT_MARK
    if isinstance(cell, (list, dict)):
        text = json.dumps(cell, ensure_ascii=False)
        if len(text) > MAX_CELL_CHARS:
            return text[:MAX_CELL_CHARS] + CUT_MARK
    return cell


def to_csv(value: Any) -> str:
    """A CSV cell: empty for NULL, the text for a string, else JSON."""
    if value is None:
        return ""
    cell = to_json(value)
    if isinstance(cell, str):
        return cell
    return json.dumps(cell, ensure_ascii=False)


_INTEGERS: Final = frozenset({"int2", "int4", "int8", "oid"})
_FLOATS: Final = frozenset({"float4", "float8"})
_TEXTS: Final = frozenset({"text", "varchar", "bpchar", "name", "char", "citext"})


def coerce(params: Sequence[Any], types: Sequence[str]) -> list[Any]:
    """The JSON parameters as the Python types that asyncpg needs for the parameter types
    of the statement. Raises ``DriverError`` for a count or a value that does not fit."""
    if len(params) != len(types):
        raise DriverError(f"the SQL has {len(types)} parameters, and params has {len(params)}")
    pairs = enumerate(zip(params, types, strict=True), 1)
    return [_coerce(value, kind, index) for index, (value, kind) in pairs]


def _coerce(value: Any, kind: str, index: int) -> Any:
    if value is None:
        return None
    try:
        if kind in _TEXTS:
            return value if isinstance(value, str) else json.dumps(value)
        if kind in ("json", "jsonb"):
            return value if isinstance(value, str) else json.dumps(value)
        if kind == "bool":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in _ON or text in ("t", "y"):
                return True
            if text in _OFF - {""} or text in ("f", "n"):
                return False
            raise ValueError(value)
        if kind in _INTEGERS:
            if isinstance(value, bool):
                raise ValueError(value)
            if isinstance(value, float):
                if not value.is_integer():
                    raise ValueError(value)
                return int(value)
            return int(value)
        if kind in _FLOATS:
            return float(value)
        if kind == "numeric":
            return Decimal(str(value))
        if kind == "uuid":
            return uuid.UUID(str(value))
        if isinstance(value, str):
            if kind == "date":
                return dt.date.fromisoformat(value)
            if kind in ("timestamp", "timestamptz"):
                return dt.datetime.fromisoformat(value)
            if kind in ("time", "timetz"):
                return dt.time.fromisoformat(value)
    except (ValueError, TypeError, InvalidOperation):
        raise DriverError(f"params[{index - 1}] (${index}) is not a valid {kind}") from None
    return value


# -- the catalog --------------------------------------------------------------------------

COLUMNS_SQL: Final = """
SELECT n.nspname AS schema, c.relname AS table, c.relkind::text AS kind, c.oid AS oid,
       obj_description(c.oid, 'pg_class') AS table_comment,
       a.attname AS column, format_type(a.atttypid, a.atttypmod) AS type,
       a.attnotnull AS not_null, col_description(c.oid, a.attnum) AS column_comment
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE c.relkind IN ('r', 'v', 'm', 'p', 'f')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_toast%' AND n.nspname NOT LIKE 'pg\\_temp%'
  AND ($1::text IS NULL OR n.nspname = $1)
  AND ($2::text IS NULL OR c.relname = $2)
  AND has_schema_privilege(n.oid, 'USAGE')
  AND (has_table_privilege(c.oid, 'SELECT') OR has_column_privilege(c.oid, a.attnum, 'SELECT'))
ORDER BY n.nspname, c.relname, a.attnum
"""
"""The columns that the user can read."""

CONSTRAINTS_SQL: Final = """
SELECT conrelid AS oid, contype::text AS kind, conname AS name,
       pg_get_constraintdef(oid) AS definition
FROM pg_catalog.pg_constraint
WHERE conrelid = ANY($1::oid[]) AND contype IN ('p', 'u', 'f')
ORDER BY conrelid, contype, conname
"""

INDEXES_SQL: Final = """
SELECT i.indrelid AS oid, pg_get_indexdef(i.indexrelid) AS definition
FROM pg_catalog.pg_index i
WHERE i.indrelid = ANY($1::oid[]) AND NOT i.indisprimary
ORDER BY i.indrelid, i.indexrelid
"""

RELATION_SQL: Final = """
SELECT n.nspname AS schema, c.relname AS table, c.oid AS oid
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.oid = to_regclass($1) AND c.relkind IN ('r', 'v', 'm', 'p', 'f')
"""

SAMPLE_COLUMNS_SQL: Final = """
SELECT a.attname AS column FROM pg_catalog.pg_attribute a
WHERE a.attrelid = $1 AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""

KINDS: Final = {"r": "table", "v": "view", "m": "materialized view", "p": "table", "f": "table"}


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def mentions(text: str, names: frozenset[str]) -> bool:
    """Does an index or constraint definition name one of the columns?"""
    words = {w.strip('"').lower() for w in re.findall(r'"(?:[^"]|"")+"|[A-Za-z_][\w$]*', text)}
    return bool(words & names)


__all__ = ["DEFAULT_PORT", "DbSettings", "writes_on"]
