"""The connector catalog: one YAML manifest for each connector (docs/architecture.md,
Section 10.3).

The manifests are in ``connectors/catalog/`` of the repo. A pull request changes them, and a
deploy ships them. A manifest gives:

- the name, the title, the description, and the kind of the connector;
- the scopes that a connection of the connector can have (``personal``, ``workspace``);
- for ``composio``: the Composio toolkit. Composio holds the credential;
- for ``http_api``: the form fields (a secret field goes to the vault, never into chat), the
  base URL, the auth header, and the test request;
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
from typing import Annotated, Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from sama.gate.decide import ToolAccess

ENV_DIRECTORY: Final = "SAMA_CONNECTORS_DIR"
CONNECTOR_NAME: Final = re.compile(r"[a-z][a-z0-9_-]{0,39}")
"""The same rule as the ``ck_connections_connector`` CHECK."""
_PATH_PARAM: Final = re.compile(r"\{([a-z_][a-z0-9_]{0,63})\}")

Scope = Literal["personal", "workspace"]
Kind = Literal["composio", "http_api"]
"""The kinds that this release can connect. The table allows more kinds (Section 10.3)."""


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


class Composio(_Strict):
    toolkit: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


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
    fields: tuple[FormField, ...] = ()
    tools: Annotated[tuple[ToolSpec, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _fits_kind(self) -> Manifest:
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
