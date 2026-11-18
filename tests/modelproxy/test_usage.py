"""Usage from provider responses (docs/architecture.md, Section 16.6)."""

from __future__ import annotations

import json
from typing import Any

from sama.modelproxy.prices import Provider, TokenCounts
from sama.modelproxy.usage import UsageReader, anthropic_counts, openai_counts


def sse(*events: dict[str, Any], named: bool = True) -> bytes:
    parts = []
    for event in events:
        head = f"event: {event.get('type', 'chunk')}\n" if named else ""
        parts.append(f"{head}data: {json.dumps(event)}\n\n")
    return "".join(parts).encode()


def read(provider: Provider, data: bytes, *, stream: bool, size: int = 7) -> TokenCounts | None:
    """Feeds ``data`` in small chunks, so events are split across chunks."""
    reader = UsageReader(provider, stream=stream)
    for start in range(0, len(data), size):
        reader.feed(data[start : start + size])
    return reader.counts()


def test_anthropic_cache_writes_are_split_by_lifetime() -> None:
    counts = anthropic_counts(
        {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 40,
            "cache_creation": {"ephemeral_5m_input_tokens": 15, "ephemeral_1h_input_tokens": 25},
            "server_tool_use": {"web_search_requests": 2},
        }
    )
    assert counts == TokenCounts(
        input=10,
        output=20,
        cache_read=30,
        cache_write=15,
        cache_write_long=25,
        web_search_requests=2,
    )


def test_anthropic_writes_without_detail_are_short_writes() -> None:
    counts = anthropic_counts({"input_tokens": 1, "cache_creation_input_tokens": 9})
    assert (counts.cache_write, counts.cache_write_long) == (9, 0)


def test_openai_input_does_not_count_the_cache_twice() -> None:
    counts = openai_counts(
        {
            "input_tokens": 12000,
            "output_tokens": 50,
            "input_tokens_details": {
                "cached_tokens": 4000,
                "cache_write_tokens": 7000,
                "cache_write_tokens_details": {"ttl_30m_tokens": 3000, "ttl_12h_tokens": 4000},
            },
        }
    )
    assert counts == TokenCounts(
        input=1000, output=50, cache_read=4000, cache_write=3000, cache_write_long=4000
    )


def test_openai_chat_completions_usage() -> None:
    counts = openai_counts(
        {
            "prompt_tokens": 100,
            "completion_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": 64},
        }
    )
    assert counts == TokenCounts(input=36, output=7, cache_read=64)


def test_bad_values_count_as_zero() -> None:
    counts = anthropic_counts({"input_tokens": -5, "output_tokens": "9", "cache_creation": []})
    assert counts == TokenCounts()


def test_an_anthropic_stream_keeps_the_input_of_message_start() -> None:
    data = sse(
        {
            "type": "message_start",
            "message": {
                "usage": {"input_tokens": 25, "cache_read_input_tokens": 5, "output_tokens": 1}
            },
        },
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
        {"type": "message_delta", "usage": {"output_tokens": 42}},
        {"type": "message_stop"},
    )
    assert read(Provider.ANTHROPIC, data, stream=True) == TokenCounts(
        input=25, output=42, cache_read=5
    )


def test_an_anthropic_json_body() -> None:
    body = json.dumps({"type": "message", "usage": {"input_tokens": 3, "output_tokens": 4}})
    assert read(Provider.ANTHROPIC, body.encode(), stream=False) == TokenCounts(input=3, output=4)


def test_an_error_body_has_no_usage() -> None:
    body = json.dumps({"type": "error", "error": {"type": "overloaded_error"}}).encode()
    assert read(Provider.ANTHROPIC, body, stream=False) is None
    assert read(Provider.ANTHROPIC, b"not json", stream=False) is None


def test_an_openai_responses_stream_reads_the_completed_event() -> None:
    data = sse(
        {"type": "response.created", "response": {"usage": None}},
        {"type": "response.output_text.delta", "delta": "hi"},
        {
            "type": "response.completed",
            "response": {"usage": {"input_tokens": 9, "output_tokens": 2}},
        },
    )
    assert read(Provider.OPENAI, data, stream=True) == TokenCounts(input=9, output=2)


def test_a_chat_completions_stream_reads_the_last_chunk() -> None:
    data = (
        sse(
            {"choices": [{"delta": {"content": "hi"}}], "usage": None},
            {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 3}},
            named=False,
        )
        + b"data: [DONE]\n\n"
    )
    assert read(Provider.OPENAI, data, stream=True) == TokenCounts(input=11, output=3)


def test_a_stream_with_crlf_lines() -> None:
    data = sse({"type": "message_delta", "usage": {"output_tokens": 6}}).replace(b"\n", b"\r\n")
    assert read(Provider.ANTHROPIC, data, stream=True) == TokenCounts(output=6)


def test_a_stream_that_stops_early_has_the_usage_so_far() -> None:
    data = sse({"type": "message_start", "message": {"usage": {"input_tokens": 8}}})
    assert read(Provider.ANTHROPIC, data + b"data: {broken", stream=True) == TokenCounts(input=8)
