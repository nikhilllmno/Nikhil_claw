"""The senders of the deliveries: each answer becomes ``sent``, ``failed``, or ``unknown``
(Section 12.1). An unknown result is never sent again, so a message does not go out twice."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from sama.scheduler.delivery import EmailConfig, Sender
from sama.vault.crypto import VaultError
from sama.vault.store import SecretName

ORG = uuid.uuid4()


def sender(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    tokens: bool = True,
    email: bool = True,
) -> tuple[Sender, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    async def secret(org_id: uuid.UUID, name: SecretName) -> SecretStr | None:
        assert org_id == ORG
        return SecretStr(f"token-{name.value}") if tokens else None

    return (
        Sender(
            secret,
            email=EmailConfig(SecretStr("sg-key"), "jobs@example.test") if email else None,
            http=httpx.AsyncClient(transport=httpx.MockTransport(record)),
            telegram_url="https://tg.test",
            slack_url="https://slack.test/api",
            sendgrid_url="https://sg.test/send",
        ),
        seen,
    )


async def test_telegram_ok_gives_a_receipt() -> None:
    send, seen = sender(
        lambda _: httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})
    )
    result = await send.telegram(ORG, "-100", "hello")
    assert (result.outcome, result.receipt) == ("sent", "telegram:-100:42")
    assert seen[0].url.path.endswith("/sendMessage")
    assert json.loads(seen[0].content) == {"chat_id": "-100", "text": "hello"}


@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (httpx.Response(400, json={"ok": False, "description": "chat not found"}), "failed"),
        (httpx.Response(502, text="bad gateway"), "unknown"),
        (httpx.Response(200, text="<html>"), "unknown"),
    ],
)
async def test_telegram_answers(response: httpx.Response, outcome: str) -> None:
    send, _ = sender(lambda _: response)
    assert (await send.telegram(ORG, "1", "x")).outcome == outcome


async def test_slack_sends_with_the_bot_token() -> None:
    send, seen = sender(
        lambda _: httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "1.2"})
    )
    result = await send.slack(ORG, "C1", "hi", "0.1")
    assert (result.outcome, result.receipt) == ("sent", "slack:C1:1.2")
    assert seen[0].headers["authorization"] == "Bearer token-slack_bot_token"
    assert json.loads(seen[0].content)["thread_ts"] == "0.1"


async def test_slack_refusal_is_a_failure() -> None:
    send, _ = sender(lambda _: httpx.Response(200, json={"ok": False, "error": "not_in_channel"}))
    result = await send.slack(ORG, "C1", "hi")
    assert result.outcome == "failed"
    assert result.error is not None
    assert "not_in_channel" in result.error


async def test_no_token_fails_before_a_request() -> None:
    send, seen = sender(lambda _: httpx.Response(200), tokens=False)
    assert (await send.telegram(ORG, "1", "x")).outcome == "failed"
    assert (await send.slack(ORG, "C1", "x")).outcome == "failed"
    assert seen == []


async def test_a_vault_error_is_a_failure_and_not_a_crash() -> None:
    async def broken(org_id: uuid.UUID, name: SecretName) -> SecretStr | None:
        raise VaultError("the entry does not decrypt")

    send = Sender(
        broken,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
    )
    assert (await send.telegram(ORG, "1", "x")).outcome == "failed"


async def test_a_connect_error_is_a_failure_and_a_read_timeout_is_unknown() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    send, _ = sender(refuse)
    assert (await send.telegram(ORG, "1", "x")).outcome == "failed"
    send, _ = sender(slow)
    assert (await send.telegram(ORG, "1", "x")).outcome == "unknown"


async def test_email_goes_through_sendgrid() -> None:
    send, seen = sender(lambda _: httpx.Response(202, headers={"x-message-id": "m-1"}))
    result = await send.email("ana@example.test", "sama: report", "body")
    assert (result.outcome, result.receipt) == ("sent", "m-1")
    body = json.loads(seen[0].content)
    assert body["personalizations"] == [{"to": [{"email": "ana@example.test"}]}]
    assert body["from"] == {"email": "jobs@example.test"}
    assert seen[0].headers["authorization"] == "Bearer sg-key"


async def test_email_outcomes() -> None:
    send, _ = sender(lambda _: httpx.Response(503))
    assert (await send.email("a@b.test", "s", "t")).outcome == "unknown"
    send, _ = sender(lambda _: httpx.Response(400))
    assert (await send.email("a@b.test", "s", "t")).outcome == "failed"
    send, seen = sender(lambda _: httpx.Response(202), email=False)
    result = await send.email("a@b.test", "s", "t")
    assert result.outcome == "failed"
    assert seen == []
