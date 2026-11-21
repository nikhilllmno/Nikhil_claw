"""The SQL check of the database gateway (docs/architecture.md, Section 10.4).

The gateway parses each statement with sqlglot (the dialect of the engine: PostgreSQL,
MySQL, T-SQL, BigQuery, Snowflake, or ClickHouse) before it sends the statement to the
database. The check fails closed: a statement that does not parse, or that the check does not
know, is refused.

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
from contextvars import ContextVar
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
    r"|pg_stat_reset|pg_backup|pg_start_backup|pg_stop_backup|pg_wal_replay|pg_log_"
    r"|pg_signal|pg_restore_|pg_clear_|pg_sync_|pg_copy_|pg_set_|pg_stat_force"
    r"|ts_stat$|.*_to_xml|.*_to_xmlschema)",
    re.IGNORECASE,
)
"""Functions that read server files, run SQL from a string (``query_to_xml``, ``ts_stat``),
signal or end sessions, hold session locks on a pooled connection, or change the server
(statistics resets, backup and WAL control, stored statistics)."""
_MYSQL_DENIED: Final = re.compile(
    r"^(?:load_file|get_lock|release_lock|release_all_locks|is_free_lock|is_used_lock|benchmark"
    r"|sys_exec|sys_eval|master_pos_wait|source_pos_wait|wait_for_executed_gtid_set"
    r"|wait_until_sql_thread_after_gtids|uuid_to_bin_unsafe|lo_)$",
    re.IGNORECASE,
)
"""MySQL and MariaDB: functions that read server files, hold session locks on a pooled
connection, run user programs, or wait on replication."""
_TSQL_DENIED: Final = re.compile(
    r"^(?:openrowset|opendatasource|openquery|openxml|xp_.*|sp_.*|bulk|fn_xe_.*|fn_trace_.*"
    r"|fn_get_audit_file|fn_dblog|fn_dump_dblog|fn_get_sql)$",
    re.IGNORECASE,
)
"""SQL Server: functions that read files, logs, or other servers, and the system
procedures."""
_CLICKHOUSE_DENIED: Final = re.compile(
    r"^(?:file.*|url.*|s3.*|remote.*|cluster.*"
    r"|mysql.*|postgresql.*|mongodb.*|redis.*|sqlite.*|hdfs.*|jdbc|odbc|executable.*|input"
    r"|azure.*|gcs.*|hudi.*|deltalake.*|iceberg.*|hive.*|paimon.*|arrowflight.*|ytsaurus.*"
    r"|dictionary"
    r"|filesystem|fuzzjson|generaterandom|merge|view|loop|format|null|zeros.*|numbers_mt"
    r"|getsetting|getmacro|getserverport|currentprofiles|currentroles|enabledroles"
    r"|enabledprofiles|defaultprofiles|defaultroles|dictget.*|dicthas|dictisin|dictgetchildren"
    r"|dictgetdescendants|dictgethierarchy|joinget.*|columns)$",
    re.IGNORECASE,
)
"""ClickHouse: the table functions that read files, URLs, or other servers, the functions
that show the server settings, the functions that read dictionaries and Join tables (they
are not tables that the policy sees), and the ``COLUMNS`` matcher (it can name a hidden
column by a pattern)."""
_BIGQUERY_DENIED: Final = re.compile(r"^(?:external_query)$", re.IGNORECASE)
"""BigQuery: a query on another database through a connection."""
_SNOWFLAKE_DENIED: Final = re.compile(
    r"^(?:system\$.*|get_presigned_url|build_scoped_file_url|build_stage_file_url"
    r"|get_stage_location|get_relative_path|get_absolute_path|infer_schema|to_query|identifier"
    r"|result_scan|get_ddl)$",
    re.IGNORECASE,
)
"""Snowflake: the system functions (they can cancel queries or change the account), the
functions that read stages (files), and the functions that make SQL or a name from a text
(``TO_QUERY``, ``IDENTIFIER``): the check cannot see the tables and columns of that text."""
_SEQUENCE_FUNCTIONS: Final = re.compile(r"^(?:nextval|setval|getnextval)$", re.IGNORECASE)
"""Functions that take the next value of a sequence or set it. A sequence change is not
rolled back (SQL Server, MariaDB, Snowflake), so ``query`` refuses them."""
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
class Dialect:
    """The SQL rules of one engine."""

    name: str
    """The sqlglot dialect."""
    fold: Literal["lower", "upper", "keep"]
    """How the engine sees a name without quotes: PostgreSQL makes it lower case, Snowflake
    makes it upper case, and the other engines keep it. A quoted name keeps its case."""
    denied: re.Pattern[str]
    """Functions that the check refuses in every mode."""
    system_schemas: frozenset[str]
    """Schemas (lower case) that the check refuses when the tables are restricted. The
    ``schema`` tool shows the allowed tables."""
    table_functions: frozenset[str] | None = None
    """The functions (lower case) that a FROM item can be. ``None`` (PostgreSQL): every
    function that is not denied. The other engines fail closed: a table function can read
    files, URLs, or other servers, and the policy does not see its tables."""

    def fold_name(self, name: str) -> str:
        if self.fold == "lower":
            return name.lower()
        if self.fold == "upper":
            return name.upper()
        return name


POSTGRES: Final = Dialect(
    "postgres", "lower", _DENIED_FUNCTIONS, frozenset({"pg_catalog", "information_schema"})
)
_MYSQL_SYSTEM: Final = frozenset({"information_schema", "mysql", "performance_schema", "sys"})
MYSQL: Final = Dialect("mysql", "keep", _MYSQL_DENIED, _MYSQL_SYSTEM, frozenset({"json_table"}))
TSQL: Final = Dialect(
    "tsql",
    "keep",
    _TSQL_DENIED,
    frozenset({"sys", "information_schema"}),
    frozenset({"openjson", "string_split", "generate_series"}),
)
BIGQUERY: Final = Dialect(
    "bigquery",
    "keep",
    _BIGQUERY_DENIED,
    frozenset({"information_schema", "region-us"}),
    frozenset(),
)
SNOWFLAKE: Final = Dialect(
    "snowflake",
    "upper",
    _SNOWFLAKE_DENIED,
    frozenset({"information_schema", "account_usage"}),
    frozenset({"flatten", "split_to_table", "generator"}),
)
CLICKHOUSE: Final = Dialect(
    "clickhouse",
    "keep",
    _CLICKHOUSE_DENIED,
    frozenset({"system", "information_schema"}),
    frozenset({"numbers", "generate_series", "generateseries"}),
)

_DIALECT: ContextVar[Dialect] = ContextVar("sama_sql_dialect", default=POSTGRES)


@dataclass(frozen=True, slots=True)
class Policy:
    """The limits of one connection. The table names are as the engine sees a name without
    quotes (lower case for PostgreSQL). The hidden columns are lower case: they match any
    case, so they fail closed."""

    schemas: frozenset[str] = frozenset()
    """The allowed schemas. Empty means all."""
    tables: frozenset[tuple[str | None, str]] = frozenset()
    """The allowed tables as ``(schema, table)``; ``schema`` is ``None`` for any schema.
    Empty means all tables of the allowed schemas."""
    hidden: frozenset[tuple[str | None, str]] = frozenset()
    """The hidden columns as ``(table, column)``; ``table`` is ``None`` for every table."""
    ddl: bool = False
    fixed_path: tuple[str, ...] | None = None
    """``None``: PostgreSQL, where the gateway sets the search path. Else the schema where the
    engine finds a name without a schema: the database (MySQL, ClickHouse), the dataset
    (BigQuery), or the schema (Snowflake). ``()`` means that the gateway does not know it
    (SQL Server), so a restricted connection needs the schema in each name."""
    catalog: str | None = None
    """The database (SQL Server, Snowflake) or the project (BigQuery) of the connection. A
    table name can have this catalog part, and no other."""

    @property
    def restricted(self) -> bool:
        return bool(self.schemas or self.tables)

    def table_allowed(self, schema: str | None, table: str) -> bool:
        """Is the table allowed? Names are as PostgreSQL sees them (a quoted name keeps its
        case). ``schema`` is ``None`` for a name without a schema: PostgreSQL finds it
        through the search path of the transaction, so the name must be allowed in each
        schema of that path."""
        if schema is not None:
            if self.schemas and schema not in self.schemas:
                return False
            return not self.tables or bool({(None, table), (schema, table)} & self.tables)
        if self.fixed_path is not None:
            if not self.restricted:
                return True
            if self.fixed_path:
                # The engine reads the name in its fixed schema: check that schema too.
                return self.table_allowed(self.fixed_path[0], table)
            # The gateway does not know the schema (SQL Server): a name alone is safe only
            # when every schema is allowed and the name is allowed in each schema.
            return not self.schemas and (None, table) in self.tables
        if not self.tables or (None, table) in self.tables:
            # The search path is the allowed schemas, or all schemas are allowed.
            return True
        path = self.search_path()
        return bool(path) and all((s, table) in self.tables for s in path)

    def search_path(self) -> tuple[str, ...]:
        """The schemas of the search path of the transaction, or ``()`` for the default."""
        if self.fixed_path is not None:
            return ()
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


def check(sql: str, mode: Mode, policy: Policy, dialect: Dialect = POSTGRES) -> Checked:
    """Checks one statement. Raises ``SqlRejectedError`` with the reason."""
    token = _DIALECT.set(dialect)
    try:
        return _check(sql, mode, policy)
    finally:
        _DIALECT.reset(token)


def _check(sql: str, mode: Mode, policy: Policy) -> Checked:
    text = sql.strip()
    if not text:
        raise SqlRejectedError("the SQL is empty")
    if mode == "query":
        explain = _EXPLAIN.match(text) if _DIALECT.get() is POSTGRES else None
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


def _no_comments(text: str) -> None:
    """The engines other than PostgreSQL do not get SQL comments. A server can read a comment
    in a way that sqlglot does not: MySQL and MariaDB run the text of ``/*! ... */`` and
    ``/*M! ... */``, MySQL reads ``/*+ ... */`` as hints that can set variables, and SQL
    Server nests comments. So the check refuses all comments."""
    dialect = _DIALECT.get()
    if dialect is POSTGRES:
        return
    try:
        tokens = sqlglot.Dialect.get_or_raise(dialect.name).tokenize(text)
    except SqlglotError as error:
        raise SqlRejectedError(f"the SQL does not parse: {_short(error)}") from None
    if any(token.comments for token in tokens):
        raise SqlRejectedError("SQL comments are not allowed on this engine. Remove them")


def _one(text: str) -> exp.Expr:
    """The single statement of the text. More than one statement, or none, is refused."""
    _no_comments(text)
    logger = logging.getLogger("sqlglot")
    level = logger.level
    logger.setLevel(logging.ERROR)  # sqlglot warns when it falls back to a Command.
    try:
        parsed = [s for s in sqlglot.parse(text, read=_DIALECT.get().name) if s is not None]
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
        allowed = "SELECT, WITH, VALUES, or EXPLAIN" if _DIALECT.get() is POSTGRES else "SELECT"
        raise SqlRejectedError(
            f"query runs only {allowed}, not {_kind(statement).upper()}. Use execute to change data"
        )
    for node in statement.walk():
        if isinstance(node, _DML + _DDL):
            raise SqlRejectedError(f"query cannot run {_kind(node).upper()} (read only)")
        if isinstance(node, _NEVER):
            raise SqlRejectedError(_never_reason(node))
        if isinstance(node, exp.Select) and node.args.get("format") is not None:
            # ClickHouse FORMAT: the gateway reads the rows in its own format.
            raise SqlRejectedError("a FORMAT clause is not allowed. The gateway sets the format")
        if _DIALECT.get() is not POSTGRES and _takes_sequence_value(node):
            raise SqlRejectedError(
                "query cannot take a sequence value (NEXT VALUE FOR, NEXTVAL): the change is "
                "not rolled back. Use execute"
            )
    return _check_common(statement, policy)


def _takes_sequence_value(node: exp.Expr) -> bool:
    """``NEXT VALUE FOR s``, ``nextval(s)``, ``setval(...)``, or ``s.nextval``."""
    if type(node).__name__ == "NextValueFor":
        return True
    if isinstance(node, exp.Func) and _SEQUENCE_FUNCTIONS.match(_function_name(node)):
        return True
    return isinstance(node, exp.Column) and node.name.lower() == "nextval"


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
    columns to drop by name.

    Names follow PostgreSQL: a quoted name keeps its case, other names are lower case. A
    name without a schema is a CTE only where that CTE is visible (its own WITH and the
    queries inside it); in all other places it is a table."""
    derived: set[str] = set()
    """The names of relations that are not tables: CTEs, subqueries, and functions."""
    aliases: dict[str, set[str]] = {}
    """Alias (or name) to the table names that it can be, anywhere in the statement."""
    tables: list[str] = []
    """The tables of the statement."""
    for cte in statement.find_all(exp.CTE):
        derived.add(_alias_name(cte))
    dialect = _DIALECT.get()
    for node in statement.find_all(exp.Func):
        name = _function_name(node)
        if name and dialect.denied.match(name):
            raise SqlRejectedError(f"the function {name} is not allowed")
    for item in statement.walk():
        if type(item).__name__ == "DynamicIdentifier":
            # Snowflake IDENTIFIER('...'): a name from a text, which the check cannot see.
            raise SqlRejectedError("IDENTIFIER(...) is not allowed. Write the name")
        source = _relation_function(item)
        if source is not None and dialect.table_functions is not None:
            name = _function_name(source).lower()
            if name not in dialect.table_functions:
                raise SqlRejectedError(
                    f"the table function {name} is not allowed on this engine. Read a table"
                )
    for table in statement.find_all(exp.Table):
        alias = _alias_name(table)
        if not isinstance(table.this, exp.Identifier):
            if not isinstance(table.this, exp.Func):
                # For example a Snowflake stage (@stage): files, not a table.
                raise SqlRejectedError(f"{table.sql(dialect=dialect.name)} is not a table")
            # A function in FROM, for example generate_series(1, 3).
            if alias:
                derived.add(alias)
            continue
        name = _name(table.this)
        db = table.args.get("db")
        schema = _name(db) if isinstance(db, exp.Identifier) else None
        catalog = table.args.get("catalog")
        if catalog is not None:
            same = (
                isinstance(catalog, exp.Identifier)
                and policy.catalog is not None
                and _name(catalog) in (policy.catalog, dialect.fold_name(policy.catalog))
            )
            if not same:
                raise SqlRejectedError("a table name with another database part is not allowed")
        if schema is None and _is_cte(table, name):
            if alias:
                derived.add(alias)
            continue
        tables.append(name)
        aliases.setdefault(alias or name, set()).add(name)
        aliases.setdefault(name, set()).add(name)
        low = name.lower()
        if dialect is POSTGRES:
            if _STATISTICS.match(low) and (policy.hidden or policy.restricted):
                raise SqlRejectedError(f"{name} is not allowed on this connection")
            if policy.restricted and schema is None and low.startswith("pg_"):
                raise SqlRejectedError(f"{name} is not an allowed table")
        if policy.restricted and schema is not None and schema.lower() in dialect.system_schemas:
            raise SqlRejectedError(f"{schema}.{name} is not an allowed table. Use the schema tool")
        if not policy.table_allowed(schema, name):
            if schema is None and (
                policy.fixed_path == () or any(t == name for s, t in policy.tables if s is not None)
            ):
                raise SqlRejectedError(
                    f"give the schema of {name} (for example schema.{name}); the name alone "
                    "can be a table that is not allowed"
                )
            shown = f"{schema}.{name}" if schema else name
            raise SqlRejectedError(f"{shown} is not an allowed table on this connection")
    for relation in statement.find_all(exp.Subquery, exp.Lateral):
        if relation.alias:
            derived.add(_alias_name(relation))
    drop = policy.hidden_names(tables)
    if drop:
        _check_hidden(statement, policy, aliases, derived, drop)
    return drop


def _relation_function(node: exp.Expr) -> exp.Func | None:
    """The function of a FROM or JOIN item (``FROM f(...)``, ``TABLE(f(...))``,
    ``LATERAL f(...)``, ``CROSS APPLY s.f(...)``), or ``None``. An item that is not a table,
    a subquery, or a function gives a name that no list has (fail closed)."""
    rows = type(node).__name__ == "TableFromRows"
    if not (isinstance(node, (exp.Table, exp.Lateral)) or rows):
        return None
    inner = node.this
    if isinstance(inner, exp.Dot) and isinstance(inner.expression, exp.Func):
        inner = inner.expression  # A function with a schema: sys.dm_exec_sql_text(...).
    if isinstance(inner, exp.Func):
        return inner
    if isinstance(node, exp.Table) or isinstance(inner, (exp.Subquery, exp.Query)):
        return None
    return exp.Anonymous(this="unknown")


def _name(identifier: exp.Identifier) -> str:
    """A name as the engine sees it: a quoted name keeps its case."""
    text = str(identifier.this)
    return text if identifier.quoted else _DIALECT.get().fold_name(text)


def _alias_name(node: exp.Expr) -> str:
    alias = node.args.get("alias")
    if isinstance(alias, exp.TableAlias) and isinstance(alias.this, exp.Identifier):
        return _name(alias.this)
    return _DIALECT.get().fold_name(node.alias) if node.alias else ""


def _is_cte(table: exp.Table, name: str) -> bool:
    """Is the table name a CTE that is visible at this place? A CTE is visible in the query
    of its WITH, in the CTEs after it, and in itself and all CTEs of a WITH RECURSIVE. The
    queries inside these see it too, unless a nearer CTE has the same name (the same
    answer)."""
    trail: list[exp.Expr] = [table]
    node = table.parent
    while node is not None:
        with_ = node.args.get("with_")
        if isinstance(with_, exp.With):
            ctes = list(with_.expressions)
            visible = ctes
            if trail[-1] is with_:
                inner = trail[-2] if len(trail) >= 2 else None
                if not with_.args.get("recursive"):
                    index = next((i for i, c in enumerate(ctes) if c is inner), len(ctes))
                    visible = ctes[:index]
            if any(_alias_name(cte) == name for cte in visible):
                return True
        trail.append(node)
        node = node.parent
    return False


def _check_hidden(
    statement: exp.Expr,
    policy: Policy,
    aliases: dict[str, set[str]],
    derived: set[str],
    names: frozenset[str],
) -> None:
    """``names``: the hidden column names that apply to the tables of the statement."""
    relations = {r.lower() for r in set(aliases) | derived}
    for node in statement.find_all(exp.Select):
        if node.args.get("kind"):
            # BigQuery SELECT AS STRUCT / AS VALUE: the row is one value, so the gateway
            # cannot drop a hidden column from it by its name.
            raise SqlRejectedError(
                "SELECT AS STRUCT and SELECT AS VALUE are not allowed when columns are hidden"
            )
    if _DIALECT.get() is SNOWFLAKE and any(statement.find_all(exp.Parameter)):
        # Snowflake $1, $2: a column by its position, which can be a hidden column.
        raise SqlRejectedError(
            "$1, $2 (a column by position) are not allowed when columns are hidden"
        )
    for column in statement.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            _check_star(column, statement)
            continue
        name = column.name.lower()
        if name in names:
            qualifier = column.args.get("table")
            key = _name(qualifier) if isinstance(qualifier, exp.Identifier) else ""
            sources = _column_tables(column, key, aliases, derived) if key else None
            # Only a column of named tables that all do not hide it is safe. A column
            # without a table name, or of a subquery, can be the hidden column (fail closed).
            if not sources or any(policy.column_hidden(table, name) for table in sources):
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
        if alias.columns and not _function_alias(alias.parent):
            raise SqlRejectedError(
                "column names after an alias are not allowed when columns are hidden"
            )
    for item in statement.walk():
        # SQL Server FOR JSON and FOR XML put all columns in one value.
        if type(item).__name__ == "ForClause":
            raise SqlRejectedError("FOR JSON and FOR XML are not allowed when columns are hidden")
    for dot in statement.find_all(exp.Dot):
        # (row).column reads a field of a whole row: ``(u).email``.
        if dot.name.lower() in names:
            raise SqlRejectedError(f"the column {dot.name} is hidden on this connection")


def _function_alias(node: exp.Expr | None) -> bool:
    """Is this the alias of a function, ``unnest``, or ``VALUES`` in FROM? Its column names
    cannot rename a column of a table. All other column names after an alias can (a table,
    a subquery, a ``LATERAL`` subquery, a CTE)."""
    if isinstance(node, (exp.Unnest, exp.Values)):
        return True
    if isinstance(node, (exp.Table, exp.Lateral)):
        return isinstance(node.this, exp.Func)
    return False


def _column_tables(
    column: exp.Column, key: str, aliases: dict[str, set[str]], derived: set[str]
) -> set[str] | None:
    """The tables that the qualifier ``key`` of the column can be. PostgreSQL looks for the
    qualifier in the FROM of the nearest query, then in the queries around it. ``None``: the
    qualifier is a subquery, a CTE, or a function, so the column can be any value."""
    node = column.parent
    while node is not None:
        if isinstance(node, exp.Select):
            for source in _sources(node):
                name = _alias_name(source)
                if (
                    not name
                    and isinstance(source, exp.Table)
                    and (isinstance(source.this, exp.Identifier))
                ):
                    name = _name(source.this)
                if name != key:
                    continue
                if (
                    isinstance(source, exp.Table)
                    and isinstance(source.this, exp.Identifier)
                    and not _is_cte(source, _name(source.this))
                ):
                    return {_name(source.this)}
                return None
        node = node.parent
    # Not found in a FROM (for example, the target of an UPDATE): all tables of the name.
    if key in derived:
        return None
    return aliases.get(key)


def _sources(select: exp.Select) -> list[exp.Expr]:
    """The relations in the FROM and the JOINs of one query."""
    found: list[exp.Expr] = []
    from_ = select.args.get("from_")
    if isinstance(from_, exp.From) and isinstance(from_.this, exp.Expr):
        found.append(from_.this)
    for join in select.args.get("joins") or []:
        if isinstance(join, exp.Join) and isinstance(join.this, exp.Expr):
            found.append(join.this)
    return found


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
        # A subquery right in a SELECT is a value (a column or a predicate), not a FROM item.
        value = isinstance(node, exp.Subquery) and isinstance(up, exp.Select)
        if up is None or value or not isinstance(up, _STAR_PATH):
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
        name = node.sql(dialect=_DIALECT.get().name).split("(", 1)[0].strip()
        return name.strip('"`[]')
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


def parse_names(text: str, dialect: Dialect = POSTGRES) -> list[str]:
    """A comma list of names (spaces and empty items are dropped), as the engine sees a name
    without quotes (lower case for PostgreSQL)."""
    return [dialect.fold_name(item.strip()) for item in text.split(",") if item.strip()]


def policy_of(
    *,
    schemas: Iterable[str] = (),
    tables: Iterable[str] = (),
    hidden_columns: Iterable[str] = (),
    ddl: bool = False,
    fixed_path: tuple[str, ...] | None = None,
    catalog: str | None = None,
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
        owner = table.rpartition(".")[2].lower()
        hidden.add((owner or None, column.lower()))
    return Policy(
        schemas=frozenset(schemas),
        tables=frozenset(allowed),
        hidden=frozenset(hidden),
        ddl=ddl,
        fixed_path=fixed_path,
        catalog=catalog,
    )
