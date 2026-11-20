"""A client of remote MCP servers over streamable HTTP (docs/architecture.md, Section 10.3).

The control plane is the MCP client: the sandbox never talks to a remote server, and the
credential never leaves the control plane. Every request goes through ``OutboundClient``, so
the URL policy of Section 16.4 holds (https, public addresses only, no redirects, size and time
limits).

The client speaks JSON-RPC 2.0 by ``POST``. A server answers with one JSON object or with an
event stream (``text/event-stream``). The client reads the stream only until the answer to its
request. Requests from the server (for example ``sampling``) are not answered: sama does not
give a remote server a model or the file system.

A session starts with ``initialize`` and ``notifications/initialized``. A server can give a
session id (``Mcp-Session-Id``). The client keeps the session in memory for each connection.
A ``404`` for a session means that the server forgot it: the client starts a new session once.

This module does not copy Hermes code. It follows the MCP specification, version 2025-06-18.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sama.connectors.errors import short_error
from sama.gate.decide import ToolAccess
from sama.tools.outbound import (
    BlockedUrlError,
    FetchError,
    FetchResult,
    OutboundClient,
    UrlPolicy,
)

PROTOCOL_VERSION: Final = "2025-06-18"
SUPPORTED_VERSIONS: Final = frozenset({"2025-06-18", "2025-03-26", "2024-11-05"})
CLIENT_INFO: Final = {"name": "sama", "version": "0.1"}
TOOL_PREFIX: Final = "mcp_"
MAX_PAGES: Final = 10
MAX_TOOLS: Final = 200
MAX_DESCRIPTION_CHARS: Final = 1_800
MAX_SCHEMA_BYTES: Final = 20_000
MAX_TOOL_NAME: Final = 64
UNAVAILABLE_CODES: Final = frozenset({408, 425, 429})

POLICY: Final = UrlPolicy(
    max_redirects=0,
    max_bytes=5_000_000,
    hop_timeout_s=60.0,
    total_timeout_s=90.0,
)
"""A tool call can take some time. A redirect is never followed: it could send the
credential to another host."""

_NAME_CHARS: Final = re.compile(r"[^A-Za-z0-9_-]")
_SAMA_NAME: Final = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")


class McpError(Exception):
    """A request to a remote MCP server failed. The message is short and safe to show."""


class McpUnavailableError(McpError):
    """The server did not answer (network, time, 408, 429, or 5xx). Try again later."""


class McpAuthError(McpError):
    """The server refused the credential (401 or 403)."""

    def __init__(self, message: str, *, challenge: str | None = None) -> None:
        super().__init__(message)
        self.challenge = challenge
        """The ``WWW-Authenticate`` header of the answer."""


class McpSessionGoneError(McpError):
    """The server does not know the session now (404 with a session id)."""


@dataclass(frozen=True, slots=True)
class McpSession:
    """A session with a server. ``session_id`` is ``None`` for a server without sessions."""

    version: str
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteTool:
    """One tool of a remote server, as ``tools/list`` gave it, with limits applied."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    title: str | None = None
    read_only: bool | None = None
    destructive: bool | None = None

    def stored(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "read_only": self.read_only,
            "destructive": self.destructive,
        }

    @classmethod
    def from_stored(cls, data: Mapping[str, Any]) -> RemoteTool | None:
        name = data.get("name")
        schema = data.get("input_schema")
        if not isinstance(name, str) or not isinstance(schema, Mapping):
            return None
        return cls(
            name=name,
            description=str(data.get("description") or ""),
            input_schema=schema,
            title=_optional_str(data.get("title")),
            read_only=_optional_bool(data.get("read_only")),
            destructive=_optional_bool(data.get("destructive")),
        )


@dataclass(frozen=True, slots=True)
class ExposedTool:
    """A remote tool under its sama name, with its access."""

    name: str
    """``mcp_<connector>_<tool>``."""
    remote: RemoteTool
    access: ToolAccess


def parse_tool(data: object) -> RemoteTool | None:
    """One entry of ``tools/list``, or ``None`` if it is not valid. A long description is
    cut. A schema that is not an object, or that is too large, becomes an open object."""
    if not isinstance(data, Mapping):
        return None
    name = data.get("name")
    if not isinstance(name, str) or not name or len(name) > 128:
        return None
    description = data.get("description")
    text = description if isinstance(description, str) else ""
    if len(text) > MAX_DESCRIPTION_CHARS:
        text = text[:MAX_DESCRIPTION_CHARS].rstrip() + "..."
    schema = data.get("inputSchema")
    if (
        not isinstance(schema, Mapping)
        or schema.get("type") != "object"
        or len(json.dumps(schema, default=str)) > MAX_SCHEMA_BYTES
    ):
        schema = {"type": "object"}
    annotations = data.get("annotations")
    hints: Mapping[str, Any] = annotations if isinstance(annotations, Mapping) else {}
    title = data.get("title") or hints.get("title")
    return RemoteTool(
        name=name,
        description=text,
        input_schema=json.loads(json.dumps(schema, default=str)),
        title=title[:120] if isinstance(title, str) else None,
        read_only=_optional_bool(hints.get("readOnlyHint")),
        destructive=_optional_bool(hints.get("destructiveHint")),
    )


def tools_hash(tools: Sequence[RemoteTool]) -> str:
    """A hash of the tools, so a change of the tool list is easy to see."""
    data = json.dumps([t.stored() for t in tools], sort_keys=True, default=str)
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def sama_name(connector: str, remote: str) -> str | None:
    """``mcp_<connector>_<tool>``: a ``-`` of the connector becomes ``_``, and a character
    that a tool name cannot have becomes ``_``. ``None`` if the name is longer than 64."""
    name = f"{TOOL_PREFIX}{connector.replace('-', '_')}_{_NAME_CHARS.sub('_', remote)}"
    return name if _SAMA_NAME.fullmatch(name) else None


def matches(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def tool_access(tool: RemoteTool, overrides: Mapping[str, ToolAccess]) -> ToolAccess:
    """The access of a remote tool. An override of the manifest wins. Else ``readOnlyHint``
    gives ``read``, and ``destructiveHint: false`` gives ``write``. Else the tool can delete
    things, as the MCP specification says for a tool without hints."""
    for pattern, access in overrides.items():
        if fnmatch.fnmatchcase(tool.name, pattern):
            return access
    if tool.read_only is True:
        return "read"
    if tool.destructive is False:
        return "write"
    return "destructive"


def exposed_tools(
    connector: str,
    tools: Sequence[RemoteTool],
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    access: Mapping[str, ToolAccess] | None = None,
) -> list[ExposedTool]:
    """The tools that a run can use, under their sama names. A tool that the filters drop, or
    whose name is too long or the same as a name before it, is not in the list."""
    out: list[ExposedTool] = []
    seen: set[str] = set()
    for tool in tools:
        if include and not matches(tool.name, include):
            continue
        if exclude and matches(tool.name, exclude):
            continue
        name = sama_name(connector, tool.name)
        if name is None or name in seen:
            continue
        seen.add(name)
        out.append(ExposedTool(name, tool, tool_access(tool, access or {})))
    return out


def render_result(result: object) -> str:
    """The text of a ``tools/call`` result. A result with ``isError`` is text too: the model
    reads the error and can try again, as the MCP specification says."""
    if not isinstance(result, Mapping):
        raise McpError("the server sent a result that is not valid")
    parts: list[str] = []
    content = result.get("content")
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, Mapping):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} ({item.get('mimeType', 'unknown type')}) not shown]")
        elif kind == "resource_link":
            parts.append(f"[resource {item.get('name', '')}: {item.get('uri', '')}]")
        elif kind == "resource":
            resource = item.get("resource")
            if isinstance(resource, Mapping) and isinstance(resource.get("text"), str):
                parts.append(str(resource["text"]))
            elif isinstance(resource, Mapping):
                parts.append(f"[resource {resource.get('uri', '')} not shown]")
    structured = result.get("structuredContent")
    if not parts and structured is not None:
        parts.append(json.dumps(structured, ensure_ascii=False, default=str, indent=1))
    text = "\n".join(parts).strip()
    if result.get("isError") is True:
        return f"The tool reported an error:\n{text or '(no details)'}"
    return text or "(the tool gave no content)"


def answer_of(body: bytes, content_type: str, request_id: int) -> dict[str, Any] | None:
    """The JSON-RPC answer with the id in a JSON body or an event stream, or ``None``."""
    if "text/event-stream" in content_type.lower():
        return _from_stream(body, request_id)
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return _match(data, request_id)


def _from_stream(body: bytes, request_id: int) -> dict[str, Any] | None:
    text = body.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    # An event ends at an empty line. The last event can be cut: it has no empty line yet.
    for event in text.split("\n\n"):
        data = [
            line[5:].removeprefix(" ") for line in event.split("\n") if line.startswith("data:")
        ]
        if not data:
            continue
        try:
            found = _match(json.loads("\n".join(data)), request_id)
        except ValueError:
            continue
        if found is not None:
            return found
    return None


def _match(data: object, request_id: int) -> dict[str, Any] | None:
    items = data if isinstance(data, list) else [data]
    for item in items:
        if (
            isinstance(item, dict)
            and item.get("id") == request_id
            and ("result" in item or "error" in item)
        ):
            return item
    return None


class McpClient:
    """The JSON-RPC requests to one remote server. It keeps no state: the caller keeps the
    session."""

    def __init__(self, http: OutboundClient | None = None) -> None:
        self._http = http or OutboundClient(POLICY)
        self._next = 0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def check(self, url: str) -> None:
        """Raises ``BlockedUrlError`` or ``FetchError`` if the outbound URL policy refuses the
        URL (Section 16.4)."""
        await self._http.check(url)

    async def open(self, url: str, headers: Mapping[str, str]) -> McpSession:
        """``initialize`` and ``notifications/initialized``. Returns the session."""
        params = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }
        result, session_id = await self._request(url, headers, None, "initialize", params)
        version = result.get("protocolVersion") if isinstance(result, Mapping) else None
        if not isinstance(version, str) or version not in SUPPORTED_VERSIONS:
            raise McpError(f"the server speaks an MCP version that sama does not know: {version}")
        session = McpSession(version=version, session_id=session_id)
        await self._notify(url, headers, session, "notifications/initialized")
        return session

    async def list_tools(
        self, url: str, headers: Mapping[str, str], session: McpSession
    ) -> list[RemoteTool]:
        """All pages of ``tools/list``, at most ``MAX_PAGES`` pages and ``MAX_TOOLS`` tools."""
        tools: list[RemoteTool] = []
        cursor: str | None = None
        for _page in range(MAX_PAGES):
            params = {"cursor": cursor} if cursor else {}
            result, _ = await self._request(url, headers, session, "tools/list", params)
            if not isinstance(result, Mapping) or not isinstance(result.get("tools"), list):
                raise McpError("the server sent a tool list that is not valid")
            for entry in result["tools"]:
                tool = parse_tool(entry)
                if tool is not None and len(tools) < MAX_TOOLS:
                    tools.append(tool)
            following = result.get("nextCursor")
            if not isinstance(following, str) or not following or len(tools) >= MAX_TOOLS:
                break
            cursor = following
        return tools

    async def call_tool(
        self,
        url: str,
        headers: Mapping[str, str],
        session: McpSession,
        name: str,
        arguments: Mapping[str, Any],
    ) -> str:
        params = {"name": name, "arguments": dict(arguments)}
        result, _ = await self._request(url, headers, session, "tools/call", params)
        return render_result(result)

    async def _notify(
        self, url: str, headers: Mapping[str, str], session: McpSession, method: str
    ) -> None:
        body = {"jsonrpc": "2.0", "method": method}
        answer = await self._post(url, headers, session, body, None)
        if answer.status_code >= 400:
            self._raise_status(answer, session)

    async def _request(
        self,
        url: str,
        headers: Mapping[str, str],
        session: McpSession | None,
        method: str,
        params: Mapping[str, Any],
    ) -> tuple[Any, str | None]:
        self._next += 1
        request_id = self._next
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
        answer = await self._post(url, headers, session, body, request_id)
        if answer.status_code >= 400:
            self._raise_status(answer, session)
        found = answer_of(answer.content, answer.content_type, request_id)
        if found is None:
            if answer.truncated:
                raise McpError(f"the answer to {method} is too large")
            raise McpError(f"the server sent no answer to {method}")
        error = found.get("error")
        if error is not None:
            message = error.get("message") if isinstance(error, Mapping) else None
            raise McpError(short_error(f"{method} failed: {message or error}"))
        return found.get("result"), answer.headers.get("mcp-session-id")

    async def _post(
        self,
        url: str,
        headers: Mapping[str, str],
        session: McpSession | None,
        body: Mapping[str, Any],
        request_id: int | None,
    ) -> FetchResult:
        sent = {
            **headers,
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        }
        if session is not None:
            sent["mcp-protocol-version"] = session.version
            if session.session_id:
                sent["mcp-session-id"] = session.session_id
        enough: Callable[[bytes], bool] | None = None
        if request_id is not None:

            def enough(data: bytes) -> bool:
                # Only an event stream can stay open after the answer.
                return b"\n\n" in data and _from_stream(data, request_id) is not None

        try:
            return await self._http.request("POST", url, headers=sent, json=body, enough=enough)
        except BlockedUrlError as error:
            raise McpError(f"the URL policy blocked the server: {error}") from None
        except FetchError as error:
            raise McpUnavailableError(str(error)) from None

    @staticmethod
    def _raise_status(answer: FetchResult, session: McpSession | None) -> None:
        status = answer.status_code
        text = short_error(answer.content.decode("utf-8", errors="replace"))
        if status in (401, 403):
            raise McpAuthError(
                f"the server refused the credential ({status})",
                challenge=answer.headers.get("www-authenticate"),
            )
        if status == 404 and session is not None and session.session_id:
            raise McpSessionGoneError("the server does not know the session now")
        if status in UNAVAILABLE_CODES or status >= 500:
            raise McpUnavailableError(f"the server answered {status}")
        raise McpError(f"the server answered {status}: {text}" if text else f"answered {status}")


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


__all__ = [
    "MAX_TOOLS",
    "POLICY",
    "PROTOCOL_VERSION",
    "ExposedTool",
    "McpAuthError",
    "McpClient",
    "McpError",
    "McpSession",
    "McpSessionGoneError",
    "McpUnavailableError",
    "RemoteTool",
    "answer_of",
    "exposed_tools",
    "parse_tool",
    "render_result",
    "sama_name",
    "tool_access",
    "tools_hash",
]
