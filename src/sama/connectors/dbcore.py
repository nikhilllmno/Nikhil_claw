"""The parts of the database gateway that all engines use (docs/architecture.md, Section 10.4).

- ``RowCollector``: the rows of a result, for the model text and for the CSV file, with the
  limits of the gateway.
- ``TableInfo`` and ``render_tables``: the text of the ``schema`` tool.
- ``Target``: where a driver connects (a checked address, or the end of an SSH tunnel).
- ``SqlSession``: what an engine gives the gateway for one call.
- ``bind_marks``: the ``?`` marks of a statement, found with the tokenizer of the engine.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

import sqlglot
from sqlglot import TokenType
from sqlglot.errors import SqlglotError

from sama.connectors.http_api import DriverError
from sama.connectors.postgres import CUT_MARK, model_cell, to_csv

MAX_ROWS: Final = 1_000
"""The most rows in the model text."""
MAX_MODEL_BYTES: Final = 48_000
"""The most bytes of rows in the model text. With the notes and the untrusted wrapper, the
tool result stays under the 50,000-byte cap of the tool server (``MAX_OUTPUT_BYTES``)."""


@dataclass(frozen=True, slots=True)
class Rows:
    """The result of a statement, as the model sees it and as a CSV text."""

    columns: list[str]
    dropped: int
    shown: list[str]
    """The JSON lines of the rows for the model."""
    count: int
    complete: bool
    """False if more rows exist than were read."""
    csv: str | None
    """All rows that were read, as CSV, if they do not all fit in the model text."""
    status: str
    cut: bool = False
    """A shown value was cut to ``MAX_CELL_CHARS``; the CSV has the full value."""


class RowCollector:
    """Collects the rows of one result. Call ``start`` with the column names, ``add`` for
    each row (it returns ``False`` when the row does not fit: stop reading), and ``finish``.

    - The file stays in its limits (``max_rows`` and ``max_bytes`` in UTF-8): the row that
      does not fit is not in it, and the result is not complete.
    - The model text has at most ``show`` rows and ``MAX_MODEL_BYTES``. The rows after a row
      that does not fit are not shown.
    - A column with a name in ``drop`` (lower case) is left out.
    """

    def __init__(
        self,
        drop: frozenset[str],
        max_rows: int,
        max_bytes: int,
        *,
        keep_csv: bool = False,
        show: int = MAX_ROWS,
    ) -> None:
        self._drop = drop
        self._max_rows = max_rows
        self._max_bytes = max_bytes
        self._keep_csv = keep_csv
        self._show = show
        self._keep: list[int] | None = None
        self._names: list[str] = []
        self.columns: list[str] = []
        self._parts: list[str] = []
        self._written = 0
        self._shown: list[str] = []
        self._used = 0
        self._fits = True
        self._cut = False
        self.count = 0
        self.complete = True

    @property
    def started(self) -> bool:
        return self._keep is not None

    def start(self, names: Sequence[str]) -> None:
        self._names = [str(n) for n in names]
        self._keep = [i for i, n in enumerate(self._names) if n.lower() not in self._drop]
        self.columns = [self._names[i] for i in self._keep]
        header = _csv_line(self.columns)
        self._parts = [header]
        self._written = len(header.encode())
        self._used = len(json.dumps(self.columns, ensure_ascii=False)) + 1

    def add(self, row: Sequence[Any]) -> bool:
        """Adds one row (all columns, in the order of ``start``). ``False``: the row does not
        fit, and the caller stops."""
        if self._keep is None:
            raise RuntimeError("start the collector first")
        values = [row[i] for i in self._keep]
        line_csv = _csv_line([to_csv(v) for v in values])
        size_csv = len(line_csv.encode())
        if self.count >= self._max_rows or self._written + size_csv > self._max_bytes:
            self.complete = False
            return False
        self.count += 1
        self._parts.append(line_csv)
        self._written += size_csv
        if self._fits and len(self._shown) < self._show:
            cells = [model_cell(v) for v in values]
            line = json.dumps(cells, ensure_ascii=False, separators=(",", ":"))
            size = len(line.encode()) + 1
            if self._used + size <= MAX_MODEL_BYTES:
                self._shown.append(line)
                self._used += size
                self._cut = self._cut or any(_is_cut(c) for c in cells)
            else:
                self._fits = False
        return True

    def finish(self, status: str) -> Rows:
        if self._keep is None:
            return Rows([], 0, [], 0, True, None, status)
        everything = self.complete and len(self._shown) == self.count and not self._cut
        return Rows(
            columns=self.columns,
            dropped=len(self._names) - len(self._keep),
            shown=self._shown,
            count=self.count,
            complete=self.complete,
            csv="".join(self._parts) if self._keep_csv or not everything else None,
            status=status,
            cut=self._cut,
        )


def _csv_line(cells: Sequence[str]) -> str:
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerow(cells)
    return out.getvalue()


def _is_cut(cell: Any) -> bool:
    return isinstance(cell, str) and cell.endswith(CUT_MARK)


# -- the schema tool ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    name: str
    type: str
    not_null: bool = False
    comment: str | None = None


@dataclass(slots=True)
class TableInfo:
    """One table for the ``schema`` tool."""

    schema: str
    name: str
    kind: str = "table"
    comment: str | None = None
    columns: list[ColumnInfo] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    """Keys and indexes, for example ``primary key (id)``."""


def render_tables(
    tables: Sequence[TableInfo], *, hidden: Any, filtered: bool, word: str = "schema"
) -> str:
    """The text of the ``schema`` tool. ``hidden(table, column)`` is true for a hidden
    column: the column, and the details that name it, are left out."""
    if not tables:
        return "No tables match (or the user cannot read them)."
    out: list[str] = []
    used = 0
    shown = 0
    for table in tables:
        hidden_here = {c.name.lower() for c in table.columns if hidden(table.name, c.name)}
        block = [f"{table.schema}.{table.name} ({table.kind})"]
        if table.comment:
            block[0] += f" -- {table.comment[:300]}"
        for column in table.columns:
            if column.name.lower() in hidden_here:
                continue
            line = f"  {column.name} {column.type}"
            if column.not_null:
                line += " not null"
            if column.comment:
                line += f" -- {column.comment[:200]}"
            block.append(line)
        for detail in table.details:
            words = {w.strip('`"[]()').lower() for w in detail.replace(",", " ").split()}
            if words & hidden_here:
                continue
            block.append(f"  {detail}")
        text = "\n".join(block)
        if used + len(text.encode()) > MAX_MODEL_BYTES - 500:
            break
        out.append(text)
        used += len(text.encode()) + 1
        shown += 1
    if shown < len(tables):
        hint = "" if filtered else f" Give {word} or table to see the others."
        out.append(f"[Shown: {shown} of {len(tables)} tables.{hint}]")
    return "\n".join(out)


# -- engines ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Target:
    """Where a driver connects: a checked address, or ``127.0.0.1`` and the local port of an
    SSH tunnel. The driver sends the real host name for TLS (SNI and the certificate)."""

    host: str
    port: int


class SqlSession(Protocol):
    """One connection of an SQL engine, for one call, in a transaction that the engine ends
    (a read is always rolled back or is read only)."""

    async def rows(self, sql: str, params: Sequence[Any], sink: RowCollector) -> str:
        """Runs one checked statement and gives the rows to ``sink``. Returns the status (for
        example ``3 row(s) changed``). Raises ``DriverError`` with the message of the
        database."""
        ...

    async def tables(self, schema: str | None, table: str | None) -> list[TableInfo]:
        """The tables and columns that the user can read."""
        ...

    async def locate(self, parts: Sequence[str]) -> tuple[str, str, list[str]] | None:
        """The schema, the table, and the column names of ``[schema, ]table``, or ``None``."""
        ...

    def sample_sql(self, schema: str, table: str, columns: Sequence[str], limit: int) -> str:
        """A ``SELECT`` of some rows. Each name is quoted, and ``limit`` is an int."""
        ...


def bind_marks(sql: str, dialect: str) -> list[tuple[int, int]]:
    """The ``(start, end)`` of each ``?`` parameter mark, from the tokenizer of the engine. A
    ``?`` in a string or a comment is not a mark."""
    try:
        tokens = sqlglot.tokenize(sql, read=dialect)
    except SqlglotError:
        raise DriverError("the SQL cannot be read") from None
    return [(t.start, t.end + 1) for t in tokens if t.token_type == TokenType.PLACEHOLDER]


def check_params(sql: str, dialect: str, params: Sequence[Any]) -> list[tuple[int, int]]:
    """The marks of the statement. Raises ``DriverError`` if the count does not match."""
    marks = bind_marks(sql, dialect)
    if any(sql[a:b] != "?" for a, b in marks):
        raise DriverError("use ? for each parameter")
    if len(marks) != len(params):
        raise DriverError(f"the SQL has {len(marks)} parameters (?), and params has {len(params)}")
    return marks


def pyformat(sql: str, dialect: str, params: Sequence[Any]) -> str:
    """The statement for a driver with ``%s`` parameters: each ``?`` mark becomes ``%s``,
    and each other ``%`` becomes ``%%``."""
    marks = check_params(sql, dialect, params)
    if not params:
        return sql
    out: list[str] = []
    last = 0
    for start, end in marks:
        out.append(sql[last:start].replace("%", "%%"))
        out.append("%s")
        last = end
    out.append(sql[last:].replace("%", "%%"))
    return "".join(out)


def scalar(value: Any, index: int) -> Any:
    """One JSON parameter. Only a string, a number, a boolean, or null."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise DriverError(f"params[{index}] must be a string, a number, a boolean, or null")


def message_of(error: BaseException, limit: int = 400) -> str:
    """A short message of a driver error, on one line."""
    return " ".join(str(error).split())[:limit] or type(error).__name__


def jsonable(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
