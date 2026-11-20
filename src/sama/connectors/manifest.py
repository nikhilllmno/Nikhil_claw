"""The connector catalog: one YAML manifest for each connector (docs/architecture.md,
Section 10.3).

The manifests are in ``connectors/catalog/`` of the repo. A pull request changes them, and a
deploy ships them. A manifest gives:

- the name, the title, the description, and the kind of the connector;
- the scopes that a connection of the connector can have (``personal``, ``workspace``);
- for ``composio``: the Composio toolkit. Composio holds the credential;
- for ``http_api``: the form fields (a secret field goes to the vault, never into chat), the
  base URL, the auth header, and the test request;
- for ``database``: the database engine (only ``postgres``). The model makes the form fields
  and the five tools ``db_<conn>_schema``, ``sample``, ``query``, ``export``, and ``execute``
  (Section 10.4). A manifest of this kind does not list fields or tools;
- for ``mcp_remote``: the URL of a remote MCP server, its auth (``oauth``, ``bearer``,
  ``header``, or ``none``), the include and exclude filters of its tools, and the ``access``
  of tools that the server describes wrongly. The tools come from the server (``tools/list``),
  so the manifest has none;
- the tools of the connector. Each tool has a name, a description, a JSON schema of its
  arguments, and an ``access``: ``read``, ``write``, or ``destructive``. A ``read`` tool gets
  ``allow`` and the other tools get ``ask`` (Section 10.3).

A manifest that is not valid stops the load with ``ManifestError``: a deploy must not ship a
broken connector.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Annotated, Any, Final, Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from sama.gate.decide import ToolAccess

ENV_DIRECTORY: Final = "SAMA_CONNECTORS_DIR"
CONNECTOR_NAME: Final = re.compile(r"[a-z][a-z0-9_-]{0,39}")
"""The same rule as the ``ck_connections_connector`` CHECK."""
_PATH_PARAM: Final = re.compile(r"\{([a-z_][a-z0-9_]{0,63})\}")

Scope = Literal["personal", "workspace"]
Kind = Literal["composio", "http_api", "database", "mcp_remote"]
"""The kinds that this release can connect. The table allows more kinds (Section 10.3)."""
DatabaseOperation = Literal["schema", "sample", "query", "export", "execute"]
DATABASE_OPERATIONS: Final[tuple[DatabaseOperation, ...]] = get_args(DatabaseOperation)
SslMode = Literal["require", "verify-full"]


class ManifestError(ValueError):
    """A manifest is not valid."""


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FormField(_Strict):
    """One field of the connect form of an ``http_api`` connector."""

    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
    label: Annotated[str, Field(min_length=1, max_length=80)]
    secret: bool = False
    """A secret field goes to the vault entry ``connection:<id>``. The API never returns it."""
    required: bool = True
    help: Annotated[str, Field(max_length=400)] = ""


class HttpAuth(_Strict):
    header: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9-]{0,63}$")]
    field: str
    """The form field with the credential."""
    prefix: Annotated[str, Field(max_length=20)] = ""


class HttpRequest(_Strict):
    method: Literal["GET", "POST", "PATCH", "PUT", "DELETE"] = "GET"
    path: Annotated[str, Field(pattern=r"^/[A-Za-z0-9_./{}-]*$", max_length=200)]


class HttpApi(_Strict):
    base_url: Annotated[str, Field(pattern=r"^https://[a-z0-9.-]+(:[0-9]{1,5})?$")]
    """Only https, and a host without a path: a tool path cannot leave the host."""
    auth: HttpAuth
    headers: Mapping[str, str] = Field(default_factory=dict)
    test: HttpRequest


McpAuth = Literal["oauth", "bearer", "header", "none"]
TOOL_PATTERN: Final = re.compile(r"[A-Za-z0-9_.*?/:-]{1,100}")
"""A tool filter: a shell-style pattern (``fnmatch``) of the remote tool name."""
MAX_PATTERNS: Final = 50
TOKEN_FIELD: Final = "token"  # noqa: S105 - a field name, not a password
"""The form field of the credential of a ``bearer`` or ``header`` MCP server."""


def check_patterns(patterns: tuple[str, ...]) -> tuple[str, ...]:
    if len(patterns) > MAX_PATTERNS:
        raise ValueError(f"at most {MAX_PATTERNS} tool patterns")
    for pattern in patterns:
        if TOOL_PATTERN.fullmatch(pattern) is None:
            raise ValueError(f"not a valid tool pattern: {pattern!r}")
    return patterns


class McpRemote(_Strict):
    """A remote MCP server (streamable HTTP)."""

    url: Annotated[str, Field(pattern=r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[^\s#?]*)?$")]
    auth: McpAuth
    header: Annotated[str | None, Field(pattern=r"^[A-Za-z][A-Za-z0-9-]{0,63}$")] = None
    """The header of the credential (``auth: header``)."""
    scope: Annotated[str | None, Field(max_length=400)] = None
    """The OAuth scope to ask for (``auth: oauth``). Without it, sama asks for the scopes that
    the server names."""
    include: tuple[str, ...] = ()
    """Only the remote tools that match one of these patterns. Empty: all tools."""
    exclude: tuple[str, ...] = ()
    """No remote tool that matches one of these patterns."""
    access: Mapping[str, ToolAccess] = Field(default_factory=dict)
    """The access of the remote tools that match a pattern. It wins over the hints of the
    server."""

    @model_validator(mode="after")
    def _valid(self) -> McpRemote:
        if (self.auth == "header") != (self.header is not None):
            raise ValueError("an MCP server has a header if and only if its auth is header")
        if self.header is not None and self.header.lower() in RESERVED_HEADERS:
            raise ValueError(f"sama sets the header {self.header}")
        check_patterns(self.include)
        check_patterns(self.exclude)
        check_patterns(tuple(self.access))
        return self


RESERVED_HEADERS: Final = frozenset(
    {
        "accept",
        "accept-encoding",
        "content-length",
        "content-type",
        "host",
        "mcp-protocol-version",
        "mcp-session-id",
        "transfer-encoding",
        "connection",
    }
)
"""Headers that the MCP client sets. A credential header cannot be one of them."""


def mcp_fields(auth: McpAuth, header: str | None) -> tuple[FormField, ...]:
    """The form of an MCP server: one secret field for a ``bearer`` or ``header`` credential,
    and no form for ``oauth`` and ``none``."""
    if auth == "bearer":
        label, help_text = "Access token", "The token goes into the Authorization header."
    elif auth == "header":
        label, help_text = "Header value", f"The value goes into the {header} header."
    else:
        return ()
    return (FormField(name=TOKEN_FIELD, label=label, secret=True, help=help_text),)


class Composio(_Strict):
    toolkit: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class Database(_Strict):
    """The database block of a ``database`` connector (Section 10.4)."""

    engine: Literal["postgres"]
    sslmode: SslMode = "require"
    """The TLS mode when the form leaves ``sslmode`` empty."""
    hints: Mapping[str, Annotated[str, Field(max_length=400)]] = Field(default_factory=dict)
    """Help texts for the standard form fields, by field name (for example where the
    provider shows the host)."""


class ToolSpec(_Strict):
    """One tool of a connector."""

    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    description: Annotated[str, Field(min_length=1, max_length=2_000)]
    access: ToolAccess
    input_schema: Mapping[str, Any] = Field(default_factory=lambda: {"type": "object"})
    slug: Annotated[str | None, Field(pattern=r"^[A-Z][A-Z0-9_]{0,99}$")] = None
    """The Composio tool slug (``composio`` connectors)."""
    request: HttpRequest | None = None
    """The HTTP request (``http_api`` connectors). A ``{name}`` in the path takes the argument
    with that name. The other arguments go into the query (``GET``, ``DELETE``) or the JSON
    body."""

    @model_validator(mode="after")
    def _schema_is_object(self) -> ToolSpec:
        if self.input_schema.get("type") != "object":
            raise ValueError(f"{self.name}: the input schema must describe an object")
        return self


class Manifest(_Strict):
    """One connector."""

    name: Annotated[str, Field(pattern=CONNECTOR_NAME.pattern)]
    title: Annotated[str, Field(min_length=1, max_length=60)]
    description: Annotated[str, Field(min_length=1, max_length=400)]
    kind: Kind
    scopes: Annotated[tuple[Scope, ...], Field(min_length=1)] = ("personal", "workspace")
    composio: Composio | None = None
    http: HttpApi | None = None
    database: Database | None = None
    mcp: McpRemote | None = None
    fields: tuple[FormField, ...] = ()
    tools: tuple[ToolSpec, ...] = ()
    custom: bool = False
    """A connection that a workspace admin added by URL. It is not in the catalog."""

    @model_validator(mode="before")
    @classmethod
    def _mcp_form(cls, data: Any) -> Any:
        """An ``mcp_remote`` manifest does not write its form: it follows from the auth."""
        if (
            isinstance(data, dict)
            and data.get("kind") == "mcp_remote"
            and "fields" not in data
            and isinstance(data.get("mcp"), dict)
        ):
            auth = data["mcp"].get("auth")
            header = data["mcp"].get("header")
            if auth in ("bearer", "header"):
                return {**data, "fields": mcp_fields(auth, header)}
        return data

    @model_validator(mode="before")
    @classmethod
    def _database_form_and_tools(cls, data: Any) -> Any:
        """A ``database`` connector gets the standard form and the five tools."""
        if not isinstance(data, dict) or data.get("kind") != "database":
            return data
        if "fields" in data or "tools" in data:
            raise ValueError("a database connector does not list fields or tools")
        block = data.get("database")
        name = data.get("name")
        if not isinstance(block, dict) or not isinstance(name, str):
            return data  # The field checks give the error.
        hints = block.get("hints") or {}
        if not isinstance(hints, dict):
            return data
        if unknown := sorted(set(hints) - {f["name"] for f in DATABASE_FIELDS}):
            raise ValueError(f"hints for unknown fields: {', '.join(unknown)}")
        fields = [
            {**f, "help": str(hints[f["name"]])} if f["name"] in hints else dict(f)
            for f in DATABASE_FIELDS
        ]
        title = data.get("title") if isinstance(data.get("title"), str) else name
        return {**data, "fields": fields, "tools": database_tools(name, str(title))}

    @model_validator(mode="after")
    def _fits_kind(self) -> Manifest:
        if self.kind == "database":
            if (
                self.database is None
                or self.http is not None
                or self.composio is not None
                or self.mcp is not None
            ):
                raise ValueError("a database connector has a database block only")
            for tool in self.tools:
                if tool.request is not None or tool.slug is not None:
                    raise ValueError("a database tool has no request and no slug")
                if self.operation(tool) is None:
                    raise ValueError(f"{tool.name}: not a tool of a database connector")
            return self
        if self.database is not None:
            raise ValueError("only a database connector has a database block")
        if self.kind == "mcp_remote":
            if self.mcp is None or self.composio is not None or self.http is not None:
                raise ValueError("an mcp_remote connector has an mcp block only")
            if self.tools:
                raise ValueError("an mcp_remote connector gets its tools from the server")
            if self.fields != mcp_fields(self.mcp.auth, self.mcp.header):
                raise ValueError(
                    f"an mcp_remote connector with auth {self.mcp.auth} has "
                    + (
                        "one secret field named token"
                        if self.mcp.auth in ("bearer", "header")
                        else "no form fields"
                    )
                )
            return self
        if self.mcp is not None:
            raise ValueError("only an mcp_remote connector has an mcp block")
        if not self.tools:
            raise ValueError(f"a {self.kind} connector has at least one tool")
        if self.kind == "composio":
            if self.composio is None or self.http is not None or self.fields:
                raise ValueError("a composio connector has a composio block and no form")
            if any(tool.slug is None or tool.request is not None for tool in self.tools):
                raise ValueError("each tool of a composio connector has a slug and no request")
        else:
            if self.http is None or self.composio is not None or not self.fields:
                raise ValueError("an http_api connector has an http block and form fields")
            names = {f.name for f in self.fields}
            if len(names) != len(self.fields):
                raise ValueError("two form fields have the same name")
            auth = next((f for f in self.fields if f.name == self.http.auth.field), None)
            if auth is None or not auth.secret or not auth.required:
                raise ValueError("the auth field must be a required secret form field")
            for tool in self.tools:
                if tool.request is None or tool.slug is not None:
                    raise ValueError("each tool of an http_api connector has a request")
                properties = tool.input_schema.get("properties") or {}
                for param in _PATH_PARAM.findall(tool.request.path):
                    if param not in properties or param not in (
                        tool.input_schema.get("required") or ()
                    ):
                        raise ValueError(f"{tool.name}: {param} must be a required argument")
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise ValueError("two tools have the same name")
        for tool in self.tools:
            if not tool.name.startswith(f"{self.name.replace('-', '_')}_"):
                raise ValueError(f"{tool.name}: a tool name starts with the connector name")
        return self

    @property
    def secret_fields(self) -> tuple[FormField, ...]:
        return tuple(f for f in self.fields if f.secret)

    def operation(self, tool: ToolSpec) -> DatabaseOperation | None:
        """The database operation of a tool of a ``database`` connector, else ``None``."""
        if self.kind != "database":
            return None
        rest = tool.name.removeprefix(database_tool_prefix(self.name))
        if rest == tool.name:
            return None
        return next((op for op in DATABASE_OPERATIONS if op == rest), None)

    def public(self) -> dict[str, Any]:
        """The connector for the web app: no schemas, and the fields without values."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "kind": self.kind,
            "scopes": list(self.scopes),
            "fields": [f.model_dump() for f in self.fields],
            "tools": [
                {"name": t.name, "description": t.description, "access": t.access}
                for t in self.tools
            ],
            "mcp": (
                None
                if self.mcp is None
                else {"url": self.mcp.url, "auth": self.mcp.auth, "header": self.mcp.header}
            ),
        }


def catalog_directory() -> Path:
    """The folder of the manifests: the environment variable, else ``connectors/catalog/`` of
    the repo, else of the current folder."""
    configured = os.environ.get(ENV_DIRECTORY)
    if configured:
        return Path(configured)
    repo = Path(__file__).resolve().parents[3] / "connectors" / "catalog"
    return repo if repo.is_dir() else Path.cwd() / "connectors" / "catalog"


class Catalog:
    """The manifests by name, and the connector of each tool."""

    def __init__(self, manifests: Mapping[str, Manifest]) -> None:
        self._manifests = dict(sorted(manifests.items()))
        self._tools: dict[str, tuple[Manifest, ToolSpec]] = {}
        for manifest in self._manifests.values():
            if manifest.custom:
                raise ManifestError(f"{manifest.name}: a catalog connector is not custom")
            for tool in manifest.tools:
                if tool.name in self._tools:
                    raise ManifestError(f"two connectors have the tool {tool.name}")
                self._tools[tool.name] = (manifest, tool)

    @classmethod
    def load(cls, directory: Path | None = None) -> Catalog:
        """Reads every ``*.yaml`` file of the folder. A folder that does not exist gives an
        empty catalog."""
        folder = directory or catalog_directory()
        manifests: dict[str, Manifest] = {}
        if not folder.is_dir():
            return cls(manifests)
        for path in sorted(folder.glob("*.yaml")):
            manifest = parse(path.read_text(encoding="utf-8"), source=path.name)
            if path.stem != manifest.name:
                raise ManifestError(f"{path.name}: the file name must be {manifest.name}.yaml")
            manifests[manifest.name] = manifest
        return cls(manifests)

    def __iter__(self) -> Iterator[Manifest]:
        return iter(self._manifests.values())

    def __len__(self) -> int:
        return len(self._manifests)

    def get(self, name: str) -> Manifest | None:
        return self._manifests.get(name)

    def tool(self, name: str) -> tuple[Manifest, ToolSpec] | None:
        """The connector and the spec of a connector tool, or ``None``."""
        return self._tools.get(name)

    def tool_names(self) -> frozenset[str]:
        return frozenset(self._tools)


def parse(text: str, *, source: str = "manifest") -> Manifest:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ManifestError(f"{source}: not valid YAML: {error}") from None
    if not isinstance(data, dict):
        raise ManifestError(f"{source}: a manifest is a mapping")
    try:
        return Manifest.model_validate(data)
    except ValidationError as error:
        raise ManifestError(f"{source}: {error}") from None


def path_params(path: str) -> list[str]:
    return _PATH_PARAM.findall(path)


# -- database connectors (Section 10.4) -----------------------------------------------------

MAX_SQL_CHARS: Final = 20_000
MAX_EXECUTE_SQL_CHARS: Final = 1_000
"""The SQL of ``execute`` must fit on the approval card in full."""
MAX_PARAMS: Final = 100
MAX_SAMPLE_ROWS: Final = 20

DATABASE_FIELDS: Final[tuple[dict[str, Any], ...]] = (
    {"name": "host", "label": "Host", "help": "The host name of the database server."},
    {
        "name": "port",
        "label": "Port",
        "required": False,
        "help": "The port of the database server. Empty means 5432.",
    },
    {"name": "database", "label": "Database", "help": "The name of the database."},
    {
        "name": "user",
        "label": "User",
        "help": (
            "A database user for sama. Give it only the access that the agent needs: a user "
            "without access to a table or a column is the strongest control."
        ),
    },
    {"name": "password", "label": "Password", "secret": True, "help": "The password."},
    {
        "name": "sslmode",
        "label": "TLS mode",
        "required": False,
        "help": (
            "require (encrypt, do not check the certificate) or verify-full (encrypt and "
            "check the certificate and the host name). Empty means the connector default."
        ),
    },
    {
        "name": "schemas",
        "label": "Allowed schemas",
        "required": False,
        "help": "A comma list of the schemas that the agent can read, for example public. "
        "Empty means all schemas that the user can read.",
    },
    {
        "name": "tables",
        "label": "Allowed tables",
        "required": False,
        "help": "A comma list of the tables that the agent can use, as schema.table or "
        "table. Empty means all tables of the allowed schemas.",
    },
    {
        "name": "hidden_columns",
        "label": "Hidden columns",
        "required": False,
        "help": "A comma list of columns that never show in a result, as column or "
        "table.column, for example email, users.password_hash.",
    },
    {
        "name": "writes",
        "label": "Allow writes (on or off)",
        "required": False,
        "help": "on lets the agent change rows with an approved db_<conn>_execute call. "
        "Empty means off.",
    },
    {
        "name": "ddl",
        "label": "Allow schema changes (on or off)",
        "required": False,
        "help": "on lets an approved db_<conn>_execute call also run DDL (CREATE, ALTER, "
        "DROP, TRUNCATE). It needs writes on. Empty means off.",
    },
)
"""The form of every ``database`` connector. Only ``password`` is a secret."""


def database_tool_prefix(connector: str) -> str:
    """``db_<conn>_``, where ``<conn>`` is the connector name with ``_`` for ``-``."""
    return f"db_{connector.replace('-', '_')}_"


_PARAMS_SCHEMA: Final[dict[str, Any]] = {
    "type": "array",
    "maxItems": MAX_PARAMS,
    "items": {"type": ["string", "number", "integer", "boolean", "null"]},
    "description": "The values of $1, $2, ... in the SQL. Use them for values from the user.",
}


def database_tools(connector: str, title: str) -> list[dict[str, Any]]:
    """The five tools of a ``database`` connector, as manifest data."""
    prefix = database_tool_prefix(connector)
    return [
        {
            "name": f"{prefix}schema",
            "access": "read",
            "description": (
                f"Show the schemas, tables, columns, types, keys, indexes, and comments of the "
                f"{title} database. Give schema or table to see less. Use this before you "
                "write SQL."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "schema": {"type": "string", "maxLength": 63},
                    "table": {"type": "string", "maxLength": 63},
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
                        "maxLength": 130,
                        "description": "schema.table or table.",
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
                f"Run one read-only SQL statement (SELECT, WITH, VALUES, or EXPLAIN) on the "
                f"{title} database (PostgreSQL). The statement runs in a read-only "
                "transaction and stops after 30 seconds. You get at most 1,000 rows and "
                "50 KB; the full result goes to a CSV file in the sandbox. Use LIMIT, and "
                "use export for large results."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "minLength": 1, "maxLength": MAX_SQL_CHARS},
                    "params": _PARAMS_SCHEMA,
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
                    "params": _PARAMS_SCHEMA,
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
                f"the {title} database. A person must approve each call, and the approval "
                "shows the SQL. DDL runs only if the connection allows it. The connection "
                "must allow writes."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_EXECUTE_SQL_CHARS,
                    },
                    "params": {**_PARAMS_SCHEMA, "maxItems": 20},
                },
                "required": ["sql"],
            },
        },
    ]
