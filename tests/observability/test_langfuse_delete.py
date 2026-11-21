"""The trace delete of the Langfuse client (docs/architecture.md, Section 17.3)."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from sama.observability.langfuse import MAX_DELETE_IDS, LangfuseClient, LangfuseError


def _client(handler: httpx.MockTransport) -> LangfuseClient:
    return LangfuseClient("https://lf.example.test/", "pk", SecretStr("sk"), transport=handler)


async def test_the_delete_sends_unique_ids_in_batches() -> None:
    bodies: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url == "https://lf.example.test/api/public/traces"
        assert request.headers["authorization"].startswith("Basic ")
        bodies.append(json.loads(request.content)["traceIds"])
        return httpx.Response(200, json={"message": "ok"})

    client = _client(httpx.MockTransport(handler))
    ids = [f"{n:032x}" for n in range(MAX_DELETE_IDS + 3)]
    await client.delete_traces([*ids, ids[0]])
    await client.delete_traces([])
    await client.aclose()

    assert [len(body) for body in bodies] == [MAX_DELETE_IDS, 3]
    assert sorted(bodies[0] + bodies[1]) == sorted(ids)


@pytest.mark.parametrize("status", [401, 500])
async def test_a_failed_delete_raises_a_retry_error(status: int) -> None:
    client = _client(httpx.MockTransport(lambda request: httpx.Response(status)))
    with pytest.raises(LangfuseError) as raised:
        await client.delete_traces(["a" * 32])
    await client.aclose()
    assert raised.value.retry
    assert str(status) in str(raised.value)


async def test_a_delete_without_an_answer_raises_a_retry_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no answer", request=request)

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(LangfuseError, match="ConnectError") as raised:
        await client.delete_traces(["a" * 32])
    await client.aclose()
    assert raised.value.retry
