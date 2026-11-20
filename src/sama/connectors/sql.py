"""The SQL check of the database gateway (docs/architecture.md, Section 10.4).

The gateway parses each statement with sqlglot (PostgreSQL dialect) before it sends the
statement to the database. The check fails closed: a statement that does not parse, or that
the check does not know, is refused.

- ``query`` and ``export``: exactly one ``SELECT``, ``WITH ... SELECT``, ``VALUES``, set
  operation, or ``EXPLAIN`` of one of these. No node of the statement can write, lock, or
  change the session (a data-modifying CTE, ``SELECT INTO``, ``FOR UPDATE``, ``SET``).
- ``execute``: exactly one ``INSERT``, ``UPDATE``, ``DELETE``, or ``MERGE``. DDL (``CREATE``,
  ``ALTER``, ``DROP``, ``TRUNCATE``, ``COMMENT``, ``GRANT``, ``REVOKE``) only when the
  connection allows it. All other statements (``COPY``, ``CALL``, ``DO``, ``SET``,
  ``VACUUM``, ...) are refused.
- Functions that read files, run SQL from a string, signal other sessions, or change the
  server are refused in every mode.
- Allowed schemas and tables: each table of the statement must be allowed.
- Hidden columns: no reference to a hidden column anywhere in the statement, no whole-row
  reference, and a ``*`` only where the gateway can drop the hidden columns from the result
  by their names.

The read-only transaction is the first control for ``query``, and the grants of the database
user are the strongest control for tables and columns. This check is the layer in front of
them: it gives a clear error, and it closes the paths that a read-only transaction does not.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError

Mode = Literal["query", "execute"]

_EXPLAIN: Final = re.compile(
    r"^\s*explain\b\s*(?:\([^()]*\)\s*|(?:analy[sz]e|verbose)\b\s*)*", re.IGNORECASE
)
_DENIED_FUNCTIONS: Final = re.compile(
    r"^(?:pg_read|pg_ls|pg_stat_file|pg_file|lo_|dblink|pg_terminate_backend|pg_cancel_backend"
    r"|pg_reload_conf|pg_rotate_logfile|pg_promote|pg_switch_wal|pg_create|pg_drop"
    r"|pg_replication|pg_logical|pg_import|pg_advisory|pg_try_advisory|pg_notify|set_config"
    r"|ts_stat$|.*_to_xml|.*_to_xmlschema)",
    re.IGNORECASE,
)
"""Functions that read server files, run SQL from a string (``query_to_xml``, ``ts_stat``),
signal or end sessions, hold session locks on a pooled connection, or change the server."""
_STATISTICS: Final = re.compile(r"^(?:pg_stats|pg_statistic|pg_stats_ext.*)$", re.IGNORECASE)
"""Views of column statistics. Their sample values can show hidden or other data."""
_DDL_COMMANDS: Final = frozenset(
    {"CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT", "REVOKE", "COMMENT"}
)


def _classes(*names: str) -> tuple[type[exp.Expr], ...]:
    found = (getattr(exp, name, None) for name in names)
    return tuple(c for c in found if isinstance(c, type) and issubclass(c, exp.Expr))


_READ_ROOTS: Final = _classes("Select", "Union", "Intersect", "Except", "Values")
_DML: Final = _classes("Insert", "Update", "Delete", "Merge")
_DDL: Final = _classes("Create", "Drop", "Alter", "TruncateTable", "Comment", "Grant", "Revoke")
_NEVER: Final = _classes(
    "Command",
    "Copy",
    "Set",
    "Transaction",
    "Commit",
    "Rollback",
    "Lock",
    "Into",
    "Use",
    "Pragma",
    "Analyze",
    "Kill",
    "Cache",
    "Uncache",
    "Refresh",
    "Describe",
    "Show",
    "LoadData",
    "Detach",
    "Attach",
    "Install",
)
"""Nodes that no checked statement can hold (the root ``Command`` of DDL is checked first)."""
_STAR_PATH: Final = _classes("Select", "Subquery", "From", "Join", "Lateral", "CTE", "With")
"""The nodes between a ``*`` and the root through which a column keeps its name."""


class SqlRejectedError(ValueError):
    """The gateway does not send the statement. The message tells the model why."""


@dataclass(frozen=True, slots=True)
class Policy:
    """The limits of one connection. All names are lower case."""

    schemas: frozenset[str] = frozenset()
    """The allowed schemas. Empty means all."""
    tables: frozenset[tuple[str | None, str]] = frozenset()
    """The allowed tables as ``(schema, table)``; ``schema`` is ``None`` for any schema.
    Empty means all tables of the allowed schemas."""
    hidden: frozenset[tuple[str | None, str]] = frozenset()
    """The hidden columns as ``(table, column)``; ``table`` is ``None`` for every table."""
    ddl: bool = False

    @property
    def restricted(self) -> bool:
        return bool(self.schemas or self.tables)

    def table_allowed(self, schema: str | None, table: str) -> bool:
        """Is the table allowed? ``schema`` is ``None`` for a name without a schema; the
        search path of the transaction is then the allowed schemas."""
        schema = None if schema is None else schema.lower()
        table = table.lower()
        if self.schemas and schema is not None and schema not in self.schemas:
            return False
        if not self.tables:
            return True
        for allowed_schema, allowed_table in self.tables:
            if allowed_table != table:
                continue
            if allowed_schema is None or schema is None or allowed_schema == schema:
                return True
        return False

    def search_path(self) -> tuple[str, ...]:
        """The schemas of the search path of the transaction, or ``()`` for the default."""
        if self.schemas:
            return tuple(sorted(self.schemas))
        named = {s for s, _ in self.tables if s is not None}
        if self.tables and all(s is not None for s, _ in self.tables):
            return tuple(sorted(named))
        return ()

    def column_hidden(self, table: str, column: str) -> bool:
        """Is the column of this table hidden?"""
        column = column.lower()
        return (None, column) in self.hidden or (table.lower(), column) in self.hidden

    def hidden_names(self, tables: Iterable[str]) -> frozenset[str]:
        """The hidden column names that apply to a statement with these tables."""
        present = {t.lower() for t in tables}
        return frozenset(c for t, c in self.hidden if t is None or t in present)


@dataclass(frozen=True, slots=True)
class Checked:
    """A statement that passed the check."""

    sql: str
    """The statement to run (the text the model gave)."""
    kind: str
    """The root statement, for example ``select``, ``insert``, ``create``."""
    ddl: bool
    drop_columns: frozenset[str] = frozenset()
    """The result columns to drop by name: the hidden columns of the tables of the
    statement. A ``*`` can bring them."""


def check(sql: str, mode: Mode, policy: Policy) -> Checked:
    """Checks one statement. Raises ``SqlRejectedError`` with the reason."""
    text = sql.strip()
    if not text:
        raise SqlRejectedError("the SQL is empty")
    if mode == "query":
        explain = _EXPLAIN.match(text)
        if explain is not None and explain.end() > 0 and text[: explain.end()].strip():
            inner = text[explain.end() :]
            statement = _one(inner)
            drop = _check_read(statement, policy)
            return Checked(sql=text, kind="explain", ddl=False, drop_columns=drop)
        statement = _one(text)
        drop = _check_read(statement, policy)
        return Checked(sql=text, kind=_kind(statement), ddl=False, drop_columns=drop)
    statement = _one(text)
    return _check_execute(text, statement, policy)


def _one(text: str) -> exp.Expr:
    """The single statement of the text. More than one statement, or none, is refused."""
    logger = logging.getLogger("sqlglot")
    level = logger.level
    logger.setLevel(logging.ERROR)  # sqlglot warns when it falls back to a Command.
    try:
        parsed = [s for s in sqlglot.parse(text, read="postgres") if s is not None]
    except (ParseError, SqlglotError) as error:
        raise SqlRejectedError(f"the SQL does not parse: {_short(error)}") from None
    finally:
        logger.setLevel(level)
    if not parsed:
        raise SqlRejectedError("the SQL has no statement")
    if len(parsed) > 1:
        raise SqlRejectedError("give exactly one SQL statement (no ';' between statements)")
    return parsed[0]


def _kind(statement: exp.Expr) -> str:
    if isinstance(statement, exp.Command):
        return str(statement.name).lower()
    return type(statement).__name__.lower()


def _check_read(statement: exp.Expr, policy: Policy) -> frozenset[str]:
    if not isinstance(statement, _READ_ROOTS):
        raise SqlRejectedError(
            f"query runs only SELECT, WITH, VALUES, or EXPLAIN, not {_kind(statement).upper()}."
            " Use execute to change data"
        )
    for node in statement.walk():
        if isinstance(node, _DML + _DDL):
            raise SqlRejectedError(f"query cannot run {_kind(node).upper()} (read only)")
        if isinstance(node, _NEVER):
            raise SqlRejectedError(_never_reason(node))
    return _check_common(statement, policy)


def _check_execute(text: str, statement: exp.Expr, policy: Policy) -> Checked:
    ddl = isinstance(statement, _DDL) or (
        isinstance(statement, exp.Command) and str(statement.name).upper() in _DDL_COMMANDS
    )
    if ddl:
        if not policy.ddl:
            raise SqlRejectedError(
                "DDL is off for this connection. An admin can turn on 'Allow schema changes'"
            )
        if isinstance(statement, exp.Command):
            # sqlglot did not parse it, so the check cannot see the tables or the columns.
            if policy.restricted or policy.hidden:
                raise SqlRejectedError(
                    "this DDL cannot be checked against the allowed tables and hidden columns"
                )
            return Checked(sql=text, kind=_kind(statement), ddl=True)
    elif not isinstance(statement, _DML):
        raise SqlRejectedError(
            f"execute runs only INSERT, UPDATE, DELETE, or MERGE (and DDL if allowed), not "
            f"{_kind(statement).upper()}"
        )
    for node in statement.walk():
        if node is statement:
            continue
        if isinstance(node, _DDL) and not policy.ddl:
            raise SqlRejectedError("DDL is off for this connection")
        if isinstance(node, _NEVER):
            raise SqlRejectedError(_never_reason(node))
    drop = _check_common(statement, policy)
    return Checked(sql=text, kind=_kind(statement), ddl=ddl, drop_columns=drop)


def _never_reason(node: exp.Expr) -> str:
    if isinstance(node, exp.Into):
        return "SELECT INTO makes a table; it is not allowed"
    if isinstance(node, exp.Lock):
        return "row locks (FOR UPDATE, FOR SHARE) are not allowed"
    return f"{_kind(node).upper()} is not allowed"


def _check_common(statement: exp.Expr, policy: Policy) -> frozenset[str]:
    """The function, table, and hidden-column rules of every mode. Returns the result
    columns to drop by name."""
    ctes = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    derived: set[str] = set(ctes)
    """The names of relations that are not tables: CTEs, subqueries, and functions."""
    aliases: dict[str, str] = {}
    """Alias (or name) to table name, for the tables of the statement."""
    for node in statement.find_all(exp.Func):
        name = _function_name(node)
        if name and _DENIED_FUNCTIONS.match(name):
            raise SqlRejectedError(f"the function {name} is not allowed")
    for table in statement.find_all(exp.Table):
        alias = table.alias.lower() if table.alias else ""
        if not isinstance(table.this, exp.Identifier):
            # A function in FROM, for example generate_series(1, 3).
            if alias:
                derived.add(alias)
            continue
        name = table.name.lower()
        schema = table.db.lower() if table.db else None
        if table.catalog:
            raise SqlRejectedError("a table name with a database part is not allowed")
        if schema is None and name in ctes:
            if alias:
                derived.add(alias)
            continue
        aliases[alias or name] = name
        aliases.setdefault(name, name)
        if _STATISTICS.match(name) and (policy.hidden or policy.restricted):
            raise SqlRejectedError(f"{name} is not allowed on this connection")
        if policy.restricted and schema is None and name.startswith("pg_"):
            raise SqlRejectedError(f"{name} is not an allowed table")
        if policy.restricted and schema in ("pg_catalog", "information_schema"):
            raise SqlRejectedError(f"{schema}.{name} is not an allowed table. Use the schema tool")
        if not policy.table_allowed(schema, name):
            shown = f"{schema}.{name}" if schema else name
            raise SqlRejectedError(f"{shown} is not an allowed table on this connection")
    for subquery in statement.find_all(exp.Subquery):
        if subquery.alias:
            derived.add(subquery.alias.lower())
    drop = policy.hidden_names(aliases.values())
    if drop:
        _check_hidden(statement, policy, aliases, derived, drop)
    return drop


def _check_hidden(
    statement: exp.Expr,
    policy: Policy,
    aliases: dict[str, str],
    derived: set[str],
    names: frozenset[str],
) -> None:
    """``names``: the hidden column names that apply to the tables of the statement."""
    relations = set(aliases) | derived
    for column in statement.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            _check_star(column, statement)
            continue
        name = column.name.lower()
        if name in names:
            qualifier = column.table.lower() if column.table else ""
            table = aliases.get(qualifier) if qualifier else None
            # Only a column of a named table that does not hide it is safe. A column without
            # a table name, or of a subquery, can be the hidden column (fail closed).
            if table is None or policy.column_hidden(table, name):
                raise SqlRejectedError(f"the column {column.name} is hidden on this connection")
        if not column.table and name in relations:
            raise SqlRejectedError(
                f"{column.name} names a whole row here. Name the columns that you need"
            )
    for star in statement.find_all(exp.Star):
        if isinstance(star.parent, exp.Column):
            continue  # Checked with its column.
        if isinstance(star.parent, exp.Count):
            continue  # count(*) gives no column values.
        _check_star(star, statement)
    for alias in statement.find_all(exp.TableAlias):
        if alias.columns and isinstance(alias.parent, (exp.Subquery, exp.CTE)):
            raise SqlRejectedError(
                "column names after an alias are not allowed when columns are hidden"
            )
    for dot in statement.find_all(exp.Dot):
        # (row).column reads a field of a whole row: ``(u).email``.
        if dot.name.lower() in names:
            raise SqlRejectedError(f"the column {dot.name} is hidden on this connection")


def _check_star(star: exp.Expr, root: exp.Expr) -> None:
    """A ``*`` is allowed only as a column of a SELECT whose columns reach the result with
    their names: through FROM subqueries and CTEs, not set operations, functions, or
    predicates. The gateway then drops the hidden columns by name. ``RETURNING *`` of the
    root statement is allowed for the same reason."""
    parent = star.parent
    if isinstance(parent, exp.Returning) and parent.parent is root:
        return
    if not isinstance(parent, exp.Select) or star.arg_key != "expressions":
        raise SqlRejectedError(
            "* is allowed only as a column of a SELECT when columns are hidden. Name the "
            "columns that you need"
        )
    node: exp.Expr = parent
    while node is not root:
        up = node.parent
        if up is None or not isinstance(up, _STAR_PATH):
            raise SqlRejectedError(
                "* is allowed here only in the main SELECT, a FROM subquery, or a CTE when "
                "columns are hidden. Name the columns that you need"
            )
        node = up
    if not isinstance(root, exp.Select):
        raise SqlRejectedError("* is not allowed in this statement when columns are hidden")


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name)
    try:
        return node.sql(dialect="postgres").split("(", 1)[0].strip().strip('"')
    except SqlglotError:
        return type(node).__name__


def _short(error: Exception) -> str:
    """The first parse error, without the token dump and the terminal colors of sqlglot."""
    errors = getattr(error, "errors", None) or []
    if errors and isinstance(errors[0], dict):
        first = errors[0]
        where = f" (line {first.get('line')}, column {first.get('col')})"
        text = str(first.get("description") or "").split(" but got ")[0] + where
    else:
        text = str(error)
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", text).split())[:200]


def parse_names(text: str) -> list[str]:
    """A comma list of names (spaces and empty items are dropped), in lower case."""
    return [item.strip().lower() for item in text.split(",") if item.strip()]


def policy_of(
    *,
    schemas: Iterable[str] = (),
    tables: Iterable[str] = (),
    hidden_columns: Iterable[str] = (),
    ddl: bool = False,
) -> Policy:
    """A policy from the form lists. ``tables`` items are ``schema.table`` or ``table``;
    ``hidden_columns`` items are ``column`` or ``table.column``."""
    allowed: set[tuple[str | None, str]] = set()
    for item in tables:
        schema, _, table = item.rpartition(".")
        allowed.add((schema or None, table))
    hidden: set[tuple[str | None, str]] = set()
    for item in hidden_columns:
        table, _, column = item.rpartition(".")
        # schema.table.column: the table part is the last name before the column.
        hidden.add((table.rpartition(".")[2] or None, column))
    return Policy(
        schemas=frozenset(schemas),
        tables=frozenset(allowed),
        hidden=frozenset(hidden),
        ddl=ddl,
    )
