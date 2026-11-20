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
MAX_EVENT_BYTES: Final = 16 * 1024 * 1024
"""A stream line or event that is larger than this stops the usage reader."""
DEFAULT_OUTPUT_ESTIMATE: Final = 64_000
"""The output tokens of an estimate when the request sets no output limit."""
_OUTPUT_LIMITS: Final = ("max_tokens", "max_output_tokens", "max_completion_tokens")
_OPENAI_STREAM_END: Final = frozenset(
    {"response.completed", "response.incomplete", "response.failed"}
)


class Endpoint(StrEnum):
    MESSAGES = "messages"
    COUNT_TOKENS = "count_tokens"
    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"
    IMAGES = "images"
    """``image_generate`` (M4.9). The tool server calls it, not the proxy."""
    SPEECH = "speech"
    """``text_to_speech`` (M4.9). The input count is the number of characters."""


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


class _SseLines:
    """Splits a byte stream into the ``data`` payloads of server-sent events.

    A line or an event of more than ``limit`` bytes sets ``overflow``, and the parser stops.
    """

    def __init__(self, limit: int = MAX_EVENT_BYTES) -> None:
        self._limit = limit
        self._buffer = bytearray()
        self._data: list[bytes] = []
        self._data_bytes = 0
        self.overflow = False

    def feed(self, chunk: bytes) -> list[bytes]:
        if self.overflow:
            return []
        start = len(self._buffer)
        self._buffer += chunk
        events: list[bytes] = []
        line_start = 0
        while (end := self._buffer.find(b"\n", max(start, line_start))) >= 0:
            if end - line_start > self._limit:
                return self._overflow(events)
            line = bytes(self._buffer[line_start:end]).rstrip(b"\r")
            line_start = end + 1
            if not line:
                if self._data:
                    events.append(b"\n".join(self._data))
                    self._data, self._data_bytes = [], 0
            elif line.startswith(b"data:"):
                self._data_bytes += len(line)
                if self._data_bytes > self._limit:
                    return self._overflow(events)
                self._data.append(line[5:].lstrip(b" "))
        del self._buffer[:line_start]
        if len(self._buffer) > self._limit:
            return self._overflow(events)
        return events

    def _overflow(self, events: list[bytes]) -> list[bytes]:
        """Stops the parser. The complete events before the large one stay."""
        self.overflow = True
        self._buffer.clear()
        self._data.clear()
        return events


class UsageReader:
    """Reads the usage of one call from the bytes of its response."""

    def __init__(self, provider: Provider, *, stream: bool) -> None:
        self._provider = provider
        self._stream = stream
        self._sse = _SseLines()
        self._body = bytearray()
        self._too_big = False
        self._usage: dict[str, Any] = {}
        self._final = False

    def feed(self, chunk: bytes) -> None:
        if self._final and self._stream:
            return
        if not self._stream:
            if not self._too_big:
                self._body += chunk
                if len(self._body) > MAX_BODY_BYTES:
                    self._too_big = True
                    self._body.clear()
            return
        for data in self._sse.feed(chunk):
            self._event(data)

    @property
    def final(self) -> bool:
        """True when the reader saw the final usage of the call: the JSON body, the Anthropic
        ``message_delta``, the OpenAI terminal event, or the last Chat Completions chunk."""
        if not self._stream and not self._too_big and self._body and not self._final:
            self._json_body()
        return self._final

    def counts(self) -> TokenCounts:
        """The usage so far. A response with no usage (for example, an error) has zero counts."""
        if not self._stream and not self._too_big and self._body and not self._final:
            self._json_body()
        if self._provider is Provider.ANTHROPIC:
            return anthropic_counts(self._usage)
        return openai_counts(self._usage)

    def _json_body(self) -> None:
        try:
            body = json.loads(self._body)
        except ValueError:
            return
        usage = _dict(body, "usage") if isinstance(body, Mapping) else None
        if usage is not None:
            self._merge(usage, final=True)

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
        final = True
        if self._provider is Provider.ANTHROPIC:
            if kind == "message_start":
                usage, final = _dict(_dict(event, "message"), "usage"), False
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
            self._merge(usage, final=final)

    def _merge(self, usage: Mapping[str, Any], *, final: bool) -> None:
        """A later Anthropic ``message_delta`` has the final counts. It can leave out the
        input counts, so the values of ``message_start`` stay."""
        self._final = self._final or final
        for key, value in usage.items():
            if value is not None:
                self._usage[key] = value


def estimate_counts(request_bytes: int, body: Mapping[str, Any], seen: TokenCounts) -> TokenCounts:
    """A high estimate for a call whose final usage is not known (Section 16.6).

    The input is one token for every 2 bytes of the request. A token has about 4 bytes of
    text, so the estimate is high. The output is the output limit of the request. Counts that
    the reader saw stay if they are higher.
    """
    limit = next((body[k] for k in _OUTPUT_LIMITS if isinstance(body.get(k), int)), None)
    output = limit if limit is not None and limit > 0 else DEFAULT_OUTPUT_ESTIMATE
    return seen.model_copy(
        update={
            "input": max(seen.input, -(-request_bytes // 2) - seen.cache_read),
            "output": max(seen.output, output),
        }
    )
