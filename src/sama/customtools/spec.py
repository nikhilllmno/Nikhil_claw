"""The definition of a custom tool and its checks (docs/architecture.md, Section 10.8).

A custom tool is an HTTP request or a script that a user, a workspace, or an org adds. The
definition has a description, a JSON schema of the arguments, a policy (``allow``, ``ask``, or
``deny``), and the request or the script. This module checks a definition before sama stores
it, and it fills the templates of an HTTP request with the arguments of a call.

Rules of the templates:

- A placeholder is ``{{name}}``. ``name`` must be a property of the input schema.
- The scheme and the host of the URL are fixed: a placeholder can be in the path and the query
  only. A path value is quoted into one path segment, so the agent cannot leave the path or the
  host. A query value is encoded. A query parameter whose value is one placeholder of an
  argument that the call does not give is left out.
- A header value is text. It cannot hold a line break.
- The body is a JSON value. A string that is one placeholder becomes the argument as a JSON
  value. A string with a placeholder in other text gets the argument as text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Annotated, Any, Final, Literal
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sama.prompt.scan import scan
from sama.vault.redact import redact

TOOLSET: Final = "custom_tools"
"""The toolset that grants the custom tools to a profile."""
PREFIX: Final = "custom_"
"""The agent name of a custom tool is ``custom_<name>``."""
NAME: Final = re.compile(r"[a-z][a-z0-9_]{1,39}")
MAX_DESCRIPTION_CHARS: Final = 1_000
MAX_SUMMARY_CHARS: Final = 500
MAX_SCHEMA_BYTES: Final = 20_000
MAX_PROPERTIES: Final = 50
MAX_URL_CHARS: Final = 2_000
MAX_HEADERS: Final = 20
MAX_HEADER_CHARS: Final = 2_000
MAX_BODY_BYTES: Final = 20_000
MAX_CODE_CHARS: Final = 50_000
MAX_ARGUMENT_BYTES: Final = 100_000
DEFAULT_SCRIPT_TIMEOUT_S: Final = 30
MAX_SCRIPT_TIMEOUT_S: Final = 120
DEFAULT_RESPONSE_CHARS: Final = 20_000
MAX_RESPONSE_CHARS: Final = 100_000
MAX_PATH_PARTS: Final = 10
MAX_FIELDS: Final = 20

Kind = Literal["http", "script"]
Policy = Literal["allow", "ask", "deny"]
Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]
Language = Literal["python", "node"]

PLACEHOLDER: Final = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,63})\s*\}\}")
_WHOLE: Final = re.compile(r"\s*\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,63})\s*\}\}\s*")
_PROPERTY: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_HEADER: Final = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}")
_PART: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ORIGIN: Final = re.compile(r"(https://[^/?#{}\s]+)(.*)", re.DOTALL)
RESERVED_HEADERS: Final = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "upgrade",
        "te",
        "trailer",
        "keep-alive",
        "proxy-authorization",
        "authorization",
        "cookie",
    }
)
"""Headers that sama sets, or that carry a credential. A credential goes in ``auth``, so the
vault holds it and the tool definition never does."""
JSON_TYPES: Final = frozenset({"string", "integer", "number", "boolean", "array", "object", "null"})


class DefinitionError(ValueError):
    """The definition is not valid. The message is safe to show and holds no secret."""


class ArgumentError(ValueError):
    """The arguments of a call do not fit the tool. The message is safe to show."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HttpAuth(_Model):
    """How sama sends the credential of the vault. ``api_key`` goes in the ``header`` or in the
    ``query`` parameter with that name."""

    type: Literal["none", "bearer", "api_key", "basic"] = "none"
    header: Annotated[str, Field(max_length=64)] | None = None
    query: Annotated[str, Field(max_length=64)] | None = None


class ResponseMap(_Model):
    """What part of a JSON answer the agent gets. ``path`` is a dot path (``data.items`` or
    ``items.0``). ``fields`` keeps only these keys of an object, or of each object of a list.
    ``max_chars`` cuts the text."""

    path: Annotated[str, Field(max_length=300)] | None = None
    fields: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=MAX_FIELDS
    )
    max_chars: int = Field(default=DEFAULT_RESPONSE_CHARS, ge=100, le=MAX_RESPONSE_CHARS)


class HttpConfig(_Model):
    method: Method = "GET"
    url: Annotated[str, Field(min_length=9, max_length=MAX_URL_CHARS)]
    headers: dict[str, Annotated[str, Field(max_length=MAX_HEADER_CHARS)]] = Field(
        default_factory=dict
    )
    body: Any = None
    """A JSON value with placeholders, or ``None`` for no body."""
    auth: HttpAuth = Field(default_factory=HttpAuth)
    response: ResponseMap = Field(default_factory=ResponseMap)


class ScriptConfig(_Model):
    language: Language = "python"
    code: Annotated[str, Field(min_length=1, max_length=MAX_CODE_CHARS)]
    timeout_s: int = Field(default=DEFAULT_SCRIPT_TIMEOUT_S, ge=1, le=MAX_SCRIPT_TIMEOUT_S)


class Definition(_Model):
    """One version of a custom tool, as the API takes it and as sama stores it."""

    kind: Kind
    description: Annotated[str, Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)]
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    policy: Policy = "ask"
    http: HttpConfig | None = None
    script: ScriptConfig | None = None

    @model_validator(mode="after")
    def _one_config(self) -> Definition:
        if self.kind == "http" and (self.http is None or self.script is not None):
            raise ValueError("an http tool needs http and no script")
        if self.kind == "script" and (self.script is None or self.http is not None):
            raise ValueError("a script tool needs script and no http")
        return self

    def config(self) -> dict[str, Any]:
        """The ``config`` column: the request or the script."""
        if self.http is not None:
            return {"http": self.http.model_dump(mode="json")}
        if self.script is None:  # pragma: no cover - the validator needs one config
            raise ValueError("the definition has no config")
        return {"script": self.script.model_dump(mode="json")}

    @classmethod
    def of(
        cls, kind: str, description: str, input_schema: Any, policy: str, config: Any
    ) -> Definition:
        """The definition of a stored version."""
        return cls.model_validate(
            {
                "kind": kind,
                "description": description,
                "input_schema": input_schema,
                "policy": policy,
                **(config if isinstance(config, Mapping) else {}),
            }
        )


def agent_name(name: str) -> str:
    return PREFIX + name


def name_problem(name: str) -> str | None:
    if NAME.fullmatch(name) is None:
        return (
            "the name must have 2 to 40 characters: lower-case letters, digits, and _, "
            "and start with a letter"
        )
    return None


def access_of(definition: Definition) -> Literal["read", "write", "destructive"]:
    """What the tool does, for the approval card: ``GET`` and ``HEAD`` read, ``DELETE``
    deletes, every other request and every script can change things."""
    if definition.http is None:
        return "write"
    if definition.http.method in ("GET", "HEAD"):
        return "read"
    return "destructive" if definition.http.method == "DELETE" else "write"


def origin_of(url: str) -> str:
    """``https://host[:port]`` of a URL template, in lower case. The template has no
    placeholder in this part (``check``)."""
    found = _ORIGIN.fullmatch(url.strip())
    if found is None:
        raise DefinitionError("the URL must start with https:// and a host")
    return found.group(1).lower()


def check(definition: Definition) -> None:
    """Raises ``DefinitionError`` when the definition is not safe or not complete."""
    problems: list[str] = []
    _check_text("the description", definition.description, problems)
    properties = _check_schema(definition.input_schema, problems)
    if definition.http is not None:
        _check_http(definition.http, properties, problems)
    if definition.script is not None:
        if not definition.script.code.strip():
            problems.append("the script is empty")
        if redact(definition.script.code) != definition.script.code:
            # The code goes to the sandbox and to every member who sees the tool.
            problems.append("the script has a secret. A script tool cannot hold a credential")
    if problems:
        raise DefinitionError("; ".join(problems[:6]))


def check_summary(summary: str) -> None:
    problems: list[str] = []
    if len(summary) > MAX_SUMMARY_CHARS:
        problems.append(f"the summary has more than {MAX_SUMMARY_CHARS} characters")
    _check_text("the summary", summary, problems)
    if problems:
        raise DefinitionError("; ".join(problems))


def _check_text(what: str, text: str, problems: list[str]) -> None:
    if redact(text) != text:
        problems.append(f"{what} has a secret. Put a credential in the auth of the tool")
    if found := scan(text):
        problems.append(f"{what} looks like a prompt injection ({', '.join(found)})")


def _check_schema(schema: Mapping[str, Any], problems: list[str]) -> frozenset[str]:
    text = json.dumps(schema, ensure_ascii=False)
    if len(text.encode()) > MAX_SCHEMA_BYTES:
        problems.append(f"the input schema is larger than {MAX_SCHEMA_BYTES:,} bytes")
        return frozenset()
    if schema.get("type") != "object":
        problems.append('the input schema must have "type": "object"')
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        problems.append("the properties of the input schema must be an object")
        return frozenset()
    if len(properties) > MAX_PROPERTIES:
        problems.append(f"the input schema has more than {MAX_PROPERTIES} properties")
    for name, spec in properties.items():
        if _PROPERTY.fullmatch(str(name)) is None:
            problems.append(f"{name!r} is not a valid argument name")
        if not isinstance(spec, Mapping):
            problems.append(f"the schema of {name} must be an object")
            continue
        kind = spec.get("type")
        kinds = kind if isinstance(kind, list) else [kind] if kind is not None else []
        if any(not isinstance(k, str) or k not in JSON_TYPES for k in kinds):
            problems.append(f"the type of {name} is not a JSON schema type")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(
        not isinstance(name, str) or name not in properties for name in required
    ):
        problems.append("required must be a list of the property names")
    # The descriptions of the arguments go to the model too.
    _check_text("the input schema", text, problems)
    return frozenset(str(name) for name in properties)


def _check_http(http: HttpConfig, properties: frozenset[str], problems: list[str]) -> None:
    found = _ORIGIN.fullmatch(http.url.strip())
    if found is None:
        problems.append("the URL must start with https:// and a host, with no placeholder")
        return
    origin, rest = found.groups()
    parts = urlsplit(origin)
    if parts.username or parts.password or "@" in origin:
        problems.append("the URL cannot have a user name or a password")
    if "#" in rest:
        problems.append("the URL cannot have a fragment")
    if rest and not rest.startswith(("/", "?")):
        problems.append("the path of the URL must start with /")
    used = set(PLACEHOLDER.findall(rest))
    if len(http.headers) > MAX_HEADERS:
        problems.append(f"a tool has at most {MAX_HEADERS} headers")
    auth_header = (http.auth.header or "").lower() if http.auth.type == "api_key" else ""
    for name, value in http.headers.items():
        if _HEADER.fullmatch(name) is None:
            problems.append(f"{name!r} is not a valid header name")
        elif name.lower() in RESERVED_HEADERS or name.lower() == auth_header:
            problems.append(f"sama sets the header {name}. Put a credential in the auth")
        if "\r" in value or "\n" in value:
            problems.append(f"the header {name} has a line break")
        used |= set(PLACEHOLDER.findall(value))
    _check_text("the headers", json.dumps(http.headers), problems)
    if http.body is not None:
        if http.method in ("GET", "HEAD"):
            problems.append(f"a {http.method} request has no body")
        body = json.dumps(http.body, ensure_ascii=False)
        if len(body.encode()) > MAX_BODY_BYTES:
            problems.append(f"the body is larger than {MAX_BODY_BYTES:,} bytes")
        _check_text("the body", body, problems)
        used |= set(PLACEHOLDER.findall(body))
    if unknown := sorted(used - properties):
        problems.append(f"the placeholders {', '.join(unknown)} are not in the input schema")
    _check_text("the URL", http.url, problems)
    auth = http.auth
    if auth.type == "api_key":
        if (auth.header is None) == (auth.query is None):
            problems.append("an api_key auth needs a header or a query parameter, not both")
        elif auth.header is not None and (
            _HEADER.fullmatch(auth.header) is None or auth.header.lower() in RESERVED_HEADERS
        ):
            problems.append(f"{auth.header!r} cannot hold the API key")
        elif auth.query is not None and _PROPERTY.fullmatch(auth.query) is None:
            problems.append(f"{auth.query!r} is not a valid query parameter name")
    elif auth.header is not None or auth.query is not None:
        problems.append("only an api_key auth has a header or a query parameter")
    response = http.response
    if response.path is not None and not all(
        _PART.fullmatch(part) for part in response.path.split(".")
    ):
        problems.append("the response path must be names and numbers, split by dots")
    elif response.path is not None and len(response.path.split(".")) > MAX_PATH_PARTS:
        problems.append(f"the response path has more than {MAX_PATH_PARTS} parts")


# -- calls --------------------------------------------------------------------------------


def check_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> None:
    """Checks the arguments of a call against the top level of the input schema: the required
    names, no unknown names, the type, and the ``enum`` of each argument."""
    if len(json.dumps(arguments, default=str).encode()) > MAX_ARGUMENT_BYTES:
        raise ArgumentError(f"the arguments are larger than {MAX_ARGUMENT_BYTES:,} bytes")
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    if missing := sorted(str(name) for name in required if name not in arguments):
        raise ArgumentError(f"missing arguments: {', '.join(missing)}")
    if unknown := sorted(name for name in arguments if name not in properties):
        raise ArgumentError(f"unknown arguments: {', '.join(unknown)}")
    for name, value in arguments.items():
        spec = properties[name]
        if not isinstance(spec, Mapping):
            continue
        kind = spec.get("type")
        kinds = kind if isinstance(kind, list) else [kind] if kind is not None else []
        if kinds and not any(_is_type(value, k) for k in kinds):
            raise ArgumentError(f"{name} must be {' or '.join(str(k) for k in kinds)}")
        enum = spec.get("enum")
        if isinstance(enum, list) and value not in enum:
            raise ArgumentError(f"{name} must be one of {json.dumps(enum)[:200]}")


def _is_type(value: Any, kind: Any) -> bool:
    match kind:
        case "string":
            return isinstance(value, str)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "number":
            return isinstance(value, int | float) and not isinstance(value, bool)
        case "boolean":
            return isinstance(value, bool)
        case "array":
            return isinstance(value, list)
        case "object":
            return isinstance(value, dict)
        case "null":
            return value is None
    return True


def as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def render_url(template: str, arguments: Mapping[str, Any]) -> tuple[str, list[tuple[str, str]]]:
    """The URL without the query, and the query pairs, for the arguments of a call."""
    found = _ORIGIN.fullmatch(template.strip())
    if found is None:
        raise DefinitionError("the URL must start with https:// and a host")
    origin, rest = found.groups()
    path, _, query = rest.partition("?")

    def segment(match: re.Match[str]) -> str:
        name = match.group(1)
        value = arguments.get(name)
        if value is None or as_text(value) == "":
            raise ArgumentError(f"missing argument: {name}")
        text = as_text(value)
        if text in (".", ".."):
            raise ArgumentError(f"not a valid value for {name}")
        return quote(text, safe="")

    pairs: list[tuple[str, str]] = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        whole = _WHOLE.fullmatch(value)
        if whole is not None:
            given = arguments.get(whole.group(1))
            if given is None:
                continue
            items = given if isinstance(given, list) else [given]
            pairs.extend((key, as_text(item)) for item in items)
            continue
        pairs.append(
            (key, PLACEHOLDER.sub(lambda m: as_text(arguments.get(m.group(1), "")), value))
        )
    return origin + PLACEHOLDER.sub(segment, path), pairs


def full_url(base: str, pairs: list[tuple[str, str]]) -> str:
    return base + ("?" + urlencode(pairs) if pairs else "")


def render_header(template: str, arguments: Mapping[str, Any]) -> str:
    text = PLACEHOLDER.sub(lambda m: as_text(arguments.get(m.group(1), "")), template)
    if "\r" in text or "\n" in text:
        raise ArgumentError("a header value cannot have a line break")
    return text


def render_body(template: Any, arguments: Mapping[str, Any]) -> Any:
    """The JSON body for the arguments of a call."""
    if isinstance(template, str):
        whole = _WHOLE.fullmatch(template)
        if whole is not None:
            return arguments.get(whole.group(1))
        return PLACEHOLDER.sub(lambda m: as_text(arguments.get(m.group(1), "")), template)
    if isinstance(template, list):
        return [render_body(item, arguments) for item in template]
    if isinstance(template, dict):
        return {key: render_body(value, arguments) for key, value in template.items()}
    return template


def map_response(text: str, response: ResponseMap) -> str:
    """The part of the answer that the agent gets, cut to ``max_chars``."""
    if response.path is not None or response.fields:
        try:
            value: Any = json.loads(text)
        except ValueError:
            raise ArgumentError(
                "the answer is not JSON, so the response mapping cannot apply"
            ) from None
        for part in (response.path or "").split(".") if response.path else []:
            if isinstance(value, list) and part.isdigit() and int(part) < len(value):
                value = value[int(part)]
            elif isinstance(value, dict) and part in value:
                value = value[part]
            else:
                raise ArgumentError(f"the answer has no {response.path}")
        if response.fields:
            value = _pick(value, response.fields)
        text = json.dumps(value, ensure_ascii=False)
    if len(text) > response.max_chars:
        text = text[: response.max_chars] + "\n[cut]"
    return text


def _pick(value: Any, fields: list[str]) -> Any:
    if isinstance(value, dict):
        return {key: value[key] for key in fields if key in value}
    if isinstance(value, list):
        return [_pick(item, fields) for item in value]
    return value
