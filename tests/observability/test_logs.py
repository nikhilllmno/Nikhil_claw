from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import Iterator

import pytest

from sama.observability.logs import configure_logging, current_context, log_context


@pytest.fixture
def stream(monkeypatch: pytest.MonkeyPatch) -> Iterator[io.StringIO]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    out = io.StringIO()
    monkeypatch.delenv("SAMA_LOG_FORMAT", raising=False)
    monkeypatch.delenv("SAMA_LOG_LEVEL", raising=False)
    configure_logging("api", stream=out)
    yield out
    root.handlers[:] = handlers
    root.setLevel(level)


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_a_record_is_one_json_line_with_the_service(stream: io.StringIO) -> None:
    logging.getLogger("sama.test").info("hello %s", "you", extra={"count": 3})
    [line] = _lines(stream)
    assert line["msg"] == "hello you"
    assert line["level"] == "info"
    assert line["logger"] == "sama.test"
    assert line["service"] == "api"
    assert line["count"] == 3
    assert str(line["ts"]).endswith("+00:00")


def test_the_context_adds_the_run_ids_and_comes_back(stream: io.StringIO) -> None:
    log = logging.getLogger("sama.test")
    with log_context(run_id="r1", org_id=None):
        with log_context(session_id="s1", run_id="r2"):
            log.info("inner")
        log.info("outer")
    log.info("after")
    inner, outer, after = _lines(stream)
    assert (inner["run_id"], inner["session_id"]) == ("r2", "s1")
    assert outer["run_id"] == "r1"
    assert "session_id" not in outer
    assert "org_id" not in outer
    assert "run_id" not in after
    assert current_context() == {}


async def test_a_task_started_in_the_context_keeps_it(stream: io.StringIO) -> None:
    async def child() -> None:
        await asyncio.sleep(0)
        logging.getLogger("sama.test").info("child")

    with log_context(run_id="r9"):
        task = asyncio.create_task(child())
    await task
    [line] = _lines(stream)
    assert line["run_id"] == "r9"


def test_a_field_never_replaces_the_message(stream: io.StringIO) -> None:
    with log_context(msg="evil", level="evil"):
        logging.getLogger("sama.test").warning("real")
    [line] = _lines(stream)
    assert (line["msg"], line["level"]) == ("real", "warning")


def test_an_exception_goes_in_one_line(stream: io.StringIO) -> None:
    try:
        raise ValueError("bad")
    except ValueError:
        logging.getLogger("sama.test").exception("failed")
    [line] = _lines(stream)
    assert "ValueError: bad" in str(line["exc"])


def test_httpx_logs_only_warnings(stream: io.StringIO) -> None:
    logging.getLogger("httpx").info("GET https://api.telegram.org/bot123:secret/getMe")
    assert stream.getvalue() == ""


def test_the_text_format_and_the_level_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, stream: io.StringIO
) -> None:
    out = io.StringIO()
    monkeypatch.setenv("SAMA_LOG_FORMAT", "text")
    monkeypatch.setenv("SAMA_LOG_LEVEL", "warning")
    configure_logging("api", stream=out)
    with log_context(run_id="r1"):
        logging.getLogger("sama.test").info("hidden")
        logging.getLogger("sama.test").warning("shown")
    assert "hidden" not in out.getvalue()
    assert out.getvalue().rstrip().endswith("shown run_id=r1")


def test_secrets_are_redacted_in_the_logs(stream: io.StringIO) -> None:
    from sama.vault.redact import default_redactor

    default_redactor().add("planted-log-secret-1")
    try:
        token = "ghp_" + "z9" * 18
        logging.getLogger("sama.test").warning(
            "key %s and %s", "planted-log-secret-1", token, extra={"url": "planted-log-secret-1"}
        )
    finally:
        default_redactor().forget("planted-log-secret-1")
    text = stream.getvalue()
    assert "planted-log-secret-1" not in text
    assert token not in text
    [line] = _lines(stream)
    assert line["msg"] == "key [REDACTED] and [REDACTED]"
    assert line["url"] == "[REDACTED]"


def test_a_secret_that_json_escapes_is_redacted(stream: io.StringIO) -> None:
    from sama.vault.redact import default_redactor

    secret = 'planted"log\\secret'
    default_redactor().add(secret)
    try:
        logging.getLogger("sama.test").warning("key %s", secret, extra={"field": [secret]})
    finally:
        default_redactor().forget(secret)
    [line] = _lines(stream)
    assert line["msg"] == "key [REDACTED]"
    assert line["field"] == ["[REDACTED]"]
