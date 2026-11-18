"""A scripted Anthropic Messages API for the conformance suite. It calls no real model.

The Claude CLI talks to it through ``ANTHROPIC_BASE_URL``, the same way as to the sama model
proxy (decision D6). So the suite runs the real CLI, its hooks, its MCP client, and its
limits, but the answers are fixed and free.

The model follows rules on the last user turn, so extra calls by the CLI do not break a
script:

- A tool result: reply ``"tool result: <text>"``.
- ``CALL <tool> <json>``: call that tool with that input.
- ``SLOW <n>``: stream ``n`` words, one every ``SLOW_DELAY_S`` seconds.
- A summary request (compaction): reply with a short summary.
- ``Remember the word <w>``: reply ``"OK"``.
- ``What word``: reply with the word from an earlier ``Remember the word`` turn. This proves
  that the CLI sent the earlier turns, so the session really resumed.
- Any other text: reply ``"echo: <text>"``.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp

SLOW_DELAY_S = 0.5
INPUT_TOKENS = 20
_CALL = re.compile(r"CALL (\S+) (\{.*\})", re.S)
_SLOW = re.compile(r"SLOW (\d+)")
_SUMMARY_HINTS = ("summary of the conversation", "summarize", "summarise")
_REMEMBER = re.compile(r"Remember the word (\w+)")


@dataclass
class ModelRequest:
    """One request that the scripted model got, for assertions."""

    model: str
    stream: bool
    last_user_text: str
    earlier_text: str
    tool_results: list[str]
    tool_names: list[str]


@dataclass
class ScriptedModel:
    requests: list[ModelRequest] = field(default_factory=list)
    _ids: Iterator[int] = field(default_factory=lambda: iter(range(1, 1_000_000)))

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/v1/messages", self._messages, methods=["POST"]),
                Route("/v1/messages/count_tokens", self._count, methods=["POST"]),
            ]
        )

    async def _count(self, request: Request) -> Response:
        return JSONResponse({"input_tokens": INPUT_TOKENS})

    async def _messages(self, request: Request) -> Response:
        body = await request.json()
        seen = _parse(body)
        self.requests.append(seen)
        blocks, delay = self._answer(seen)
        message_id = f"msg_{next(self._ids)}"
        if not seen.stream:
            return JSONResponse(_message(message_id, seen.model, blocks))
        return StreamingResponse(
            _stream(message_id, seen.model, blocks, delay), media_type="text/event-stream"
        )

    def _answer(self, seen: ModelRequest) -> tuple[list[dict[str, Any]], float]:
        text = seen.last_user_text
        if seen.tool_results:
            return [_text("tool result: " + " | ".join(seen.tool_results))], 0.0
        if any(hint in text.lower() for hint in _SUMMARY_HINTS):
            return [_text("<summary>The user tested sama.</summary>")], 0.0
        call = _CALL.search(text)
        if call:
            tool_id = f"toolu_{next(self._ids):06d}"
            block = {"type": "tool_use", "id": tool_id, "name": call[1]}
            return [block | {"input": json.loads(call[2])}], 0.0
        if _REMEMBER.search(text):
            return [_text("OK")], 0.0
        if text.startswith("What word"):
            remembered = _REMEMBER.search(seen.earlier_text)
            return [_text(remembered[1] if remembered else "I do not know.")], 0.0
        slow = _SLOW.search(text)
        if slow:
            words = [f"word{i} " for i in range(int(slow[1]))]
            return [_text("".join(words))], SLOW_DELAY_S
        return [_text(f"echo: {text}")], 0.0


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _content_text(content: Any) -> tuple[str, list[str]]:
    """The text and the tool results of one message content."""
    if isinstance(content, str):
        return content, []
    texts: list[str] = []
    results: list[str] = []
    for block in content:
        # The CLI adds context blocks to the user turn. The script reads only the user text.
        if block.get("type") == "text" and not block["text"].startswith("<system-reminder>"):
            texts.append(block["text"])
        elif block.get("type") == "tool_result":
            inner, _ = _content_text(block.get("content") or "")
            results.append(inner)
    return "\n".join(texts), results


def _parse(body: dict[str, Any]) -> ModelRequest:
    messages = body.get("messages") or []
    last = messages[-1] if messages else {"content": ""}
    text, results = _content_text(last.get("content", ""))
    earlier = "\n".join(_content_text(m.get("content", ""))[0] for m in messages[:-1])
    return ModelRequest(
        model=str(body.get("model", "")),
        stream=bool(body.get("stream")),
        last_user_text=text,
        earlier_text=earlier,
        tool_results=results,
        tool_names=[str(tool.get("name")) for tool in body.get("tools") or []],
    )


def _usage(output_tokens: int) -> dict[str, int]:
    return {"input_tokens": INPUT_TOKENS, "output_tokens": output_tokens}


def _stop_reason(blocks: list[dict[str, Any]]) -> str:
    return "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"


def _message(message_id: str, model: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": _stop_reason(blocks),
        "stop_sequence": None,
        "usage": _usage(len(blocks) * 5),
    }


async def _stream(
    message_id: str, model: str, blocks: list[dict[str, Any]], delay: float
) -> AsyncIterator[str]:
    def event(data: dict[str, Any]) -> str:
        return f"event: {data['type']}\ndata: {json.dumps(data)}\n\n"

    start = _message(message_id, model, []) | {"stop_reason": None, "usage": _usage(1)}
    yield event({"type": "message_start", "message": start})
    output_tokens = 1
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            yield event(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                }
            )
            pieces = block["text"].split(" ") if delay else [block["text"]]
            for number, piece in enumerate(pieces):
                if delay:
                    await asyncio.sleep(delay)
                chunk = piece if number == len(pieces) - 1 else piece + " "
                output_tokens += 1
                yield event(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": chunk},
                    }
                )
        else:
            start_block = {
                "type": "tool_use",
                "id": block["id"],
                "name": block["name"],
                "input": {},
            }
            yield event(
                {"type": "content_block_start", "index": index, "content_block": start_block}
            )
            yield event(
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(block["input"]),
                    },
                }
            )
            output_tokens += 5
        yield event({"type": "content_block_stop", "index": index})
    yield event(
        {
            "type": "message_delta",
            "delta": {"stop_reason": _stop_reason(blocks), "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        }
    )
    yield event({"type": "message_stop"})


@contextmanager
def serve(app: ASGIApp) -> Iterator[str]:
    """Serves ``app`` on a free local port in a thread. Yields the base URL.

    The socket stays bound from the port choice to the server start, so no other process can
    take the port in between.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                raise RuntimeError("the local test server did not start")
            time.sleep(0.02)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
