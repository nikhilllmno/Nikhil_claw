"""Deferred tools and the tool search (docs/architecture.md, Sections 6.2 and 10.3).

A deferred tool is a sama tool that is not in the tool list of the model. Every tool schema is
in every model call, so rare tools (most connector tools, image generation, text to speech) wait
in the ``DeferredCatalog`` instead. The model finds them with three sama tools:

- ``tool_search`` lists the deferred tools that match some words,
- ``tool_describe`` gives the description and the input schema of one tool,
- ``tool_call`` runs one tool.

The catalog gives no authority. The tool gate judges a ``tool_call`` as a call of the tool that
it names (``sama.gate.decide.effective_call``), so a deferred call has the same toolset check,
policy, approval, and ticket as a direct call. ``tool_search`` and ``tool_describe`` show only
the tools that the run can use, so a run does not learn the names of other tools.

Products and connectors register tools with ``DeferredCatalog.register`` (decision D16). The
catalog is also the ``ToolsetSource`` of the gate: each tool belongs to a toolset, and a profile
grants the toolset.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from sama.gate.decide import META_TOOLS, TOOLSETS, ToolAccess, canonical_name, tools_of
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.output import untrusted

DEFAULT_SEARCH_LIMIT: Final = 5
MAX_SEARCH_LIMIT: Final = 10
MAX_QUERY_CHARS: Final = 200
MAX_DESCRIPTION_CHARS: Final = 2_000
SHORT_CHARS: Final = 160
"""``tool_search`` shows the first part of each description."""

_NAME: Final = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
_WORD: Final = re.compile(r"[a-z0-9]+")
_RESERVED: Final = frozenset(tools_of(TOOLSETS)) | META_TOOLS

DeferredHandler = Callable[[RunTokenClaims, Mapping[str, Any]], Awaitable[str]]
"""Runs the tool for the run of the claims. The tool server has already checked the gate."""


class DeferredToolError(Exception):
    """A deferred tool cannot be found or called. The message is safe to show to the agent."""


@dataclass(frozen=True, slots=True)
class DeferredTool:
    """One deferred tool. ``toolset`` is the toolset that grants it. ``input_schema`` is a JSON
    schema of the arguments (an object). ``handler`` does the work and returns text."""

    name: str
    description: str
    toolset: str
    handler: DeferredHandler
    input_schema: Mapping[str, Any] = field(default_factory=lambda: {"type": "object"})
    tags: tuple[str, ...] = ()
    access: ToolAccess = "write"
    """What the tool does, for its default policy (Section 10.3). A ``destructive`` tool can
    delete or change data that cannot be restored; the approval card says so."""
    source: str | None = None
    """Set when an outside party wrote the description and the schema, for example a remote
    MCP server. ``tool_search`` and ``tool_describe`` then put that text in an untrusted
    content block."""

    def __post_init__(self) -> None:
        if _NAME.fullmatch(self.name) is None:
            raise ValueError(f"not a valid tool name: {self.name!r}")
        if canonical_name(self.name) != self.name:
            raise ValueError(f"{self.name} has the prefix of a sama MCP tool name")
        if self.name in _RESERVED:
            raise ValueError(f"{self.name} is the name of a sama tool")
        if not self.toolset.strip():
            raise ValueError("a deferred tool needs a toolset")
        if len(self.description) > MAX_DESCRIPTION_CHARS:
            raise ValueError(f"the description has more than {MAX_DESCRIPTION_CHARS} characters")
        if self.input_schema.get("type") != "object":
            raise ValueError("the input schema must describe an object")


def missing_arguments(tool: DeferredTool, arguments: Mapping[str, Any]) -> list[str]:
    """The names of the required arguments that the call lacks. The handler checks the rest of
    the arguments: the catalog does not validate types."""
    required = tool.input_schema.get("required")
    if not isinstance(required, list):
        return []
    return sorted(str(name) for name in required if name not in arguments)


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _score(tool: DeferredTool, query: set[str]) -> int:
    """A match in the name counts three, in a tag two, in the description one."""
    name = _words(tool.name.replace("_", " ").replace("-", " "))
    tags = _words(" ".join(tool.tags))
    description = _words(tool.description)
    return sum(3 * (w in name) + 2 * (w in tags) + (w in description) for w in query)


class DeferredCatalog:
    """The deferred tools of the tool server. It is also the ``ToolsetSource`` of the gate."""

    def __init__(self, tools: Collection[DeferredTool] = ()) -> None:
        self._tools: dict[str, DeferredTool] = {}
        for tool in tools:
            self.register(tool)

    def __len__(self) -> int:
        return len(self._tools)

    def register(self, tool: DeferredTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"the deferred tool {tool.name} is already registered")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> DeferredTool | None:
        return self._tools.get(name)

    def extended(self, extra: Collection[DeferredTool]) -> DeferredCatalog:
        """A copy with more tools, for example the tools of the remote MCP servers of one run.
        A tool with the name of a tool in this catalog is not added."""
        copy = DeferredCatalog()
        copy._tools = dict(self._tools)
        for tool in extra:
            copy._tools.setdefault(tool.name, tool)
        return copy

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def toolsets(self) -> Mapping[str, frozenset[str]]:
        """The tools of each toolset, for the gate."""
        grouped: dict[str, set[str]] = {}
        for tool in self._tools.values():
            grouped.setdefault(tool.toolset, set()).add(tool.name)
        return {name: frozenset(tools) for name, tools in grouped.items()}

    def access(self) -> Mapping[str, ToolAccess]:
        """What each tool does, for the default policy of the gate: a tool that only reads
        gets ``allow``, every other tool gets ``ask`` (Section 10.3)."""
        return {name: tool.access for name, tool in self._tools.items()}

    def search(
        self, query: str | None, allowed: Collection[str], limit: int = DEFAULT_SEARCH_LIMIT
    ) -> list[DeferredTool]:
        """The tools in ``allowed`` that match the words of the query, best first. Without a
        query, the first tools by name. A tool that matches no word is not in the result."""
        limit = max(1, min(limit, MAX_SEARCH_LIMIT))
        candidates = [tool for name, tool in sorted(self._tools.items()) if name in allowed]
        words = _words((query or "")[:MAX_QUERY_CHARS])
        if not words:
            return candidates[:limit]
        scored = [(_score(tool, words), tool) for tool in candidates]
        ranked = sorted((item for item in scored if item[0] > 0), key=lambda item: -item[0])
        return [tool for _score_, tool in ranked[:limit]]


def format_search(tools: list[DeferredTool], *, query: str | None) -> str:
    """The result of ``tool_search``. The lines of tools that an outside party describes are
    in untrusted content blocks, one block for each source."""
    if not tools:
        return "No tool matches." if query else "There are no tools to search."
    lines: list[str] = []
    outside: dict[str, list[str]] = {}
    for tool in tools:
        short = " ".join(tool.description.split())
        if len(short) > SHORT_CHARS:
            short = short[:SHORT_CHARS].rstrip() + "..."
        line = f"- {tool.name}: {short}"
        if tool.source is None:
            lines.append(line)
        else:
            outside.setdefault(tool.source, []).append(line)
    blocks = [untrusted("\n".join(found), source=source) for source, found in outside.items()]
    return (
        f"{len(tools)} tools. Use tool_describe to see the arguments of one tool, then "
        "tool_call to run it.\n" + "\n".join([*lines, *blocks])
    )


def format_describe(tool: DeferredTool) -> str:
    """The result of ``tool_describe``."""
    schema = json.dumps(dict(tool.input_schema), ensure_ascii=False, indent=1)
    kind = {
        "read": "reads only",
        "write": "can change things",
        "destructive": "can delete things",
    }[tool.access]
    body = f"{tool.description}\n\nArguments (JSON schema):\n{schema}"
    if tool.source is not None:
        body = untrusted(body, source=tool.source)
    return (
        f"{tool.name} ({kind})\n{body}\n\n"
        f'Run it with tool_call: name="{tool.name}" and arguments as an object.'
    )
