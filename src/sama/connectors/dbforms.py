"""The form and the tools of each database engine (docs/architecture.md, Sections 10.4 and
10.6).

A ``database`` manifest gives only its engine. This module makes the form fields and the
tools of that engine. It does not import a driver: the manifest loads without them.

The engines:

- ``postgres`` (also Supabase and Neon), ``mysql``, ``mariadb``, ``mssql`` (SQL Server),
  ``clickhouse``: a host and a port. An SSH tunnel can go to a host in a private network.
- ``bigquery`` and ``snowflake``: an HTTPS API at a fixed host, with a key in the vault.
- ``mongodb``: a host and a port. Its tools read with ``find`` and ``aggregate``, not SQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal, get_args

Engine = Literal[
    "postgres", "mysql", "mariadb", "mssql", "clickhouse", "bigquery", "snowflake", "mongodb"
]
ENGINES: Final[tuple[Engine, ...]] = get_args(Engine)

MAX_SQL_CHARS: Final = 20_000
MAX_EXECUTE_SQL_CHARS: Final = 1_000
"""The SQL of ``execute`` must fit on the approval card in full."""
MAX_PARAMS: Final = 100
MAX_SAMPLE_ROWS: Final = 20
MAX_BYTES_BILLED: Final = 1_000_000_000
"""The default limit of the bytes that one BigQuery query can bill (1 GB)."""


@dataclass(frozen=True, slots=True)
class EngineSpec:
    """The fixed facts of one engine."""

    name: Engine
    label: str
    """The name of the engine for people and for the model, for example ``MySQL``."""
    default_port: int | None
    """``None``: the engine is an HTTPS API at a fixed host (no host field, no tunnel)."""
    placeholder: Literal["$1", "?"]
    """How the SQL marks a parameter."""
    sql: bool = True
    """``False`` for MongoDB."""
    schema_word: str = "schema"
    """What the engine calls a schema: ``schema``, ``database``, or ``dataset``."""

    @property
    def network(self) -> bool:
        """The engine has a host and a port, so an SSH tunnel can apply."""
        return self.default_port is not None


SPECS: Final[dict[Engine, EngineSpec]] = {
    "postgres": EngineSpec("postgres", "PostgreSQL", 5432, "$1"),
    "mysql": EngineSpec("mysql", "MySQL", 3306, "?", schema_word="database"),
    "mariadb": EngineSpec("mariadb", "MariaDB", 3306, "?", schema_word="database"),
    "mssql": EngineSpec("mssql", "SQL Server", 1433, "?"),
    "clickhouse": EngineSpec("clickhouse", "ClickHouse", 8443, "?", schema_word="database"),
    "bigquery": EngineSpec("bigquery", "BigQuery", None, "?", schema_word="dataset"),
    "snowflake": EngineSpec("snowflake", "Snowflake", None, "?"),
    "mongodb": EngineSpec("mongodb", "MongoDB", 27017, "?", sql=False, schema_word="database"),
}


def _field(name: str, label: str, help_text: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "label": label, "help": help_text, **extra}


_USER_HELP: Final = (
    "A database user for sama. Give it only the access that the agent needs: a user "
    "without access to a table or a column is the strongest control."
)
_SSL_HELP: Final = (
    "require (encrypt, do not check the certificate) or verify-full (encrypt and check the "
    "certificate and the host name). Empty means the connector default. disable is allowed "
    "only through an SSH tunnel, which encrypts the connection."
)


def _connection_fields(spec: EngineSpec) -> list[dict[str, Any]]:
    port = spec.default_port
    database_help = {
        "mongodb": "The name of the database that the agent reads.",
        "clickhouse": "The default database. Empty means default.",
    }.get(spec.name, "The name of the database.")
    fields = [
        _field("host", "Host", "The host name of the database server."),
        _field(
            "port",
            "Port",
            f"The port of the database server. Empty means {port}.",
            required=False,
        ),
        _field(
            "database",
            "Database",
            database_help,
            required=spec.name not in ("clickhouse",),
        ),
        _field("user", "User", _USER_HELP, required=spec.name != "mongodb"),
        _field(
            "password",
            "Password",
            "The password.",
            secret=True,
            required=spec.name not in ("mongodb",),
        ),
    ]
    if spec.name == "mongodb":
        fields.append(
            _field(
                "auth_source",
                "Authentication database",
                "The database of the user. Empty means admin.",
                required=False,
            )
        )
    fields.append(_field("sslmode", "TLS mode", _SSL_HELP, required=False))
    return fields


def _policy_fields(spec: EngineSpec) -> list[dict[str, Any]]:
    if spec.name == "mongodb":
        return [
            _field(
                "tables",
                "Allowed collections",
                "A comma list of the collections that the agent can read. Empty means all "
                "collections of the database.",
                required=False,
            ),
            _field(
                "hidden_columns",
                "Hidden fields",
                "A comma list of fields that never show in a result, as field or "
                "collection.field, for example email, users.password_hash.",
                required=False,
            ),
        ]
    word = spec.schema_word
    example = {"database": "shop", "dataset": "analytics"}.get(word, "public")
    fields = [
        _field(
            "schemas",
            f"Allowed {word}s",
            f"A comma list of the {word}s that the agent can read, for example {example}. "
            f"Empty means all {word}s that the user can read.",
            required=False,
        ),
        _field(
            "tables",
            "Allowed tables",
            f"A comma list of the tables that the agent can use, as {word}.table or table. "
            f"Empty means all tables of the allowed {word}s.",
            required=False,
        ),
        _field(
            "hidden_columns",
            "Hidden columns",
            "A comma list of columns that never show in a result, as column or "
            "table.column, for example email, users.password_hash.",
            required=False,
        ),
        _field(
            "writes",
            "Allow writes (on or off)",
            "on lets the agent change rows with an approved db_<conn>_execute call. "
            "Empty means off.",
            required=False,
        ),
        _field(
            "ddl",
            "Allow schema changes (on or off)",
            "on lets an approved db_<conn>_execute call also run DDL (CREATE, ALTER, DROP, "
            "TRUNCATE). It needs writes on. Empty means off.",
            required=False,
        ),
    ]
    return fields


SSH_FIELDS: Final[tuple[dict[str, Any], ...]] = (
    _field(
        "ssh_host",
        "SSH host (optional)",
        "A bastion host with a public address. sama opens an SSH tunnel to it, and the "
        "bastion connects to the database host. Empty means no tunnel.",
        required=False,
    ),
    _field("ssh_port", "SSH port", "The SSH port of the bastion. Empty means 22.", required=False),
    _field("ssh_user", "SSH user", "The user on the bastion.", required=False),
    _field(
        "ssh_private_key",
        "SSH private key",
        "The private key of the SSH user (OpenSSH or PEM, without a passphrase). One line "
        "is accepted.",
        secret=True,
        required=False,
    ),
    _field(
        "ssh_host_key",
        "SSH host key",
        "The public host key of the bastion (for example ssh-ed25519 AAAA...) or its SHA256 "
        "fingerprint (SHA256:...). sama connects only to a bastion with this key.",
        required=False,
    ),
)

_BIGQUERY_FIELDS: Final[tuple[dict[str, Any], ...]] = (
    _field("project", "Project", "The ID of the Google Cloud project that runs the queries."),
    _field(
        "dataset",
        "Default dataset",
        "The dataset of a table name without a dataset. Empty means that each name needs one.",
        required=False,
    ),
    _field(
        "location",
        "Location",
        "The location of the datasets, for example US or EU. Empty means the default.",
        required=False,
    ),
    _field(
        "service_account_json",
        "Service account key (JSON)",
        "The JSON key of a service account with the BigQuery Job User role and read access "
        "to the datasets.",
        secret=True,
    ),
    _field(
        "max_bytes_billed",
        "Maximum bytes billed",
        f"A query that would bill more bytes fails and costs nothing. Empty means "
        f"{MAX_BYTES_BILLED:,} (1 GB).",
        required=False,
    ),
)

_SNOWFLAKE_FIELDS: Final[tuple[dict[str, Any], ...]] = (
    _field(
        "account",
        "Account",
        "The account identifier, for example myorg-myaccount (the first part of the "
        "host name myorg-myaccount.snowflakecomputing.com).",
    ),
    _field("user", "User", _USER_HELP),
    _field(
        "private_key",
        "Private key",
        "The RSA private key of the user for key-pair authentication (PEM, not encrypted). "
        "One line is accepted.",
        secret=True,
    ),
    _field("warehouse", "Warehouse", "The warehouse that runs the queries."),
    _field("database", "Database", "The database of a table name without a database."),
    _field("schema", "Schema", "The schema of a table name without a schema."),
    _field(
        "role", "Role", "The role of the queries. Empty means the user default.", required=False
    ),
)


def database_fields(engine: Engine) -> tuple[dict[str, Any], ...]:
    """The form of a ``database`` connector of this engine."""
    spec = SPECS[engine]
    if engine == "bigquery":
        return (*_BIGQUERY_FIELDS, *_policy_fields(spec))
    if engine == "snowflake":
        return (*_SNOWFLAKE_FIELDS, *_policy_fields(spec))
    return (*_connection_fields(spec), *_policy_fields(spec), *SSH_FIELDS)


DATABASE_FIELDS: Final = database_fields("postgres")
"""The form of a PostgreSQL connector."""


def database_tool_prefix(connector: str) -> str:
    """``db_<conn>_``, where ``<conn>`` is the connector name with ``_`` for ``-``."""
    return f"db_{connector.replace('-', '_')}_"


def _params_schema(spec: EngineSpec) -> dict[str, Any]:
    marks = "$1, $2, ..." if spec.placeholder == "$1" else "each ? (in order)"
    return {
        "type": "array",
        "maxItems": MAX_PARAMS,
        "items": {"type": ["string", "number", "integer", "boolean", "null"]},
        "description": f"The values of {marks} in the SQL. Use them for values from the user.",
    }


def database_tools(connector: str, title: str, engine: Engine = "postgres") -> list[dict[str, Any]]:
    """The tools of a ``database`` connector, as manifest data."""
    spec = SPECS[engine]
    if not spec.sql:
        return _document_tools(connector, title, spec)
    prefix = database_tool_prefix(connector)
    word = spec.schema_word
    params = _params_schema(spec)
    read_only = {
        "postgres": "The statement runs in a read-only transaction",
        "mysql": "The statement runs in a read-only transaction",
        "mariadb": "The statement runs in a read-only transaction",
        "mssql": "The statement runs in a transaction that is always rolled back",
        "clickhouse": "The statement runs with readonly=1",
        "bigquery": (
            "Only a SELECT runs, and a query that would bill too many bytes fails before it runs"
        ),
        "snowflake": "The statement must be a SELECT",
    }[engine]
    statements = "SELECT, WITH, VALUES, or EXPLAIN" if engine == "postgres" else "SELECT or WITH"
    return [
        {
            "name": f"{prefix}schema",
            "access": "read",
            "description": (
                f"Show the {word}s, tables, columns, types, keys, indexes, and comments of the "
                f"{title} database ({spec.label}). Give schema ({word}) or table to see less. "
                "Use this before you write SQL."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "schema": {"type": "string", "maxLength": 128},
                    "table": {"type": "string", "maxLength": 128},
                    "refresh": {
                        "type": "boolean",
                        "description": "Read the catalog again (the result is kept 10 min).",
                    },
                },
            },
        },
        {
            "name": f"{prefix}sample",
            "access": "read",
            "description": (
                f"Show some rows of one table of the {title} database (at most {MAX_SAMPLE_ROWS})."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "table": {
                        "type": "string",
                        "maxLength": 260,
                        "description": f"{word}.table or table.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SAMPLE_ROWS},
                },
                "required": ["table"],
            },
        },
        {
            "name": f"{prefix}query",
            "access": "read",
            "description": (
                f"Run one read-only SQL statement ({statements}) on the "
                f"{title} database ({spec.label} SQL). {read_only}, and it stops after 30 "
                "seconds. You get at most 1,000 rows and 50 KB; the full result goes to a CSV "
                "file in the sandbox. Use LIMIT, and use export for large results."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "minLength": 1, "maxLength": MAX_SQL_CHARS},
                    "params": params,
                },
                "required": ["sql"],
            },
        },
        {
            "name": f"{prefix}export",
            "access": "read",
            "description": (
                f"Run one read-only SQL statement on the {title} database and write all "
                "rows to a CSV file in the sandbox (.sama/exports/). You get the path and "
                "the row count, not the rows."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "minLength": 1, "maxLength": MAX_SQL_CHARS},
                    "params": params,
                    "format": {"type": "string", "enum": ["csv"]},
                },
                "required": ["sql"],
            },
        },
        {
            "name": f"{prefix}execute",
            "access": "destructive",
            "description": (
                f"Run one SQL statement that changes data (INSERT, UPDATE, DELETE, MERGE) on "
                f"the {title} database ({spec.label} SQL). A person must approve each call, "
                "and the approval shows the SQL. DDL runs only if the connection allows it. "
                "The connection must allow writes."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_EXECUTE_SQL_CHARS,
                    },
                    "params": {**params, "maxItems": 20},
                },
                "required": ["sql"],
            },
        },
    ]


def _document_tools(connector: str, title: str, spec: EngineSpec) -> list[dict[str, Any]]:
    """The tools of a MongoDB connector: read only, no SQL."""
    prefix = database_tool_prefix(connector)
    find = {
        "collection": {"type": "string", "minLength": 1, "maxLength": 120},
        "filter": {"type": "object", "description": "The find filter (MongoDB JSON)."},
        "projection": {"type": "object"},
        "sort": {"type": "object", "description": 'For example {"created": -1}.'},
        "limit": {"type": "integer", "minimum": 1},
        "pipeline": {
            "type": "array",
            "maxItems": 50,
            "items": {"type": "object"},
            "description": "An aggregate pipeline. Give pipeline or filter, not both.",
        },
    }
    return [
        {
            "name": f"{prefix}schema",
            "access": "read",
            "description": (
                f"Show the collections of the {title} database ({spec.label}), their indexes, "
                "and the fields of some documents. Give table (a collection) to see less."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "table": {"type": "string", "maxLength": 120},
                    "refresh": {
                        "type": "boolean",
                        "description": "Read the catalog again (the result is kept 10 min).",
                    },
                },
            },
        },
        {
            "name": f"{prefix}sample",
            "access": "read",
            "description": (
                f"Show some documents of one collection of the {title} database (at most "
                f"{MAX_SAMPLE_ROWS})."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "table": {"type": "string", "minLength": 1, "maxLength": 120},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SAMPLE_ROWS},
                },
                "required": ["table"],
            },
        },
        {
            "name": f"{prefix}query",
            "access": "read",
            "description": (
                f"Read documents of the {title} database ({spec.label}) with find (filter, "
                "projection, sort, limit) or aggregate (pipeline). Stages that write or run "
                "code ($out, $merge, $function, $where) are refused. The read stops after "
                "30 seconds. You get at most 1,000 documents and 50 KB; the full result goes "
                "to a CSV file in the sandbox (one JSON document for each row)."
            ),
            "input_schema": {
                "type": "object",
                "properties": find,
                "required": ["collection"],
            },
        },
        {
            "name": f"{prefix}export",
            "access": "read",
            "description": (
                f"Read documents of the {title} database as query does, and write all of them "
                "to a CSV file in the sandbox (.sama/exports/). You get the path and the "
                "count, not the documents."
            ),
            "input_schema": {
                "type": "object",
                "properties": {**find, "format": {"type": "string", "enum": ["csv"]}},
                "required": ["collection"],
            },
        },
    ]
