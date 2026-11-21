"""The event streams of the coding agent CLIs (docs/architecture.md, Section 15.4).

Claude Code (``-p --output-format stream-json --verbose``) and Codex (``exec --json``) write
one JSON object per line. ``ClaudeStream`` and ``CodexStream`` turn the lines into the small set
of ``CliEvent`` types that the runtime and the tool use. A line that is not JSON, or a type
that sama does not know, gives no event: a newer CLI can add types.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from sama.coding.agents import CodingAgent
from sama.modelproxy.prices import PriceTable, Provider, TokenCounts
from sama.runtime.contract import Usage
from sama.vault.redact import redact

PREVIEW_CHARS: Final = 300
"""A tool input or output in an event is cut to this many characters."""


@dataclass(frozen=True, slots=True)
class CliSession:
    """The id of the CLI session (Claude Code) or thread (Codex). A later run resumes it."""

    session_id: str


@dataclass(frozen=True, slots=True)
class CliText:
    """Text of the agent for the user."""

    text: str


@dataclass(frozen=True, slots=True)
class CliToolStart:
    call_id: str
    name: str
    summary: str
    """One short line: the command, the path, or the start of the input."""


@dataclass(frozen=True, slots=True)
class CliToolEnd:
    call_id: str
    is_error: bool
    preview: str


@dataclass(frozen=True, slots=True)
class CliUsage:
    """The usage of this CLI invocation so far. It replaces the earlier value."""

    usage: Usage


@dataclass(frozen=True, slots=True)
class CliPlan:
    """The plan of Claude Code in its plan mode: the input of its ``ExitPlanMode`` tool
    (coding mode, Section 15.5). Redacted."""

    text: str


@dataclass(frozen=True, slots=True)
class CliResult:
    """The end of the turn. ``text`` is the last answer of the agent."""

    text: str
    is_error: bool
    message: str = ""


CliEvent = CliSession | CliText | CliToolStart | CliToolEnd | CliUsage | CliPlan | CliResult


def _short(value: Any, limit: int = PREVIEW_CHARS) -> str:
    """The value as one line of at most ``limit`` characters. It redacts before the cut: a cut
    secret no longer matches the redactor."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(redact(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _int(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _load(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


PLAN_TOOL: Final = "ExitPlanMode"
"""The tool of Claude Code that ends its plan mode. Its input ``plan`` is the plan."""
MAX_PLAN_CHARS: Final = 20_000


def _plan_text(name: str, data: Any) -> str:
    if name != PLAN_TOOL or not isinstance(data, dict):
        return ""
    plan = data.get("plan")
    return redact(plan.strip())[:MAX_PLAN_CHARS] if isinstance(plan, str) else ""


def _tool_summary(name: str, arguments: Any) -> str:
    """The most useful field of a tool input, for a progress line."""
    if isinstance(arguments, dict):
        for key in ("command", "file_path", "path", "pattern", "url", "description", "query"):
            value = arguments.get(key)
            if isinstance(value, str) and value:
                return _short(value, 160)
    return _short(arguments, 160) if arguments else ""


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "\n".join(parts)
    return ""


class ClaudeStream:
    """Parses ``claude -p --output-format stream-json --verbose``. An API error is a result with
    ``is_error`` (its ``subtype`` can still be ``success``)."""

    agent: Final = CodingAgent.CLAUDE_CODE

    def __init__(self, model: str = "", prices: PriceTable | None = None) -> None:
        self._open: set[str] = set()
        self._model = model
        self._prices = prices or PriceTable.load()

    def feed(self, line: str) -> list[CliEvent]:
        data = _load(line)
        if data is None:
            return []
        kind = data.get("type")
        events: list[CliEvent] = []
        if kind == "system" and data.get("subtype") == "init":
            session = data.get("session_id")
            if isinstance(session, str) and session:
                events.append(CliSession(session))
        elif kind == "assistant":
            message = data.get("message") or {}
            if isinstance(message, dict) and data.get("parent_tool_use_id") is None:
                for block in message.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and str(block.get("text", "")).strip():
                        events.append(CliText(redact(str(block["text"]))))
                    elif block.get("type") == "tool_use" and block.get("id"):
                        call_id, name = str(block["id"]), str(block.get("name") or "tool")
                        self._open.add(call_id)
                        events.append(
                            CliToolStart(call_id, name, _tool_summary(name, block.get("input")))
                        )
                        plan = _plan_text(name, block.get("input"))
                        if plan:
                            events.append(CliPlan(plan))
        elif kind == "user":
            message = data.get("message") or {}
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                call_id = str(block.get("tool_use_id") or "")
                if call_id in self._open:
                    self._open.discard(call_id)
                    events.append(
                        CliToolEnd(
                            call_id,
                            bool(block.get("is_error")),
                            _short(_result_text(block.get("content"))),
                        )
                    )
        elif kind == "result":
            usage = _dict(data.get("usage"))
            # ``usage`` counts this invocation only, but ``total_cost_usd`` counts the whole CLI
            # session after a resume. So the cost comes from the price table.
            counts = TokenCounts(
                input=_int(usage.get("input_tokens")),
                output=_int(usage.get("output_tokens")),
                cache_read=_int(usage.get("cache_read_input_tokens")),
                cache_write=_int(usage.get("cache_creation_input_tokens")),
            )
            try:
                cost = self._prices.cost(Provider.ANTHROPIC, self._model, counts)
            except KeyError:
                cost = Decimal(0)
            events.append(
                CliUsage(
                    Usage(
                        input_tokens=counts.input,
                        output_tokens=counts.output,
                        cache_read_tokens=counts.cache_read,
                        cache_write_tokens=counts.cache_write,
                        cost_usd=cost,
                    )
                )
            )
            session = data.get("session_id")
            if isinstance(session, str) and session:
                events.append(CliSession(session))
            is_error = bool(data.get("is_error")) or data.get("subtype") not in (None, "success")
            text = data.get("result")
            message_text = ""
            if is_error:
                errors = data.get("errors")
                message_text = _short(
                    errors if errors else (text or data.get("subtype") or "the CLI failed"), 1000
                )
            result = redact(text) if isinstance(text, str) else ""
            events.append(CliResult(result, is_error, message_text))
        return events


class CodexStream:
    """Parses ``codex exec --json``. The usage of each turn has no price, so the stream prices
    it with the price table of the model proxy (the proxy record stays the source of truth)."""

    agent: Final = CodingAgent.CODEX

    def __init__(
        self, model: str, prices: PriceTable | None = None, *, baseline: Usage | None = None
    ) -> None:
        self._model = model
        self._prices = prices or PriceTable.load()
        self._last_text = ""
        self._baseline = baseline or Usage()
        self._usage = Usage()
        self.thread_usage: Usage | None = None
        """The usage of the whole thread at the last ``turn.completed``."""
        self._failed = ""

    def feed(self, line: str) -> list[CliEvent]:
        data = _load(line)
        if data is None:
            return []
        kind = data.get("type")
        events: list[CliEvent] = []
        if kind == "thread.started":
            thread = data.get("thread_id")
            if isinstance(thread, str) and thread:
                events.append(CliSession(thread))
        elif kind in ("item.started", "item.completed"):
            item = data.get("item")
            if isinstance(item, dict):
                events += self._item(item, done=kind == "item.completed")
        elif kind == "turn.completed":
            # The usage of ``turn.completed`` counts the whole thread, also the turns of the
            # earlier invocations. ``baseline`` is the count at the end of the last one.
            self.thread_usage = self._turn_usage(_dict(data.get("usage")))
            self._usage = _minus(self.thread_usage, self._baseline)
            events.append(CliUsage(self._usage))
            events.append(CliResult(self._last_text, False))
        elif kind == "turn.failed":
            error = data.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            events.append(CliResult(self._last_text, True, _short(message or "the turn failed")))
        elif kind == "error":
            # A stream error. Codex can retry after it, so it does not end the turn.
            self._failed = _short(data.get("message") or "error", 1000)
        return events

    @property
    def last_error(self) -> str:
        return self._failed

    def _item(self, item: dict[str, Any], *, done: bool) -> list[CliEvent]:
        item_id = str(item.get("id") or "")
        kind = item.get("type") or item.get("item_type")
        if kind in ("agent_message", "assistant_message"):
            text = redact(str(item.get("text") or ""))
            if done and text.strip():
                self._last_text = text
                return [CliText(text)]
            return []
        tool: tuple[str, str] | None = None
        if kind == "command_execution":
            tool = ("Bash", _short(item.get("command") or "", 160))
        elif kind == "file_change":
            changes = item.get("changes") or []
            paths = [
                f"{c.get('kind', 'update')} {c.get('path', '')}"
                for c in changes
                if isinstance(c, dict)
            ]
            tool = ("Edit", _short(", ".join(paths), 160))
        elif kind == "mcp_tool_call":
            tool = (str(item.get("tool") or "mcp"), _short(item.get("arguments") or "", 160))
        elif kind == "web_search":
            tool = ("WebSearch", _short(item.get("query") or "", 160))
        if tool is None or not item_id:
            return []
        if not done:
            return [CliToolStart(item_id, *tool)]
        failed = item.get("status") == "failed" or item.get("exit_code") not in (None, 0)
        preview = _short(item.get("aggregated_output") or item.get("error") or "")
        if kind == "file_change":
            # A file change has no started event.
            return [CliToolStart(item_id, *tool), CliToolEnd(item_id, failed, preview)]
        return [CliToolEnd(item_id, failed, preview)]

    def _turn_usage(self, usage: dict[str, Any]) -> Usage:
        cached = _int(usage.get("cached_input_tokens"))
        written = _int(usage.get("cache_write_input_tokens"))
        # OpenAI input tokens include the cached tokens and the cache writes (Section 16.6).
        fresh = max(_int(usage.get("input_tokens")) - cached - written, 0)
        output = _int(usage.get("output_tokens"))
        counts = TokenCounts(input=fresh, output=output, cache_read=cached, cache_write=written)
        try:
            cost = self._prices.cost(Provider.OPENAI, self._model, counts)
        except KeyError:
            cost = Decimal(0)
        return Usage(
            input_tokens=fresh,
            output_tokens=output,
            cache_read_tokens=cached,
            cache_write_tokens=written,
            cost_usd=cost,
        )


def _minus(total: Usage, before: Usage) -> Usage:
    """``total - before``, with no count below zero (a thread that the CLI started again)."""
    if total.total_tokens < before.total_tokens:
        return total
    return Usage(
        input_tokens=max(total.input_tokens - before.input_tokens, 0),
        output_tokens=max(total.output_tokens - before.output_tokens, 0),
        cache_read_tokens=max(total.cache_read_tokens - before.cache_read_tokens, 0),
        cache_write_tokens=max(total.cache_write_tokens - before.cache_write_tokens, 0),
        cost_usd=max(total.cost_usd - before.cost_usd, Decimal(0)),
    )


def stream_for(
    agent: CodingAgent,
    model: str,
    prices: PriceTable | None = None,
    *,
    baseline: Usage | None = None,
) -> ClaudeStream | CodexStream:
    if agent is CodingAgent.CLAUDE_CODE:
        return ClaudeStream(model, prices)
    return CodexStream(model, prices, baseline=baseline)
