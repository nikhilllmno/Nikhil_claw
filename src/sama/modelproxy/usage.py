"""Usage from provider responses (docs/architecture.md, Section 16.6).

The proxy passes each response through unchanged. A ``UsageReader`` sees the same bytes and
finds the token counts: in the JSON body, or in the server-sent events of a stream.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Final

from sama.modelproxy.prices import Provider, TokenCounts

MAX_BODY_BYTES: Final = 64 * 1024 * 1024
"""A JSON response that is larger than this is passed on, but its usage is not read."""
_OPENAI_STREAM_END: Final = frozenset(
    {"response.completed", "response.incomplete", "response.failed"}
)


class Endpoint(StrEnum):
    MESSAGES = "messages"
    COUNT_TOKENS = "count_tokens"
    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"


def _int(data: Mapping[str, Any] | None, key: str) -> int:
    value = data.get(key) if data else None
    return value if isinstance(value, int) and value >= 0 else 0


def _dict(data: Mapping[str, Any] | None, key: str) -> Mapping[str, Any] | None:
    value = data.get(key) if data else None
    return value if isinstance(value, Mapping) else None


def anthropic_counts(usage: Mapping[str, Any]) -> TokenCounts:
    """Anthropic ``input_tokens`` has no cache tokens. ``cache_creation`` splits the writes
    by lifetime. Without it, all writes are 5-minute writes."""
    creation = _dict(usage, "cache_creation")
    if creation is not None:
        short = _int(creation, "ephemeral_5m_input_tokens")
        long = _int(creation, "ephemeral_1h_input_tokens")
    else:
        short, long = _int(usage, "cache_creation_input_tokens"), 0
    return TokenCounts(
        input=_int(usage, "input_tokens"),
        output=_int(usage, "output_tokens"),
        cache_read=_int(usage, "cache_read_input_tokens"),
        cache_write=short,
        cache_write_long=long,
        web_search_requests=_int(_dict(usage, "server_tool_use"), "web_search_requests"),
    )


def openai_counts(usage: Mapping[str, Any]) -> TokenCounts:
    """OpenAI input counts include the cached tokens and the cache writes.

    The Responses API uses ``input_tokens``. Chat Completions uses ``prompt_tokens``.
    """
    if "input_tokens" in usage:
        total, output = _int(usage, "input_tokens"), _int(usage, "output_tokens")
        details = _dict(usage, "input_tokens_details")
    else:
        total, output = _int(usage, "prompt_tokens"), _int(usage, "completion_tokens")
        details = _dict(usage, "prompt_tokens_details")
    cached = _int(details, "cached_tokens")
    writes = _int(details, "cache_write_tokens")
    by_ttl = _dict(details, "cache_write_tokens_details")
    if by_ttl is not None:
        short, long = _int(by_ttl, "ttl_30m_tokens"), _int(by_ttl, "ttl_12h_tokens")
    else:
        short, long = writes, 0
    return TokenCounts(
        input=max(0, total - cached - short - long),
        output=output,
        cache_read=cached,
        cache_write=short,
        cache_write_long=long,
    )


def _counts(provider: Provider, usage: Mapping[str, Any]) -> TokenCounts:
    if provider is Provider.ANTHROPIC:
        return anthropic_counts(usage)
    return openai_counts(usage)


class _SseLines:
    """Splits a byte stream into the ``data`` payloads of server-sent events."""

    def __init__(self) -> None:
        self._buffer = b""
        self._data: list[bytes] = []

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer += chunk
        events: list[bytes] = []
        while True:
            end = self._buffer.find(b"\n")
            if end < 0:
                return events
            line = self._buffer[:end].rstrip(b"\r")
            self._buffer = self._buffer[end + 1 :]
            if not line:
                if self._data:
                    events.append(b"\n".join(self._data))
                    self._data = []
            elif line.startswith(b"data:"):
                self._data.append(line[5:].lstrip(b" "))


class UsageReader:
    """Reads the usage of one call from the bytes of its response."""

    def __init__(self, provider: Provider, *, stream: bool) -> None:
        self._provider = provider
        self._stream = stream
        self._sse = _SseLines()
        self._body = bytearray()
        self._too_big = False
        self._usage: dict[str, Any] = {}
        self._seen = False

    def feed(self, chunk: bytes) -> None:
        if not self._stream:
            if not self._too_big:
                self._body += chunk
                if len(self._body) > MAX_BODY_BYTES:
                    self._too_big = True
                    self._body.clear()
            return
        for data in self._sse.feed(chunk):
            self._event(data)

    def counts(self) -> TokenCounts | None:
        """The usage, or ``None`` if the response had none (for example, an error)."""
        if not self._stream and not self._too_big and self._body:
            self._json_body()
        if not self._seen:
            return None
        return _counts(self._provider, self._usage)

    def _json_body(self) -> None:
        try:
            body = json.loads(self._body)
        except ValueError:
            return
        usage = _dict(body, "usage") if isinstance(body, Mapping) else None
        if usage is not None:
            self._merge(usage)

    def _event(self, data: bytes) -> None:
        if data == b"[DONE]":
            return
        try:
            event = json.loads(data)
        except ValueError:
            return
        if not isinstance(event, Mapping):
            return
        kind = event.get("type")
        if self._provider is Provider.ANTHROPIC:
            if kind == "message_start":
                usage = _dict(_dict(event, "message"), "usage")
            elif kind == "message_delta":
                usage = _dict(event, "usage")
            else:
                usage = None
        elif kind in _OPENAI_STREAM_END:
            usage = _dict(_dict(event, "response"), "usage")
        elif kind is None:
            # A Chat Completions chunk. The last chunk has the usage.
            usage = _dict(event, "usage")
        else:
            usage = None
        if usage is not None:
            self._merge(usage)

    def _merge(self, usage: Mapping[str, Any]) -> None:
        """A later Anthropic ``message_delta`` has the final counts. It can leave out the
        input counts, so the values of ``message_start`` stay."""
        self._seen = True
        for key, value in usage.items():
            if value is not None:
                self._usage[key] = value
