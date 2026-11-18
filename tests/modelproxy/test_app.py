"""The model proxy service (docs/architecture.md, Section 16.6).

The upstream provider is an ``httpx.MockTransport``. The tests call the proxy app in process.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr

from sama.modelproxy import (
    MemoryUsageStore,
    ModelProxy,
    OrgLimits,
    PriceTable,
    ProxySettings,
    TokenCounts,
)
from sama.modelproxy import app as app_module
from sama.modelproxy.app import QUEUE_CHUNKS, UpstreamStreamError
from sama.modelproxy.prices import Provider
from sama.modelproxy.store import CallRecord
from sama.modelproxy.usage import Endpoint
from sama.runtime import Budget
from sama.tenancy.run_token import RunTokenSigner
from tests.conformance.scripted_model import serve
from tests.support import make_scope

KEY = b"k" * 32
ANTHROPIC_KEY = "sk-ant-upstream"
OPENAI_KEY = "sk-openai-upstream"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
MODEL = "claude-sonnet-5-5"


def anthropic_message(output_tokens: int = 10) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "hi"}],
        "usage": {"input_tokens": 100, "output_tokens": output_tokens},
    }


def anthropic_stream() -> bytes:
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 50, "output_tokens": 1}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
        {"type": "message_delta", "usage": {"output_tokens": 7}},
        {"type": "message_stop"},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class Body(httpx.AsyncByteStream):
    """A response body that is not read yet, like a real network response."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._data


def reply(
    status: int = 200,
    *,
    json_body: Any = None,
    content: bytes = b"",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    data = json.dumps(json_body).encode() if json_body is not None else content
    sent = {"content-type": "application/json"} if json_body is not None else {}
    return httpx.Response(status, headers=sent | (headers or {}), stream=Body(data))


@dataclass
class Upstream:
    """The fake provider. ``reply`` makes the response for a request."""

    requests: list[httpx.Request] = field(default_factory=list)
    reply: Callable[[httpx.Request], httpx.Response] = field(
        default=lambda _: reply(json_body=anthropic_message())
    )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.reply(request)

    def body(self, index: int = -1) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.requests[index].content)
        return data


@dataclass
class Harness:
    upstream: Upstream
    store: MemoryUsageStore
    signer: RunTokenSigner
    client: httpx.AsyncClient
    proxy: ModelProxy

    def token(self, budget: Budget | None = None, *, shared: bool = False) -> str:
        """A token for a private session of ``user-1``, or for a shared session."""
        return self.signer.issue(make_scope("run-1", shared=shared), ttl_s=600, budget=budget)

    async def messages(
        self,
        token: str | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        sent = {"x-api-key": token if token is not None else self.token(), **(headers or {})}
        payload = body if body is not None else {"model": MODEL, "max_tokens": 10, "messages": []}
        return await self.client.post("/anthropic/v1/messages", json=payload, headers=sent)


def make_proxy(
    upstream: Upstream, store: MemoryUsageStore, signer: RunTokenSigner, **settings: Any
) -> ModelProxy:
    defaults: dict[str, Any] = {
        "anthropic_url": "https://anthropic.test",
        "openai_url": "https://openai.test",
        "anthropic_key": SecretStr(ANTHROPIC_KEY),
        "openai_key": SecretStr(OPENAI_KEY),
    }
    return ModelProxy(
        signer=signer,
        store=store,
        prices=PriceTable.load(),
        settings=ProxySettings(**(defaults | settings)),
        clock=lambda: NOW,
        transport=httpx.MockTransport(upstream.handler),
    )


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    upstream, store, signer = Upstream(), MemoryUsageStore(), RunTokenSigner(KEY)
    proxy = make_proxy(upstream, store, signer)
    transport = httpx.ASGITransport(app=proxy.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
        yield Harness(upstream, store, signer, client, proxy)
    await proxy.aclose()


def record(cost: str, *, user_id: str | None = None, at: datetime = NOW) -> CallRecord:
    return CallRecord(
        org_id="org-1",
        workspace_id="ws-1",
        session_id="sess-1",
        run_id="other-run",
        user_id=user_id,
        provider=Provider.ANTHROPIC,
        model=MODEL,
        endpoint=Endpoint.MESSAGES,
        status_code=200,
        counts=TokenCounts(),
        cost_usd=Decimal(cost),
        duration_ms=1,
        created_at=at,
    )


# Authentication -------------------------------------------------------------------------------


async def test_a_call_without_a_token_is_refused(harness: Harness) -> None:
    response = await harness.messages(token="")
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"
    assert harness.upstream.requests == []


async def test_a_token_from_another_key_is_refused(harness: Harness) -> None:
    other = RunTokenSigner(b"x" * 32).issue(make_scope("run-1"), ttl_s=600)
    response = await harness.messages(token=other)
    assert response.status_code == 401
    assert harness.upstream.requests == []


async def test_openai_errors_use_the_openai_format(harness: Harness) -> None:
    response = await harness.client.post(
        "/openai/v1/responses", json={"model": "gpt-6-luna", "input": "hi"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_error"


async def test_a_bearer_token_is_accepted(harness: Harness) -> None:
    harness.upstream.reply = lambda _: reply(
        json_body={"usage": {"input_tokens": 5, "output_tokens": 1}}
    )
    response = await harness.client.post(
        "/openai/v1/responses",
        json={"model": "gpt-6-luna", "input": "hi"},
        headers={"authorization": f"Bearer {harness.token()}"},
    )
    assert response.status_code == 200
    assert harness.upstream.requests[0].headers["authorization"] == f"Bearer {OPENAI_KEY}"


# Request checks -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"max_tokens": 1}, "no model"),
        ({"model": "claude-unknown-9"}, "not allowed"),
        ({"model": MODEL, "speed": "fast"}, "speed"),
        ({"model": MODEL, "inference_geo": "us"}, "inference_geo"),
    ],
)
async def test_a_request_that_cannot_be_priced_is_refused(
    harness: Harness, body: dict[str, Any], message: str
) -> None:
    response = await harness.messages(body=body)
    assert response.status_code == 400
    assert message in response.json()["error"]["message"]
    assert harness.upstream.requests == []


@pytest.mark.parametrize("extra", [{"background": True}, {"service_tier": "flex"}])
async def test_openai_options_with_other_prices_are_refused(
    harness: Harness, extra: dict[str, Any]
) -> None:
    response = await harness.client.post(
        "/openai/v1/responses",
        json={"model": "gpt-6-luna", "input": "hi", **extra},
        headers={"authorization": f"Bearer {harness.token()}"},
    )
    assert response.status_code == 400


async def test_a_body_that_is_not_json_is_refused(harness: Harness) -> None:
    response = await harness.client.post(
        "/anthropic/v1/messages", content=b"[1, 2", headers={"x-api-key": harness.token()}
    )
    assert response.status_code == 400


async def test_a_large_body_is_refused() -> None:
    upstream, store, signer = Upstream(), MemoryUsageStore(), RunTokenSigner(KEY)
    proxy = make_proxy(upstream, store, signer, max_request_bytes=100)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy.app()), base_url="http://proxy"
    ) as client:
        response = await client.post(
            "/anthropic/v1/messages",
            json={"model": MODEL, "messages": [{"role": "user", "content": "x" * 200}]},
            headers={"x-api-key": signer.issue(make_scope("run-1"), ttl_s=60)},
        )
    await proxy.aclose()
    assert response.status_code == 413
    assert upstream.requests == []


async def test_other_paths_are_not_found(harness: Harness) -> None:
    response = await harness.client.post(
        "/anthropic/v1/files", json={}, headers={"x-api-key": harness.token()}
    )
    assert response.status_code == 404
    assert (await harness.client.get("/healthz")).json() == {"status": "ok"}


# Forwarding -----------------------------------------------------------------------------------


async def test_the_call_uses_the_provider_key_and_not_the_run_token(harness: Harness) -> None:
    token = harness.token()
    response = await harness.messages(
        token=token,
        headers={"anthropic-version": "2023-06-01", "anthropic-beta": "b1", "cookie": "c=1"},
    )
    assert response.status_code == 200
    assert response.json() == anthropic_message()
    sent = harness.upstream.requests[0]
    assert str(sent.url) == "https://anthropic.test/v1/messages"
    assert sent.headers["x-api-key"] == ANTHROPIC_KEY
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert sent.headers["anthropic-beta"] == "b1"
    assert "cookie" not in sent.headers
    assert token not in str(sent.headers)
    assert token.encode() not in sent.content


async def test_the_call_is_recorded_with_its_cost(harness: Harness) -> None:
    await harness.messages()
    [call] = harness.store.calls
    assert (call.org_id, call.run_id, call.model) == ("org-1", "run-1", MODEL)
    assert call.counts == TokenCounts(input=100, output=10)
    # 100 input at 2 USD and 10 output at 10 USD per million tokens.
    assert call.cost_usd == Decimal("0.0003")
    assert call.status_code == 200


async def test_a_stream_passes_through_unchanged_and_is_recorded(harness: Harness) -> None:
    data = anthropic_stream()
    harness.upstream.reply = lambda _: reply(
        content=data, headers={"content-type": "text/event-stream"}
    )
    response = await harness.messages(body={"model": MODEL, "stream": True, "messages": []})
    assert response.content == data
    assert response.headers["content-type"] == "text/event-stream"
    [call] = harness.store.calls
    assert call.counts == TokenCounts(input=50, output=7)


async def test_a_chat_completions_stream_asks_for_the_usage(harness: Harness) -> None:
    harness.upstream.reply = lambda _: reply(
        content=b'data: {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 2}}\n\n'
        b"data: [DONE]\n\n",
    )
    response = await harness.client.post(
        "/openai/v1/chat/completions",
        json={"model": "gpt-6-luna", "stream": True, "stream_options": {"x": 1}, "messages": []},
        headers={"authorization": f"Bearer {harness.token()}"},
    )
    assert response.status_code == 200
    assert harness.upstream.body()["stream_options"] == {"x": 1, "include_usage": True}
    assert harness.store.calls[0].counts == TokenCounts(input=4, output=2)


async def test_an_upstream_error_passes_through_and_is_recorded(harness: Harness) -> None:
    error = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
    harness.upstream.reply = lambda _: reply(
        429, json_body=error, headers={"retry-after": "3", "set-cookie": "a=b"}
    )
    response = await harness.messages()
    assert response.status_code == 429
    assert response.json() == error
    assert response.headers["retry-after"] == "3"
    assert "set-cookie" not in response.headers
    [call] = harness.store.calls
    assert (call.status_code, call.cost_usd) == (429, Decimal(0))


async def test_an_unreachable_provider_gives_502(harness: Harness) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    harness.upstream.reply = fail
    response = await harness.messages()
    assert response.status_code == 502
    assert harness.store.calls == []


async def test_a_provider_without_a_key_gives_503() -> None:
    upstream, store, signer = Upstream(), MemoryUsageStore(), RunTokenSigner(KEY)
    proxy = make_proxy(upstream, store, signer, openai_key=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy.app()), base_url="http://proxy"
    ) as client:
        response = await client.post(
            "/openai/v1/responses",
            json={"model": "gpt-6-luna", "input": "hi"},
            headers={"authorization": f"Bearer {signer.issue(make_scope('r'), ttl_s=60)}"},
        )
    await proxy.aclose()
    assert response.status_code == 503
    assert upstream.requests == []


async def test_count_tokens_is_not_recorded(harness: Harness) -> None:
    harness.upstream.reply = lambda _: reply(json_body={"input_tokens": 12})
    harness.store.limits["org-1"] = OrgLimits(model_usd_per_month=Decimal(0))
    response = await harness.client.post(
        "/anthropic/v1/messages/count_tokens",
        json={"model": MODEL, "messages": []},
        headers={"x-api-key": harness.token()},
    )
    assert response.json() == {"input_tokens": 12}
    assert harness.store.calls == []


# Budgets --------------------------------------------------------------------------------------


async def test_the_run_cost_budget_refuses_the_next_call(harness: Harness) -> None:
    token = harness.token(Budget(max_cost_usd=Decimal("0.0005")))
    assert (await harness.messages(token=token)).status_code == 200  # 0.0003 USD
    assert (await harness.messages(token=token)).status_code == 200  # 0.0006 USD in total
    refused = await harness.messages(token=token)
    assert refused.status_code == 402
    assert refused.json()["error"] == {
        "type": "budget_exceeded",
        "message": "the run reached max_cost_usd",
    }
    assert len(harness.upstream.requests) == 2


async def test_the_run_token_budget_refuses_the_next_call(harness: Harness) -> None:
    token = harness.token(Budget(max_tokens=110))
    assert (await harness.messages(token=token)).status_code == 200  # 110 tokens
    refused = await harness.messages(token=token)
    assert refused.status_code == 402
    assert "max_tokens" in refused.json()["error"]["message"]


async def test_the_org_monthly_limit(harness: Harness) -> None:
    harness.store.limits["org-1"] = OrgLimits(model_usd_per_month=Decimal("1"))
    harness.store.calls.append(record("0.6", at=NOW - timedelta(days=30)))  # last month
    harness.store.calls.append(record("0.6"))
    assert (await harness.messages()).status_code == 200
    harness.store.calls.append(record("0.4"))
    assert (await harness.messages()).status_code == 402


async def test_the_user_daily_limit_applies_only_to_that_user(harness: Harness) -> None:
    harness.store.limits["org-1"] = OrgLimits(model_usd_per_user_day=Decimal("1"))
    harness.store.calls.append(record("1", user_id="user-1"))
    harness.store.calls.append(record("5", user_id="user-1", at=NOW - timedelta(days=1)))
    refused = await harness.messages(token=harness.token())
    assert refused.status_code == 402
    assert "daily" in refused.json()["error"]["message"]
    # A shared session has no user in its token.
    assert (await harness.messages(token=harness.token(shared=True))).status_code == 200


# Client disconnect ----------------------------------------------------------------------------


async def read_one_chunk(
    proxy: ModelProxy, store: MemoryUsageStore, path: str, body: dict[str, Any], token: str
) -> CallRecord:
    """Runs the real server, reads one chunk, and closes the connection. Returns the record."""
    with serve(proxy.app()) as url:
        async with (
            httpx.AsyncClient(base_url=url) as client,
            client.stream("POST", path, json=body, headers={"x-api-key": token}) as response,
        ):
            async for _ in response.aiter_bytes():
                break
        async with asyncio.timeout(5):
            while not store.calls:  # noqa: ASYNC110 - the server thread writes the record
                await asyncio.sleep(0.05)
    await proxy.aclose()
    return store.calls[0]


def events(*items: dict[str, Any]) -> list[bytes]:
    return [f"data: {json.dumps(item)}\n\n".encode() for item in items]


async def test_the_proxy_reads_the_rest_of_the_stream_when_the_client_stops() -> None:
    """OpenAI sends the usage only at the end. A client that stops early still pays."""
    first, last = events(
        {"type": "response.output_text.delta", "delta": "hi"},
        {
            "type": "response.completed",
            "response": {"usage": {"input_tokens": 40, "output_tokens": 900}},
        },
    )

    async def stream() -> AsyncIterator[bytes]:
        yield first
        await asyncio.sleep(0.5)  # the client is gone now
        yield last

    upstream, store, signer = Upstream(), MemoryUsageStore(), RunTokenSigner(KEY)
    upstream.reply = lambda _: httpx.Response(200, content=stream())
    proxy = make_proxy(upstream, store, signer)
    token = signer.issue(make_scope("run-1"), ttl_s=60)
    body = {"model": "gpt-6-luna", "stream": True, "input": "hi"}
    call = await read_one_chunk(proxy, store, "/openai/v1/responses", body, token)
    assert call.counts == TokenCounts(input=40, output=900)
    assert not call.usage_estimated


async def test_a_stream_with_no_final_usage_is_charged_a_high_estimate() -> None:
    async def stream() -> AsyncIterator[bytes]:
        yield anthropic_stream().split(b"\n\n")[0] + b"\n\n"  # message_start only
        await asyncio.sleep(30)
        yield b""

    upstream, store, signer = Upstream(), MemoryUsageStore(), RunTokenSigner(KEY)
    upstream.reply = lambda _: httpx.Response(200, content=stream())
    proxy = make_proxy(upstream, store, signer, drain_s=0.3)
    token = signer.issue(make_scope("run-1"), ttl_s=60, budget=Budget(max_cost_usd=Decimal("0.01")))
    body = {"model": MODEL, "stream": True, "max_tokens": 2000, "messages": []}
    call = await read_one_chunk(proxy, store, "/anthropic/v1/messages", body, token)
    assert call.usage_estimated
    assert call.counts.input == 50  # the seen input is higher than the size estimate
    assert call.counts.output == 2000
    # 50 input at 2 USD and 2,000 output at 10 USD per million tokens.
    assert call.cost_usd == Decimal("0.0201")
    # The estimate counts against the budget.
    proxy = make_proxy(upstream, store, signer)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy.app()), base_url="http://p"
    ) as client:
        refused = await client.post(
            "/anthropic/v1/messages", json=body, headers={"x-api-key": token}
        )
    await proxy.aclose()
    assert refused.status_code == 402


class FailingStore(MemoryUsageStore):
    def __init__(self) -> None:
        super().__init__()
        self.broken = True
        self.reads_fail = False

    async def record(self, call: CallRecord) -> None:
        if self.broken:
            raise RuntimeError("the database is down")
        await super().record(call)

    async def org_limits(self, org_id: str) -> OrgLimits:
        if self.reads_fail:
            raise RuntimeError("the database is down")
        return await super().org_limits(org_id)


async def test_a_record_that_cannot_be_written_blocks_the_org() -> None:
    upstream, store, signer = Upstream(), FailingStore(), RunTokenSigner(KEY)
    proxy = make_proxy(upstream, store, signer, record_retry_s=0.0)
    token = signer.issue(make_scope("run-1"), ttl_s=60)
    other = signer.issue(make_scope("run-2").model_copy(update={"org_id": "org-2"}), ttl_s=60)
    payload = {"model": MODEL, "max_tokens": 10, "messages": []}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy.app()), base_url="http://p"
    ) as client:

        async def call(key: str) -> httpx.Response:
            return await client.post(
                "/anthropic/v1/messages", json=payload, headers={"x-api-key": key}
            )

        assert (await call(token)).status_code == 200  # the response is already paid for
        assert len(proxy.unrecorded) == 1
        blocked = await call(token)
        assert blocked.status_code == 503
        assert len(upstream.requests) == 1
        store.broken = False
        assert (await call(other)).status_code == 200  # flushes the pending record
        assert proxy.unrecorded == []
        assert (await call(token)).status_code == 200
    await proxy.aclose()
    assert [c.run_id for c in store.calls] == ["run-1", "run-2", "run-1"]


async def test_a_store_that_cannot_read_refuses_the_call() -> None:
    upstream, store, signer = Upstream(), FailingStore(), RunTokenSigner(KEY)
    store.broken, store.reads_fail = False, True
    proxy = make_proxy(upstream, store, signer)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy.app()), base_url="http://p"
    ) as client:
        response = await client.post(
            "/anthropic/v1/messages",
            json={"model": MODEL, "messages": []},
            headers={"x-api-key": signer.issue(make_scope("run-1"), ttl_s=60)},
        )
    await proxy.aclose()
    assert response.status_code == 503
    assert upstream.requests == []


async def test_openai_calls_use_the_standard_tier(harness: Harness) -> None:
    harness.upstream.reply = lambda _: reply(json_body={"usage": {"input_tokens": 1}})
    for extra in ({}, {"service_tier": "auto"}):
        response = await harness.client.post(
            "/openai/v1/responses",
            json={"model": "gpt-6-luna", "input": "hi", **extra},
            headers={"authorization": f"Bearer {harness.token()}"},
        )
        assert response.status_code == 200
        assert harness.upstream.body()["service_tier"] == "default"


async def test_a_broken_provider_stream_is_an_error_for_the_client() -> None:
    async def stream() -> AsyncIterator[bytes]:
        yield anthropic_stream().split(b"\n\n")[0] + b"\n\n"
        raise httpx.ReadError("the connection broke")

    upstream, store, signer = Upstream(), MemoryUsageStore(), RunTokenSigner(KEY)
    upstream.reply = lambda _: httpx.Response(200, content=stream())
    proxy = make_proxy(upstream, store, signer)
    transport = httpx.ASGITransport(app=proxy.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://p") as client:
        with pytest.raises(UpstreamStreamError):
            await client.post(
                "/anthropic/v1/messages",
                json={"model": MODEL, "stream": True, "max_tokens": 100, "messages": []},
                headers={"x-api-key": signer.issue(make_scope("run-1"), ttl_s=60)},
            )
    await proxy.aclose()
    [call] = store.calls
    assert call.usage_estimated
    assert call.counts.output == 100


def asgi_call(
    signer: RunTokenSigner, payload: dict[str, Any]
) -> tuple[dict[str, Any], Callable[[], Awaitable[dict[str, Any]]]]:
    """An ASGI scope and a receive function for one Anthropic call with no disconnect."""
    token = signer.issue(make_scope("run-1"), ttl_s=60)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/anthropic/v1/messages",
        "raw_path": b"/anthropic/v1/messages",
        "query_string": b"",
        "headers": [(b"x-api-key", token.encode()), (b"content-type", b"application/json")],
        "server": ("proxy", 80),
        "client": ("test", 1),
    }
    messages = [{"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}]

    async def receive() -> dict[str, Any]:
        if messages:
            return messages.pop(0)
        await asyncio.Event().wait()
        return {}

    return scope, receive


async def test_a_slow_client_holds_only_a_few_chunks() -> None:
    """The ASGI app is driven directly, with a client that does not read for a while."""
    sent = 0

    async def stream() -> AsyncIterator[bytes]:
        nonlocal sent
        for _ in range(QUEUE_CHUNKS * 4):
            sent += 1
            yield b": keep-alive\n\n"
        yield anthropic_stream()

    upstream, store, signer = Upstream(), MemoryUsageStore(), RunTokenSigner(KEY)
    upstream.reply = lambda _: httpx.Response(200, content=stream())
    proxy = make_proxy(upstream, store, signer)
    app = proxy.app()
    scope, receive = asgi_call(signer, {"model": MODEL, "stream": True, "messages": []})
    reading = asyncio.Event()
    received: list[bytes] = []

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.body":
            await reading.wait()
            received.append(message.get("body", b""))

    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.sleep(0.2)  # the client does not read; the pump must wait
    assert sent <= QUEUE_CHUNKS + 3
    reading.set()
    async with asyncio.timeout(5):
        await task
    await proxy.aclose()
    assert sent == QUEUE_CHUNKS * 4
    assert b"".join(received).endswith(anthropic_stream())
    assert store.calls[0].counts == TokenCounts(input=50, output=7)


async def test_a_retried_record_is_counted_once() -> None:
    class CommitThenFail(MemoryUsageStore):
        """The first write commits, but the answer is lost."""

        def __init__(self) -> None:
            super().__init__()
            self.writes = 0

        async def record(self, call: CallRecord) -> None:
            self.writes += 1
            await super().record(call)
            if self.writes == 1:
                raise ConnectionError("the answer was lost")

    upstream, store, signer = Upstream(), CommitThenFail(), RunTokenSigner(KEY)
    proxy = make_proxy(upstream, store, signer, record_retry_s=0.0)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy.app()), base_url="http://p"
    ) as client:
        response = await client.post(
            "/anthropic/v1/messages",
            json={"model": MODEL, "messages": []},
            headers={"x-api-key": signer.issue(make_scope("run-1"), ttl_s=60)},
        )
    await proxy.aclose()
    assert response.status_code == 200
    assert store.writes == 2
    assert len(store.calls) == 1


async def test_the_fence_holds_while_a_flush_writes() -> None:
    """A record stays in the fence until its write succeeds, also during the write."""
    gate = asyncio.Event()

    class SlowStore(FailingStore):
        async def record(self, call: CallRecord) -> None:
            if not self.broken:
                await gate.wait()
            await super().record(call)

    upstream, store, signer = Upstream(), SlowStore(), RunTokenSigner(KEY)
    proxy = make_proxy(upstream, store, signer, record_retry_s=0.0)
    token = signer.issue(make_scope("run-1"), ttl_s=60)
    payload = {"model": MODEL, "messages": []}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy.app()), base_url="http://p"
    ) as client:

        async def call() -> httpx.Response:
            return await client.post(
                "/anthropic/v1/messages", json=payload, headers={"x-api-key": token}
            )

        assert (await call()).status_code == 200
        assert len(proxy.unrecorded) == 1
        store.broken = False
        first = asyncio.create_task(call())  # starts a flush that waits on the gate
        await asyncio.sleep(0.05)
        assert len(proxy.unrecorded) == 1
        second = asyncio.create_task(call())
        await asyncio.sleep(0.05)
        assert not second.done()  # it waits for the flush; it does not pass the fence
        gate.set()
        assert (await first).status_code == 200
        assert (await second).status_code == 200
    await proxy.aclose()
    assert len(upstream.requests) == 3


async def test_close_stops_open_streams_and_records_them() -> None:
    async def stream() -> AsyncIterator[bytes]:
        yield anthropic_stream().split(b"\n\n")[0] + b"\n\n"
        await asyncio.sleep(60)
        yield b""

    upstream, store, signer = Upstream(), MemoryUsageStore(), RunTokenSigner(KEY)
    upstream.reply = lambda _: httpx.Response(200, content=stream())
    proxy = make_proxy(upstream, store, signer)
    transport = httpx.ASGITransport(app=proxy.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://p") as client:

        async def read() -> None:
            async with client.stream(
                "POST",
                "/anthropic/v1/messages",
                json={"model": MODEL, "stream": True, "max_tokens": 300, "messages": []},
                headers={"x-api-key": signer.issue(make_scope("run-1"), ttl_s=60)},
            ) as response:
                async for _ in response.aiter_raw():
                    pass

        reader = asyncio.create_task(read())
        await asyncio.sleep(0.1)
        with patch.object(app_module, "CLOSE_WAIT_S", 0.1):
            async with asyncio.timeout(5):
                await proxy.aclose()
        with pytest.raises(UpstreamStreamError):
            await reader
    [call] = store.calls
    assert call.usage_estimated
    assert call.counts.output == 300


async def test_a_client_that_goes_before_the_body_still_pays() -> None:
    """The client goes while the proxy sends the headers. The pump still records the call."""
    first, last = events(
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
        {"type": "message_delta", "usage": {"output_tokens": 70}},
    )

    async def stream() -> AsyncIterator[bytes]:
        for _ in range(QUEUE_CHUNKS * 2):
            yield first
        yield last

    upstream, store, signer = Upstream(), MemoryUsageStore(), RunTokenSigner(KEY)
    upstream.reply = lambda _: httpx.Response(200, content=stream())
    proxy = make_proxy(upstream, store, signer)
    scope, receive = asgi_call(signer, {"model": MODEL, "stream": True, "messages": []})

    async def send(message: MutableMapping[str, Any]) -> None:
        raise OSError("the client is gone")

    with contextlib.suppress(Exception):
        await proxy.app()(scope, receive, send)
    async with asyncio.timeout(5):
        while not store.calls:  # noqa: ASYNC110 - the pump task writes the record
            await asyncio.sleep(0.02)
    await proxy.aclose()
    [call] = store.calls
    assert not call.usage_estimated
    assert call.counts.output == 70


async def test_close_ends_with_a_full_queue_and_a_stuck_store() -> None:
    """The client does not read, the store hangs. Close still ends, and the record waits in
    the fence and in the log."""
    hang = asyncio.Event()

    class StuckStore(MemoryUsageStore):
        async def record(self, call: CallRecord) -> None:
            await hang.wait()

    async def stream() -> AsyncIterator[bytes]:
        for _ in range(QUEUE_CHUNKS * 3):
            yield b": keep-alive\n\n"
        await asyncio.sleep(60)
        yield b""

    upstream, store, signer = Upstream(), StuckStore(), RunTokenSigner(KEY)
    upstream.reply = lambda _: httpx.Response(200, content=stream())
    proxy = make_proxy(upstream, store, signer)
    scope, receive = asgi_call(signer, {"model": MODEL, "stream": True, "messages": []})

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.body":
            await asyncio.Event().wait()  # the client never reads

    server = asyncio.create_task(proxy.app()(scope, receive, send))
    await asyncio.sleep(0.2)
    with (
        patch.object(app_module, "CLOSE_WAIT_S", 0.1),
        patch.object(app_module, "STOP_WAIT_S", 0.3),
    ):
        async with asyncio.timeout(5):
            await proxy.aclose()
    server.cancel()
    await asyncio.gather(server, return_exceptions=True)
    [call] = proxy.unrecorded
    assert call.usage_estimated
