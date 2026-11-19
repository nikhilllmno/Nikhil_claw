"""Unit tests of the Telegram parts that need no database."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from sama.gateway.pairing import (
    CODE_ALPHABET,
    CODE_LENGTH,
    GLOBAL_FAILURES_PER_MINUTE,
    LOCKOUT_S,
    MAX_FAILURES,
    Lockout,
    code_hash,
    new_code,
    normalize,
)
from sama.gateway.telegram import SPLIT_AT, split_point
from sama.gateway.telegram_client import TelegramClient, TelegramError, bot_id
from sama.gateway.transcribe import OpenAITranscriber, TranscribeError

TOKEN = SecretStr("123456:secret-token")


def test_codes_use_the_alphabet_and_normalize() -> None:
    code = new_code()
    assert len(code) == CODE_LENGTH
    assert set(code) <= set(CODE_ALPHABET)
    spaced = f"{code[:4].lower()} - {code[4:]}"
    assert normalize(spaced) == code
    assert normalize("hello") is None
    assert normalize("ABCD0OI1") is None, "0, O, I, and 1 are not in codes"
    assert code_hash(code) != code
    assert len(code_hash(code)) == 64


def test_the_lockout_locks_after_five_failures_for_one_hour() -> None:
    lockout = Lockout()
    for i in range(MAX_FAILURES - 1):
        lockout.failed("a", now=100.0 + i)
        assert not lockout.locked("a", now=100.0 + i)
    lockout.failed("a", now=200.0)
    assert lockout.locked("a", now=200.0)
    assert lockout.locked("a", now=200.0 + LOCKOUT_S - 1)
    assert not lockout.locked("a", now=200.0 + LOCKOUT_S + 1)
    assert not lockout.locked("b", now=200.0)
    lockout.failed("b", now=300.0)
    lockout.succeeded("b")
    for i in range(MAX_FAILURES - 1):
        lockout.failed("b", now=301.0 + i)
    assert not lockout.locked("b", now=310.0), "a success clears the count"


def test_the_lockout_has_a_global_limit_and_a_size_limit() -> None:
    lockout = Lockout(max_keys=3)
    for i in range(GLOBAL_FAILURES_PER_MINUTE):
        lockout.failed(f"k{i}", now=10.0)
    assert lockout.locked("new", now=20.0), "too many failures in one minute lock everyone"
    assert not lockout.locked("new", now=80.0)
    assert len(lockout._failures) <= 3


def test_an_attempt_counts_before_the_check() -> None:
    lockout = Lockout()
    # Tries at the same time: each attempt counts at once, so the sixth is refused.
    assert all(lockout.attempt("a", now=1.0) for _ in range(MAX_FAILURES))
    assert not lockout.attempt("a", now=1.0)
    assert lockout.attempt("b", now=1.0)
    lockout.succeeded("b")
    assert not lockout.locked("b", now=1.0)


def test_split_point_prefers_a_line_end_then_a_space() -> None:
    text = "a" * 3000 + "\n" + "b" * 2000
    assert split_point(text) == 3001
    spaced = "a" * 3500 + " " + "b" * 2000
    assert split_point(spaced) == 3501
    assert split_point("x" * 5000) == SPLIT_AT


def test_bot_id_reads_the_token() -> None:
    assert bot_id(TOKEN) == 123456
    assert bot_id(SecretStr("nope")) is None


class Recorder:
    def __init__(self, replies: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.replies = replies or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/file/" in path:
            self.calls.append(("file", {"path": path}))
            return httpx.Response(200, content=self.replies.get("file", b"OGG"))
        method = path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        reply = self.replies.get(method, {"ok": True, "result": {"message_id": 7}})
        return httpx.Response(200, json=reply)


def client(recorder: Recorder) -> TelegramClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return TelegramClient(TOKEN, base_url="https://tg.test", http=http)


async def test_the_client_sends_plain_text_with_buttons() -> None:
    recorder = Recorder()
    tg = client(recorder)
    buttons = [[{"text": "Approve", "callback_data": "ap:1:y"}]]
    assert await tg.send_message(5, "x" * 5000, reply_to=3, buttons=buttons) == 7
    method, body = recorder.calls[0]
    assert method == "sendMessage"
    assert len(body["text"]) == 4096
    assert body["reply_parameters"] == {"message_id": 3, "allow_sending_without_reply": True}
    assert body["reply_markup"] == {"inline_keyboard": buttons}
    assert "parse_mode" not in body
    await tg.edit_message(5, 7, "y")
    assert recorder.calls[1][1]["reply_markup"] == {"inline_keyboard": []}


async def test_client_errors_hide_the_token() -> None:
    recorder = Recorder(
        {
            "sendMessage": {"ok": False, "description": "Too Many Requests"},
            "editMessageText": {"ok": False, "description": "Bad Request: message is not modified"},
        }
    )
    tg = client(recorder)
    with pytest.raises(TelegramError) as caught:
        await tg.send_message(1, "x")
    assert "secret-token" not in str(caught.value)
    await tg.edit_message(1, 2, "same")  # "not modified" is not an error

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    failing = TelegramClient(
        TOKEN,
        base_url="https://tg.test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(broken)),
    )
    with pytest.raises(TelegramError) as caught:
        await failing.typing(1)
    assert "secret-token" not in str(caught.value)


async def test_the_client_waits_when_telegram_asks() -> None:
    limited = {
        "ok": False,
        "description": "Too Many Requests: retry after 0",
        "parameters": {"retry_after": 0},
    }
    replies = [limited, limited, {"ok": True, "result": {"message_id": 9}}]
    calls: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(429 if len(replies) > 1 else 200, json=replies.pop(0))

    http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    tg = TelegramClient(TOKEN, base_url="https://tg.test", http=http, max_retries=3)
    assert await tg.send_message(1, "x") == 9
    assert len(calls) == 3

    always = Recorder({"sendMessage": limited})
    capped = TelegramClient(
        TOKEN,
        base_url="https://tg.test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(always)),
        max_retries=2,
    )
    with pytest.raises(TelegramError):
        await capped.send_message(1, "x")
    assert len(always.calls) == 3, "the first try and two retries"

    too_long = Recorder({"sendMessage": limited | {"parameters": {"retry_after": 60}}})
    with pytest.raises(TelegramError):
        await client(too_long).send_message(1, "x")
    assert len(too_long.calls) == 1, "a wait above max_wait_s is not done"


async def test_the_client_downloads_with_a_size_cap() -> None:
    recorder = Recorder(
        {"getFile": {"ok": True, "result": {"file_path": "voice/a.oga", "file_size": 3}}}
    )
    tg = client(recorder)
    assert await tg.download("f1", max_bytes=10) == b"OGG"
    assert recorder.calls[-1] == ("file", {"path": "/file/bot123456:secret-token/voice/a.oga"})
    with pytest.raises(TelegramError, match="too large"):
        await tg.download("f1", max_bytes=2)
    big = Recorder({"getFile": {"ok": True, "result": {"file_path": "v", "file_size": 99}}})
    with pytest.raises(TelegramError, match="too large"):
        await client(big).download("f1", max_bytes=10)


async def test_the_transcriber_posts_audio() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": " hello there "})

    transcriber = OpenAITranscriber(
        SecretStr("sk-test"),
        base_url="https://ai.test/v1",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    text = await transcriber.transcribe(b"OGG", filename="v.ogg", mime_type="audio/ogg")
    assert text == "hello there"
    assert seen[0].url.path == "/v1/audio/transcriptions"
    assert seen[0].headers["authorization"] == "Bearer sk-test"

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="sk-test")

    broken = OpenAITranscriber(
        SecretStr("sk-test"),
        base_url="https://ai.test/v1",
        http=httpx.AsyncClient(transport=httpx.MockTransport(failing)),
    )
    with pytest.raises(TranscribeError) as caught:
        await broken.transcribe(b"OGG", filename="v.ogg", mime_type="audio/ogg")
    assert "sk-test" not in str(caught.value)
