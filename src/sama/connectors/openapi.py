"""The import of a custom HTTP API: an OpenAPI 3 document or a list of endpoints
(docs/architecture.md, Section 10.3.3).

Each operation becomes one tool, ``api_<connection>_<tool>``. The tool keeps the request of
the operation (the method, the path, the path and query arguments, and the body), so the
agent gives only the arguments. A ``GET`` or ``HEAD`` operation reads, a ``DELETE`` operation
can delete things, and the other methods write.

The parser does not run code and does not follow a remote ``$ref``. It reads a document of at
most ``MAX_DOCUMENT_BYTES``, and it refuses YAML aliases, so a small document cannot expand.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

import yaml

from sama.connectors.mcp_remote import MAX_SCHEMA_BYTES, MAX_TOOLS, RemoteTool, parse_tool

MAX_DOCUMENT_BYTES: Final = 2_000_000
MAX_REF_DEPTH: Final = 10
MAX_SCHEMA_NODES: Final = 2_000
MAX_DESCRIPTION: Final = 1_000
METHODS: Final = ("get", "put", "post", "delete", "patch", "head")
BODY: Final = "body"
"""The argument of the request body."""

Method = Literal["GET", "PUT", "POST", "DELETE", "PATCH", "HEAD"]
Where = Literal["path", "query"]
"""Where an argument goes in the request."""
_PATH: Final = re.compile(r"/[A-Za-z0-9_.~!$&'()*+,;=:@%{}/-]*")
_TEMPLATE: Final = re.compile(r"\{([^{}/]+)\}")
_NAME_CHARS: Final = re.compile(r"[^a-z0-9]+")
_CAMEL: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_PARAM: Final = re.compile(r"[A-Za-z0-9_.\[\]-]{1,100}")


class ApiImportError(ValueError):
    """The document or the list is not valid. The message is safe to show."""


@dataclass(frozen=True, slots=True)
class ApiOperation:
    """One operation: the tool and its request."""

    tool: RemoteTool
    method: Method
    path: str
    params: tuple[tuple[str, Where], ...]
    body: bool

    def stored(self) -> dict[str, Any]:
        return {
            **self.tool.stored(),
            "request": {
                "method": self.method,
                "path": self.path,
                "params": [list(p) for p in self.params],
                "body": self.body,
            },
        }

    @classmethod
    def from_stored(cls, data: Mapping[str, Any]) -> ApiOperation | None:
        tool = RemoteTool.from_stored(data)
        request = data.get("request")
        if tool is None or not isinstance(request, Mapping):
            return None
        method = str(request.get("method", "")).upper()
        path = request.get("path")
        if method.lower() not in METHODS or not isinstance(path, str):
            return None
        params: list[tuple[str, Where]] = []
        for item in request.get("params") or []:
            if (
                isinstance(item, list | tuple)
                and len(item) == 2
                and isinstance(item[0], str)
                and item[1] in ("path", "query")
            ):
                params.append((item[0], cast(Where, item[1])))
        return cls(tool, cast(Method, method), path, tuple(params), bool(request.get("body")))


@dataclass(frozen=True, slots=True)
class Imported:
    operations: list[ApiOperation]
    base_url: str | None
    """The first server of the document, if it is an absolute URL."""
    skipped: int
    """Operations that the import did not take (a bad path, a name clash, or the limit)."""


def load_document(text: str) -> Mapping[str, Any]:
    """Parses an OpenAPI document in JSON or YAML."""
    if len(text.encode()) > MAX_DOCUMENT_BYTES:
        raise ApiImportError(f"the document is larger than {MAX_DOCUMENT_BYTES // 1_000_000} MB")
    try:
        data = json.loads(text)
    except ValueError:
        try:
            for event in yaml.parse(text, Loader=yaml.SafeLoader):
                if isinstance(event, yaml.AliasEvent):
                    raise ApiImportError("the document uses YAML aliases; sama does not read them")
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            raise ApiImportError("the document is not valid JSON or YAML") from None
    if not isinstance(data, Mapping):
        raise ApiImportError("the document is not an object")
    version = str(data.get("openapi", ""))
    if not version.startswith("3."):
        raise ApiImportError("sama reads OpenAPI 3 documents only (the openapi field is 3.x)")
    if not isinstance(data.get("paths"), Mapping):
        raise ApiImportError("the document has no paths")
    return data


def parse_openapi(text: str, *, max_name: int) -> Imported:
    """The operations of an OpenAPI 3 document. ``max_name`` is the longest tool name
    (without the ``api_<connection>_`` prefix)."""
    document = load_document(text)
    resolver = _Refs(document)
    operations: list[ApiOperation] = []
    names: set[str] = set()
    skipped = 0
    for path, item in cast(Mapping[str, Any], document["paths"]).items():
        item = resolver.resolve(item)
        if not isinstance(item, Mapping) or not isinstance(path, str):
            skipped += 1
            continue
        shared = _list(item.get("parameters"))
        for method in METHODS:
            operation = resolver.resolve(item.get(method))
            if not isinstance(operation, Mapping):
                continue
            if len(operations) >= MAX_TOOLS:
                skipped += 1
                continue
            built = _operation(resolver, path, method, operation, shared, names, max_name)
            if built is None:
                skipped += 1
                continue
            names.add(built.tool.name)
            operations.append(built)
    return Imported(operations, _server(document), skipped)


def parse_endpoints(items: Sequence[Mapping[str, Any]], *, max_name: int) -> Imported:
    """The operations of a list of endpoints: ``name``, ``method``, ``path``,
    ``description``, ``query`` (names), and ``body`` (true if it has a JSON body)."""
    operations: list[ApiOperation] = []
    names: set[str] = set()
    skipped = 0
    for raw in items[: MAX_TOOLS * 2]:
        method = str(raw.get("method", "GET")).lower()
        path = raw.get("path")
        if method not in METHODS or not isinstance(path, str) or len(operations) >= MAX_TOOLS:
            skipped += 1
            continue
        query = raw.get("query") or []
        # Each ``{name}`` of the path is a required path argument.
        parameters = [
            {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
            for name in dict.fromkeys(_TEMPLATE.findall(path))
        ] + [
            {"name": name, "in": "query", "schema": {"type": "string"}}
            for name in (query if isinstance(query, list) else [])
            if isinstance(name, str)
        ]
        operation: dict[str, Any] = {
            "operationId": raw.get("name"),
            "description": raw.get("description") or "",
            "parameters": parameters,
        }
        if raw.get("body"):
            operation["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": {"type": "object"}}},
            }
        built = _operation(_Refs({}), path, method, operation, [], names, max_name)
        if built is None:
            skipped += 1
            continue
        names.add(built.tool.name)
        operations.append(built)
    return Imported(operations, None, skipped)


def tool_name(operation_id: object, method: str, path: str, max_name: int) -> str:
    """The tool name: the ``operationId`` in snake case, else the method and the path."""
    if isinstance(operation_id, str) and operation_id.strip():
        raw = _CAMEL.sub("_", operation_id.strip())
    else:
        raw = f"{method}_{_TEMPLATE.sub(lambda m: 'by_' + m.group(1), path)}"
    name = _NAME_CHARS.sub("_", raw.lower()).strip("_") or method
    if not name[0].isalpha():
        name = f"op_{name}"
    return name[:max_name].rstrip("_")


def _operation(
    refs: _Refs,
    path: str,
    method: str,
    operation: Mapping[str, Any],
    shared: list[Any],
    names: set[str],
    max_name: int,
) -> ApiOperation | None:
    if _PATH.fullmatch(path) is None or any(p in (".", "..") for p in path.split("/")):
        return None
    name = tool_name(operation.get("operationId"), method, path, max_name)
    if name in names:
        for number in range(2, 100):
            suffix = f"_{number}"
            candidate = name[: max_name - len(suffix)].rstrip("_") + suffix
            if candidate not in names:
                name = candidate
                break
        else:
            return None
    properties: dict[str, Any] = {}
    required: list[str] = []
    # Merge first: an operation parameter replaces a path-item parameter with its name and
    # place, also its ``required``.
    merged: dict[tuple[str, Where], Mapping[str, Any]] = {}
    for raw in [*shared, *_list(operation.get("parameters"))]:
        param = refs.resolve(raw)
        if not isinstance(param, Mapping):
            continue
        where, pname = param.get("in"), param.get("name")
        if where not in ("path", "query") or not isinstance(pname, str):
            continue  # sama sets the headers and sends no cookies.
        if _PARAM.fullmatch(pname) is None or pname == BODY:
            return None
        merged[(pname, cast(Where, where))] = param
    for (pname, where), param in merged.items():
        prop = refs.inline(param.get("schema")) or {"type": "string"}
        if where == "query" and not _plain_query(param, prop):
            return None  # sama sends only a scalar or a list of scalars, as form and explode.
        if isinstance(param.get("description"), str):
            prop = {**prop, "description": _cut(param["description"])}
        properties[pname] = prop
        if where == "path" or param.get("required") is True:
            required.append(pname)
    params = dict.fromkeys(merged)
    templated = set(_TEMPLATE.findall(path))
    if templated != {n for n, w in params if w == "path"}:
        return None  # Each path template must be one path argument.
    if len({n for n, _ in params}) != len(params):
        return None  # A path and a query argument with one name.
    body = False
    request_body = refs.resolve(operation.get("requestBody"))
    if isinstance(request_body, Mapping) and method not in ("get", "head"):
        content = request_body.get("content")
        media = content.get("application/json") if isinstance(content, Mapping) else None
        if isinstance(media, Mapping):
            body = True
            properties[BODY] = refs.inline(media.get("schema")) or {}
            if request_body.get("required") is True:
                required.append(BODY)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = sorted(set(required))
    if len(json.dumps(schema, default=str)) > MAX_SCHEMA_BYTES:
        schema = _smaller(schema)
    summary = operation.get("summary") if isinstance(operation.get("summary"), str) else ""
    text = operation.get("description") if isinstance(operation.get("description"), str) else ""
    description = _cut(" ".join(p for p in (summary, text) if p).strip()) or (
        f"{method.upper()} {path}"
    )
    tool = parse_tool(
        {
            "name": name,
            "description": f"{description} ({method.upper()} {path})",
            "inputSchema": schema,
            "annotations": {
                "readOnlyHint": method in ("get", "head"),
                "destructiveHint": method == "delete",
            },
        }
    )
    if tool is None:
        return None
    return ApiOperation(
        tool=tool,
        method=cast(Method, method.upper()),
        path=path,
        params=tuple(params),
        body=body,
    )


def _plain_query(param: Mapping[str, Any], schema: Mapping[str, Any]) -> bool:
    """True if sama can send the query parameter: the ``form`` style with ``explode`` (the
    OpenAPI default), and a scalar or an array of scalars (``tag=a&tag=b``). sama does not send
    an object, a nested array, or another style."""
    if param.get("style", "form") != "form" or param.get("explode", True) is not True:
        return False
    if "content" in param:
        return False
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        return False
    if kind == "array":
        items = schema.get("items")
        inner = items.get("type") if isinstance(items, Mapping) else None
        return inner not in ("object", "array")
    return True


def _smaller(schema: dict[str, Any]) -> dict[str, Any]:
    """A schema that fits: the argument names and their top-level types only."""
    properties = {
        name: {"type": value["type"]}
        if isinstance(value, Mapping) and isinstance(value.get("type"), str)
        else {}
        for name, value in schema["properties"].items()
    }
    small = {**schema, "properties": properties}
    if len(json.dumps(small)) > MAX_SCHEMA_BYTES:
        return {"type": "object"}
    return small


def _server(document: Mapping[str, Any]) -> str | None:
    """The first ``https`` server, with the defaults of its variables."""
    for server in _list(document.get("servers")):
        if not isinstance(server, Mapping) or not isinstance(server.get("url"), str):
            continue
        url = str(server["url"])
        variables = server.get("variables")
        if isinstance(variables, Mapping):
            for key, value in variables.items():
                default = value.get("default") if isinstance(value, Mapping) else None
                if isinstance(default, str):
                    url = url.replace("{" + str(key) + "}", default)
        url = url.rstrip("/")
        if url.startswith("https://") and "{" not in url:
            return url
    return None


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _cut(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_DESCRIPTION else text[:MAX_DESCRIPTION].rstrip() + "..."


class _Refs:
    """Resolves the local ``$ref`` (``#/...``) of a document. A remote or a cyclic ``$ref``
    becomes an open schema."""

    def __init__(self, document: Mapping[str, Any]) -> None:
        self._document = document
        self._budget = 0

    def resolve(self, value: Any) -> Any:
        """The target of a ``$ref`` (one level after another), or the value."""
        seen: set[str] = set()
        while isinstance(value, Mapping) and isinstance(value.get("$ref"), str):
            ref = value["$ref"]
            if ref in seen or len(seen) >= MAX_REF_DEPTH or not ref.startswith("#/"):
                return None
            seen.add(ref)
            value = self._pointer(ref)
        return value

    def inline(self, value: Any) -> dict[str, Any]:
        """A schema with its local ``$ref`` put in place, to a depth of ``MAX_REF_DEPTH`` and
        at most ``MAX_SCHEMA_NODES`` nodes."""
        self._budget = MAX_SCHEMA_NODES
        return self._inline(value, 0, ())

    def _inline(self, value: Any, depth: int, trail: tuple[str, ...]) -> dict[str, Any]:
        self._budget -= 1
        if depth > MAX_REF_DEPTH or self._budget < 0:
            return {}
        if isinstance(value, Mapping) and isinstance(value.get("$ref"), str):
            ref = value["$ref"]
            if ref in trail or not ref.startswith("#/"):
                return {}
            return self._inline(self._pointer(ref), depth + 1, (*trail, ref))
        if not isinstance(value, Mapping):
            return {}
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, Mapping):
                out[str(key)] = self._inline(item, depth + 1, trail)
            elif isinstance(item, list):
                out[str(key)] = [
                    self._inline(x, depth + 1, trail) if isinstance(x, Mapping) else x for x in item
                ]
            else:
                out[str(key)] = item
        return out

    def _pointer(self, ref: str) -> Any:
        node: Any = self._document
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, Mapping) or part not in node:
                return None
            node = node[part]
        return node
