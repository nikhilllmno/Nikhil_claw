"""The PostgreSQL side of the database gateway (docs/architecture.md, Section 10.4).

- ``DbSettings``: the form values of a connection, checked.
- ``pinned_tls``: a TLS context that sends the real host name (SNI, certificate check) while
  the socket goes to the checked IP address.
- ``to_json`` and ``to_csv``: the cell values for the model and for the files.
- ``coerce``: the JSON parameters as the types that the statement expects.
- The catalog queries of the ``schema`` tool.

Supabase and Neon are PostgreSQL: the same code connects to them.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import math
import re
import ssl
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import certifi

from sama.connectors.http_api import DriverError
from sama.connectors.manifest import Manifest, SslMode
from sama.connectors.sql import Policy, parse_names, policy_of

DEFAULT_PORT: Final = 5432
MAX_CELL_CHARS: Final = 1_000
"""A longer cell is cut in the model text. The files keep the full value."""

_HOST: Final = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62})(\.[a-z0-9]([a-z0-9-]{0,62}))*$")
_NAME: Final = re.compile(r"^[a-z_][a-z0-9_$]{0,62}$")
_ON: Final = frozenset({"on", "yes", "true", "1"})
_OFF: Final = frozenset({"", "off", "no", "false", "0"})
SSL_MODES: Final[tuple[SslMode, ...]] = ("require", "verify-full")


@dataclass(frozen=True, slots=True)
class DbSettings:
    """The checked form of one database connection. ``password`` never goes to a log."""

    host: str
    port: int
    database: str
    user: str
    password: str
    sslmode: SslMode
    writes: bool
    policy: Policy

    @classmethod
    def parse(cls, manifest: Manifest, values: Mapping[str, str]) -> DbSettings:
        """Raises ``DriverError`` with the field that is not valid."""
        host = (values.get("host") or "").strip().lower().rstrip(".")
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if not host:
            raise DriverError("the host is empty")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not _HOST.match(host):
                raise DriverError(
                    "the host must be a host name or an IP address, not a URL"
                ) from None
        port_text = (values.get("port") or "").strip()
        try:
            port = int(port_text) if port_text else DEFAULT_PORT
        except ValueError:
            raise DriverError("the port must be a number") from None
        if not 0 < port < 65_536:
            raise DriverError("the port must be 1 to 65535")
        database = (values.get("database") or "").strip()
        user = (values.get("user") or "").strip()
        if not database or not user:
            raise DriverError("the database and the user are required")
        default = manifest.database.sslmode if manifest.database is not None else "require"
        mode = (values.get("sslmode") or "").strip().lower() or default
        sslmode = next((m for m in SSL_MODES if m == mode), None)
        if sslmode is None:
            raise DriverError("the TLS mode must be require or verify-full")
        writes = _switch(values.get("writes"), "Allow writes")
        ddl = _switch(values.get("ddl"), "Allow schema changes")
        if ddl and not writes:
            raise DriverError("Allow schema changes needs Allow writes on")
        schemas = parse_names(values.get("schemas") or "")
        tables = parse_names(values.get("tables") or "")
        hidden = parse_names(values.get("hidden_columns") or "")
        for label, items, parts in (
            ("Allowed schemas", schemas, 1),
            ("Allowed tables", tables, 2),
            ("Hidden columns", hidden, 3),
        ):
            for item in items:
                names = item.split(".")
                if len(names) > parts or not all(_NAME.match(n) for n in names):
                    raise DriverError(f"{label}: {item[:60]} is not a valid name")
        return cls(
            host=host,
            port=port,
            database=database,
            user=user,
            password=values.get("password") or "",
            sslmode=sslmode,
            writes=writes,
            policy=policy_of(schemas=schemas, tables=tables, hidden_columns=hidden, ddl=ddl),
        )

    def fingerprint(self) -> str:
        """A hash of all values. A changed form gives a new pool."""
        data = json.dumps(
            [
                self.host,
                self.port,
                self.database,
                self.user,
                self.password,
                self.sslmode,
                self.writes,
                sorted(self.policy.schemas),
                sorted(map(str, self.policy.tables)),
                sorted(map(str, self.policy.hidden)),
                self.policy.ddl,
            ]
        )
        return hashlib.sha256(data.encode()).hexdigest()


def writes_on(fields: Mapping[str, str]) -> bool:
    """True if the ``writes`` field of a connection is on. A value that is not valid is off."""
    return (fields.get("writes") or "").strip().lower() in _ON


def _switch(value: str | None, label: str) -> bool:
    text = (value or "").strip().lower()
    if text in _ON:
        return True
    if text in _OFF:
        return False
    raise DriverError(f"{label} must be on or off")


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


def pinned_tls(hostname: str, mode: SslMode) -> ssl.SSLContext:
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


def model_cell(value: Any) -> Any:
    """``to_json``, with a long value cut to ``MAX_CELL_CHARS``."""
    cell = to_json(value)
    if isinstance(cell, str):
        return cell if len(cell) <= MAX_CELL_CHARS else cell[:MAX_CELL_CHARS] + "...[cut]"
    if isinstance(cell, (list, dict)):
        text = json.dumps(cell, ensure_ascii=False)
        if len(text) > MAX_CELL_CHARS:
            return text[:MAX_CELL_CHARS] + "...[cut]"
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
