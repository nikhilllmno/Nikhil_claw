"""A scripted OpenAI Responses API for the conformance suite. It calls no real model.

The OpenAI adapter talks to it through the ``base_url`` of ``AsyncOpenAI``, the same way as to
the sama model proxy (decision D6). So the suite runs the real OpenAI Agents SDK, its MCP
client, its sandbox shell tool, and its approvals, but the answers are fixed and free.

The rules are the rules of the scripted Anthropic model (``scripted_model.py``), on the last
user turn:

- A tool result: reply ``"tool result: <text>"``.
- ``CALL <tool> <json>``: call that tool with that input. ``CALL Bash {"command": ...}``
  becomes a call of the sandbox shell tool ``exec_command`` with ``cmd``, and the MCP name
  ``mcp__sama__<tool>`` becomes ``<tool>``: the names that the SDK gives these tools.
- ``SLOW <n>``: stream ``n`` words, one every ``SLOW_DELAY_S`` seconds.
- ``AUTOCOMPACT <text>``: a ``compaction`` item first, then ``"echo: <text>"``. It stands for
  the automatic compaction of the Responses API.
- ``Remember the word <w>``: reply ``"OK"``.
- ``What word``: reply with the word from an earlier ``Remember the word`` turn, also from the
  user messages that a compaction kept.
- Any other text: reply ``"echo: <text>"``.

``POST /v1/responses/compact`` keeps the user messages and adds one ``compaction`` item.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from tests.conformance.scripted_model import SLOW_DELAY_S

INPUT_TOKENS = 40
CACHED_TOKENS = 8
_CALL = re.compile(r"CALL (\S+) (\{.*\})", re.S)
_SLOW = re.compile(r"SLOW (\d+)")
_REMEMBER = re.compile(r"Remember the word (\w+)")
_MCP_PREFIX = "mcp__sama__"
SHELL_TOOL = "exec_command"


@dataclass
class ResponsesRequest:
    """One request that the scripted model got, for assertions."""

    model: str
    stream: bool
    last_user_text: str
    earlier_text: str
    tool_results: list[str]
    tool_names: list[str]
    instructions: str
    input_types: list[str]
    body: dict[str, Any]


@dataclass
class ScriptedResponses:
    requests: list[ResponsesRequest] = field(default_factory=list)
    compactions: list[dict[str, Any]] = field(default_factory=list)
    _ids: Iterator[int] = field(default_factory=lambda: iter(range(1, 1_000_000)))

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/v1/responses", self._responses, methods=["POST"]),
                Route("/v1/responses/compact", self._compact, methods=["POST"]),
            ]
        )

    def _id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._ids):06d}"

    async def _responses(self, request: Request) -> Response:
        body = await request.json()
        seen = _parse(body)
        self.requests.append(seen)
        items, delay = self._answer(seen)
        response = self._response(seen.model, items)
        if not seen.stream:
            return JSONResponse(response)
        return StreamingResponse(_stream(response, delay), media_type="text/event-stream")

    async def _compact(self, request: Request) -> Response:
        body = await request.json()
        self.compactions.append(body)
        kept = [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            }
            for text in _user_texts(body.get("input") or [])
        ]
        output = [*kept, self._compaction_item()]
        return JSONResponse(
            {
                "id": self._id("cmp"),
                "object": "response.compaction",
                "created_at": int(time.time()),
                "output": output,
                "usage": _usage(INPUT_TOKENS, 12),
            }
        )

    def _compaction_item(self) -> dict[str, Any]:
        return {
            "id": self._id("cmpitem"),
            "type": "compaction",
            "encrypted_content": "scripted-compaction",
        }

    def _answer(self, seen: ResponsesRequest) -> tuple[list[dict[str, Any]], float]:
        text = seen.last_user_text
        if seen.tool_results:
            return [self._message("tool result: " + " | ".join(seen.tool_results))], 0.0
        call = _CALL.search(text)
        if call:
            name, arguments = call[1], json.loads(call[2])
            if name == "Bash":
                name, arguments = SHELL_TOOL, {"cmd": arguments["command"]}
            name = name.removeprefix(_MCP_PREFIX)
            return [
                {
                    "id": self._id("fc"),
                    "type": "function_call",
                    "call_id": self._id("call"),
                    "name": name,
                    "arguments": json.dumps(arguments),
                    "status": "completed",
                }
            ], 0.0
        if text.startswith("AUTOCOMPACT "):
            return [self._compaction_item(), self._message(f"echo: {text}")], 0.0
        if _REMEMBER.search(text):
            return [self._message("OK")], 0.0
        if text.startswith("What word"):
            remembered = _REMEMBER.search(seen.earlier_text)
            return [self._message(remembered[1] if remembered else "I do not know.")], 0.0
        slow = _SLOW.search(text)
        if slow:
            words = [f"word{i} " for i in range(int(slow[1]))]
            return [self._message("".join(words))], SLOW_DELAY_S
        return [self._message(f"echo: {text}")], 0.0

    def _message(self, text: str) -> dict[str, Any]:
        return {
            "id": self._id("msg"),
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": [], "logprobs": []}],
        }

    def _response(self, model: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        output_tokens = sum(
            len(part["text"].split()) + 1
            for item in items
            if item["type"] == "message"
            for part in item["content"]
        ) + 5 * sum(1 for item in items if item["type"] == "function_call")
        return {
            "id": self._id("resp"),
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": model,
            "output": items,
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": [],
            "usage": _usage(INPUT_TOKENS, output_tokens),
        }


def _usage(input_tokens: int, output_tokens: int) -> dict[str, Any]:
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": CACHED_TOKENS},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": input_tokens + output_tokens,
    }


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    texts = [
        part.get("text", "")
        for part in content or []
        if isinstance(part, dict) and part.get("type") in {"input_text", "output_text"}
    ]
    return "\n".join(texts)


def _user_texts(items: list[Any]) -> list[str]:
    return [
        _content_text(item.get("content"))
        for item in items
        if isinstance(item, dict) and item.get("role") == "user"
    ]


def _parse(body: dict[str, Any]) -> ResponsesRequest:
    raw = body.get("input") or []
    items: list[dict[str, Any]] = (
        [{"role": "user", "content": raw}] if isinstance(raw, str) else list(raw)
    )
    results: list[str] = []
    for item in reversed(items):
        if item.get("type") != "function_call_output":
            break
        output = item.get("output")
        results.insert(0, output if isinstance(output, str) else json.dumps(output))
    users = [i for i in items if i.get("role") == "user"]
    last = _content_text(users[-1].get("content")) if users else ""
    earlier = "\n".join(_content_text(i.get("content")) for i in users[:-1])
    return ResponsesRequest(
        model=str(body.get("model", "")),
        stream=bool(body.get("stream")),
        last_user_text=last.strip(),
        earlier_text=earlier,
        tool_results=results,
        tool_names=[str(t.get("name")) for t in body.get("tools") or []],
        instructions=str(body.get("instructions") or ""),
        input_types=[str(i.get("type", "message")) for i in items],
        body=body,
    )


async def _stream(response: dict[str, Any], delay: float) -> AsyncIterator[str]:
    sequence = iter(range(1_000_000))

    def event(data: dict[str, Any]) -> str:
        data = {**data, "sequence_number": next(sequence)}
        return f"event: {data['type']}\ndata: {json.dumps(data)}\n\n"

    start = {**response, "status": "in_progress", "output": [], "usage": None}
    yield event({"type": "response.created", "response": start})
    for index, item in enumerate(response["output"]):
        if item["type"] != "message":
            yield event({"type": "response.output_item.added", "output_index": index, "item": item})
            yield event({"type": "response.output_item.done", "output_index": index, "item": item})
            continue
        empty = {**item, "status": "in_progress", "content": []}
        yield event({"type": "response.output_item.added", "output_index": index, "item": empty})
        text = item["content"][0]["text"]
        part = {"type": "output_text", "text": "", "annotations": [], "logprobs": []}
        yield event(
            {
                "type": "response.content_part.added",
                "item_id": item["id"],
                "output_index": index,
                "content_index": 0,
                "part": part,
            }
        )
        pieces = text.split(" ") if delay else [text]
        for number, piece in enumerate(pieces):
            if delay:
                await asyncio.sleep(delay)
            chunk = piece if number == len(pieces) - 1 else piece + " "
            yield event(
                {
                    "type": "response.output_text.delta",
                    "item_id": item["id"],
                    "output_index": index,
                    "content_index": 0,
                    "delta": chunk,
                    "logprobs": [],
                }
            )
        yield event(
            {
                "type": "response.output_text.done",
                "item_id": item["id"],
                "output_index": index,
                "content_index": 0,
                "text": text,
                "logprobs": [],
            }
        )
        yield event(
            {
                "type": "response.content_part.done",
                "item_id": item["id"],
                "output_index": index,
                "content_index": 0,
                "part": item["content"][0],
            }
        )
        yield event({"type": "response.output_item.done", "output_index": index, "item": item})
    yield event({"type": "response.completed", "response": response})
