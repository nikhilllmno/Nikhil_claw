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
- for ``mcp_hosted``: a stdio MCP server that sama runs in a connector host (Section 10.3.2):
  the pinned package (``npm`` or ``pypi``, an exact version), the environment variables that
  the form fields give, the egress allowlist, the memory limit, and the idle time. A custom
  connection (not in the catalog) can have a command instead of a package;
- for a custom ``http_api`` connection (``api``): the base URL and the auth (``api_key``,
  ``bearer``, ``basic``, ``oauth2`` client credentials, or ``none``) of an API that a workspace
  admin imported. Its tools are the operations of the import (Section 10.3.3);
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
Kind = Literal["composio", "http_api", "database", "mcp_remote", "mcp_hosted"]
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


# -- hosted stdio MCP servers (Section 10.3.2) ------------------------------------------------

ENV_NAME: Final = re.compile(r"[A-Z][A-Z0-9_]{0,39}")
BLOCKED_ENV: Final = frozenset({"PATH", "HOME", "USER", "SHELL", "PWD", "TMPDIR", "IFS"})
BLOCKED_ENV_PREFIXES: Final = ("LD_", "DYLD_", "NODE_", "NPM_", "PYTHON", "PIP_", "UV_", "SAMA_")
"""Variables that change how the runtime loads code. A form field cannot set them."""
EGRESS_HOST: Final = re.compile(
    r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}"
)
MAX_EGRESS: Final = 20
MAX_ARGS: Final = 32
MAX_ARG_CHARS: Final = 500
REGISTRY_HOSTS: Final[Mapping[str, tuple[str, ...]]] = {
    "npm": ("registry.npmjs.org",),
    "pypi": ("pypi.org", "files.pythonhosted.org"),
}
"""The hosts that the host needs to get the package. They are always in the allowlist."""
DEFAULT_HOST_MEMORY_MB: Final = 384
DEFAULT_HOST_IDLE_S: Final = 600
_NPM_NAME: Final = re.compile(r"(@[a-z0-9][a-z0-9._-]{0,100}/)?[a-z0-9][a-z0-9._-]{0,100}")
_PYPI_NAME: Final = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._-]{0,98}[A-Za-z0-9])?")
_EXACT_VERSION: Final[Mapping[str, re.Pattern[str]]] = {
    # Full semver: npm reads ``1``, ``1.2``, and ``1.x`` as ranges.
    "npm": re.compile(
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
        r"(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?"
    ),
    # A PEP 440 release with an optional pre, post, or dev part. ``==`` pins it.
    "pypi": re.compile(r"[0-9]+(\.[0-9]+){0,3}((a|b|rc)[0-9]+)?(\.post[0-9]+)?(\.dev[0-9]+)?"),
}
"""One version for each registry. A range (``^``, ``~``, ``*``, ``>=``, ``x``, a space) or a
tag does not match."""
_BIN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def _check_args(args: tuple[str, ...], what: str) -> tuple[str, ...]:
    if len(args) > MAX_ARGS:
        raise ValueError(f"{what} has more than {MAX_ARGS} parts")
    for arg in args:
        if len(arg) > MAX_ARG_CHARS or "\x00" in arg or "\n" in arg:
            raise ValueError(f"{what} has a part that is too long or has a control character")
    return args


class McpPackage(_Strict):
    """The pinned package of a stdio MCP server. The host installs this version only."""

    registry: Literal["npm", "pypi"]
    name: Annotated[str, Field(min_length=1, max_length=214)]
    version: Annotated[str, Field(min_length=1, max_length=64)]
    bin: str | None = None
    """The program of the package. ``pypi`` default: the package name. ``npm`` default: the
    program that ``npx`` runs for the package."""
    args: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _valid(self) -> McpPackage:
        pattern = _NPM_NAME if self.registry == "npm" else _PYPI_NAME
        if pattern.fullmatch(self.name) is None:
            raise ValueError(f"not a valid {self.registry} package name: {self.name!r}")
        if _EXACT_VERSION[self.registry].fullmatch(self.version) is None:
            raise ValueError(f"the version must be one exact version, not {self.version!r}")
        if self.bin is not None and _BIN.fullmatch(self.bin) is None:
            raise ValueError(f"not a valid program name: {self.bin!r}")
        _check_args(self.args, "args")
        return self

    @property
    def pinned(self) -> str:
        """``name@version`` (npm) or ``name==version`` (pypi)."""
        joiner = "@" if self.registry == "npm" else "=="
        return f"{self.name}{joiner}{self.version}"


class McpHosted(_Strict):
    """A stdio MCP server that sama runs in its own connector host (an E2B sandbox)."""

    package: McpPackage | None = None
    command: tuple[str, ...] | None = None
    """A custom command (``argv``). Only a custom connection of the org owner has one."""
    env: Mapping[str, str] = Field(default_factory=dict)
    """Environment variable -> form field. The host process gets the value of the field."""
    egress: tuple[str, ...] = ()
    """The hosts that the server can reach. The package registry is always allowed."""
    memory_mb: Annotated[int, Field(ge=64, le=4096)] = DEFAULT_HOST_MEMORY_MB
    """The memory limit of the server processes. Above it, the host stops the server, and the
    next call starts it again."""
    idle_s: Annotated[int, Field(ge=60, le=3600)] = DEFAULT_HOST_IDLE_S
    """The host pauses after this time without a call."""
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    access: Mapping[str, ToolAccess] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _valid(self) -> McpHosted:
        if (self.package is None) == (self.command is None):
            raise ValueError("a hosted MCP server has a package or a command, not both")
        if self.command is not None:
            if not self.command or not self.command[0]:
                raise ValueError("the command is empty")
            _check_args(self.command, "the command")
        for name in self.env:
            if ENV_NAME.fullmatch(name) is None:
                raise ValueError(f"not a valid environment variable: {name!r}")
            if name in BLOCKED_ENV or name.startswith(BLOCKED_ENV_PREFIXES):
                raise ValueError(f"a form field cannot set {name}")
        if len(self.egress) > MAX_EGRESS:
            raise ValueError(f"at most {MAX_EGRESS} egress hosts")
        for host in self.egress:
            if EGRESS_HOST.fullmatch(host) is None:
                raise ValueError(f"not a valid host name: {host!r}")
        check_patterns(self.include)
        check_patterns(self.exclude)
        check_patterns(tuple(self.access))
        return self

    @property
    def allow_out(self) -> tuple[str, ...]:
        """The egress allowlist of the host: the egress hosts and the package registry."""
        hosts = set(self.egress)
        if self.package is not None:
            hosts.update(REGISTRY_HOSTS[self.package.registry])
        return tuple(sorted(hosts))


def hosted_fields(env: tuple[str, ...]) -> tuple[FormField, ...]:
    """The form of a custom stdio server: one required secret field for each variable."""
    return tuple(
        FormField(
            name=name.lower(),
            label=name,
            secret=True,
            help=f"The server gets the value as the environment variable {name}.",
        )
        for name in env
    )


# -- custom HTTP APIs (Section 10.3.3) --------------------------------------------------------

ApiAuthType = Literal["api_key", "bearer", "basic", "oauth2", "none"]
API_RESERVED_HEADERS: Final = frozenset(
    {"accept", "accept-encoding", "content-length", "content-type", "host", "transfer-encoding"}
)
_HTTPS_URL: Final = r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[^\s#?]*)?$"


class ApiAuth(_Strict):
    """The auth of a custom API. The credential is in the form, so the vault holds it."""

    type: ApiAuthType
    name: Annotated[str | None, Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")] = None
    """The header or the query parameter of the API key (``api_key``)."""
    location: Literal["header", "query"] = "header"
    token_url: Annotated[str | None, Field(pattern=_HTTPS_URL)] = None
    """The token endpoint (``oauth2``, the client credentials grant)."""
    scope: Annotated[str | None, Field(max_length=400)] = None
    client_auth: Literal["basic", "body"] = "basic"
    """How the client id and the client secret go to the token endpoint."""

    @model_validator(mode="after")
    def _valid(self) -> ApiAuth:
        if (self.type == "api_key") != (self.name is not None):
            raise ValueError("an API key auth has a name, and only it has one")
        if (self.type == "oauth2") != (self.token_url is not None):
            raise ValueError("an oauth2 auth has a token_url, and only it has one")
        if self.scope is not None and self.type != "oauth2":
            raise ValueError("only an oauth2 auth has a scope")
        if (
            self.name is not None
            and self.location == "header"
            and self.name.lower() in API_RESERVED_HEADERS
        ):
            raise ValueError(f"sama sets the header {self.name}")
        return self


class ApiSpec(_Strict):
    """A custom HTTP API: the base URL and the auth. The operations are the tools."""

    base_url: Annotated[str, Field(pattern=_HTTPS_URL, max_length=500)]
    """Only https. A path is allowed. A tool path goes after it."""
    auth: ApiAuth
    test: Annotated[str | None, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")] = None
    """The operation that the health check calls. Default: the first ``GET`` operation
    without required arguments."""


def api_fields(auth: ApiAuth) -> tuple[FormField, ...]:
    """The form of a custom API: the fields of its auth."""
    if auth.type == "api_key":
        where = "header" if auth.location == "header" else "query parameter"
        return (
            FormField(
                name="api_key",
                label="API key",
                secret=True,
                help=f"The key goes into the {auth.name} {where}.",
            ),
        )
    if auth.type == "bearer":
        return (
            FormField(
                name=TOKEN_FIELD,
                label="Bearer token",
                secret=True,
                help="The token goes into the Authorization header.",
            ),
        )
    if auth.type == "basic":
        return (
            FormField(name="username", label="User name"),
            FormField(name="password", label="Password", secret=True),
        )
    if auth.type == "oauth2":
        return (
            FormField(name="client_id", label="Client ID"),
            FormField(
                name="client_secret",
                label="Client secret",
                secret=True,
                help="sama gets a token from the token URL with the client credentials grant.",
            ),
        )
    return ()


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
    hosted: McpHosted | None = None
    api: ApiSpec | None = None
    fields: tuple[FormField, ...] = ()
    tools: tuple[ToolSpec, ...] = ()
    custom: bool = False
    """A connection that a workspace admin added (a server by URL, a stdio server, or an API).
    It is not in the catalog."""

    @model_validator(mode="before")
    @classmethod
    def _mcp_form(cls, data: Any) -> Any:
        """An ``mcp_remote`` manifest and a custom API do not write their form: it follows from
        the auth."""
        if not isinstance(data, dict) or "fields" in data:
            return data
        if data.get("kind") == "mcp_remote" and isinstance(data.get("mcp"), dict):
            auth = data["mcp"].get("auth")
            header = data["mcp"].get("header")
            if auth in ("bearer", "header"):
                return {**data, "fields": mcp_fields(auth, header)}
        if data.get("kind") == "http_api" and isinstance(data.get("api"), dict):
            auth = data["api"].get("auth")
            if isinstance(auth, dict):
                return {**data, "fields": api_fields(ApiAuth.model_validate(auth))}
            if isinstance(auth, ApiAuth):
                return {**data, "fields": api_fields(auth)}
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
        if self.hosted is not None and self.kind != "mcp_hosted":
            raise ValueError("only an mcp_hosted connector has a hosted block")
        if self.api is not None and self.kind != "http_api":
            raise ValueError("only an http_api connector has an api block")
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
        if self.kind == "mcp_hosted":
            return self._fits_hosted()
        if self.api is not None:
            return self._fits_api()
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

    def _fits_hosted(self) -> Manifest:
        hosted = self.hosted
        if hosted is None or self.composio or self.http or self.mcp or self.api:
            raise ValueError("an mcp_hosted connector has a hosted block only")
        if self.tools:
            raise ValueError("an mcp_hosted connector gets its tools from the server")
        if hosted.command is not None and not self.custom:
            raise ValueError("only a custom connection runs a command; a catalog one has a package")
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names):
            raise ValueError("two form fields have the same name")
        for variable, field in hosted.env.items():
            if field not in names:
                raise ValueError(
                    f"the variable {variable} names the form field {field}, "
                    "and the form has no such field"
                )
        return self

    def _fits_api(self) -> Manifest:
        api = self.api
        if api is None or self.composio or self.http or self.mcp or self.hosted:
            raise ValueError("a custom API has an api block only")
        if not self.custom:
            raise ValueError("only a custom connection has an api block")
        if self.tools:
            raise ValueError("the tools of a custom API are the operations of its import")
        if self.fields != api_fields(api.auth):
            raise ValueError(f"a custom API with auth {api.auth.type} has the form of that auth")
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
            "hosted": None if self.hosted is None else _hosted_public(self.hosted),
            "api": (
                None
                if self.api is None
                else {"base_url": self.api.base_url, "auth": self.api.auth.type}
            ),
        }


def _hosted_public(hosted: McpHosted) -> dict[str, Any]:
    """The hosted block for the web app: what runs and what it can reach. No values."""
    return {
        "package": None if hosted.package is None else hosted.package.model_dump(),
        "command": None if hosted.command is None else list(hosted.command),
        "env": sorted(hosted.env),
        "egress": list(hosted.allow_out),
        "memory_mb": hosted.memory_mb,
        "idle_s": hosted.idle_s,
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
