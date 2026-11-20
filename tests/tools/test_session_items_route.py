"""The ``POST /session-items`` route of the tool server (docs/architecture.md, Section 5.3)."""

from __future__ import annotations

import secrets
from typing import Any

import httpx
import pytest

from sama.runtime.session_items import (
    MAX_SESSION_ITEMS_BODY_BYTES,
    MemorySessionItems,
    SessionItemsAnswer,
    SessionItemsError,
    SessionItemsRequest,
)
from sama.tenancy import RunTokenClaims, RunTokenSigner
from sama.tools import MemoryTodoStore, ToolServer
from tests.conformance.scripted_model import serve
from tests.support import make_scope


class FakeItems:
    """A backend over ``MemorySessionItems``. It records the claims of each call."""

    def __init__(self, error: Exception | None = None, call_error: Exception | None = None):
        self.error = error
        self.call_error = call_error
        self.memory = MemorySessionItems()
        self.calls: list[str] = []

    async def authorize(self, claims: RunTokenClaims) -> None:
        if self.error is not None:
            raise self.error

    async def call(
        self, claims: RunTokenClaims, request: SessionItemsRequest
    ) -> SessionItemsAnswer:
        self.calls.append(claims.session_id)
        if self.call_error is not None:
            raise self.call_error
        scope = make_scope(claims.run_id).model_copy(update={"session_id": claims.session_id})
        return await self.memory.call(scope, request)


def rig(items: Any) -> tuple[ToolServer, RunTokenSigner]:
    signer = RunTokenSigner(secrets.token_bytes(32))
    return ToolServer(signer=signer, todos=MemoryTodoStore(), session_items=items), signer


async def post(base: str, token: str | None, body: Any = None, raw: bytes | None = None) -> Any:
    headers = {"content-type": "application/json"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient() as http:
        if raw is not None:
            return await http.post(base + "/session-items", headers=headers, content=raw)
        return await http.post(base + "/session-items", headers=headers, json=body)


ADD = {"op": "add", "conversation": "c-1", "items": [{"role": "user", "content": "hi"}]}
GET = {"op": "get", "conversation": "c-1"}


async def test_the_route_runs_operations_on_the_session_of_the_token() -> None:
    items = FakeItems()
    tools, signer = rig(items)
    token = signer.issue(make_scope("run-7"), ttl_s=600)
    with serve(tools.app()) as base:
        assert (await post(base, token, ADD)).json() == {"items": [], "count": 1}
        response = await post(base, token, GET)
    assert response.status_code == 200
    assert response.json() == {"items": [{"role": "user", "content": "hi"}], "count": 1}
    assert items.calls == ["sess-1", "sess-1"]


@pytest.mark.parametrize("token", [None, "nope"])
async def test_the_route_needs_a_run_token(token: str | None) -> None:
    items = FakeItems()
    tools, _signer = rig(items)
    with serve(tools.app()) as base:
        assert (await post(base, token, ADD)).status_code == 401
    assert items.calls == []


async def test_the_route_answers_503_without_a_store() -> None:
    tools, signer = rig(None)
    with serve(tools.app()) as base:
        assert (await post(base, signer.issue(make_scope(), ttl_s=600), GET)).status_code == 503


@pytest.mark.parametrize(
    "body",
    [
        {"op": "drop", "conversation": "c"},
        {"op": "get", "conversation": "bad name"},
        {"op": "get", "conversation": "c", "items": [{}]},
        {"op": "get", "conversation": "c", "extra": 1},
        [1],
    ],
)
async def test_a_bad_request_gets_422(body: Any) -> None:
    items = FakeItems()
    tools, signer = rig(items)
    with serve(tools.app()) as base:
        response = await post(base, signer.issue(make_scope(), ttl_s=600), body)
    assert response.status_code == 422
    assert items.calls == []


async def test_a_large_request_gets_413() -> None:
    tools, signer = rig(FakeItems())
    raw = b'{"pad": "' + b"x" * MAX_SESSION_ITEMS_BODY_BYTES + b'"}'
    with serve(tools.app()) as base:
        response = await post(base, signer.issue(make_scope(), ttl_s=600), raw=raw)
    assert response.status_code == 413


@pytest.mark.parametrize("where", ["authorize", "call"])
async def test_a_store_error_is_409_and_a_crash_is_503_without_details(where: str) -> None:
    def backend(error: Exception) -> FakeItems:
        return FakeItems(error=error) if where == "authorize" else FakeItems(call_error=error)

    tools, signer = rig(backend(SessionItemsError("the run is not running")))
    with serve(tools.app()) as base:
        response = await post(base, signer.issue(make_scope(), ttl_s=600), GET)
    assert response.status_code == 409
    assert response.json() == {"error": "the run is not running"}
    tools, signer = rig(backend(RuntimeError("database password is hunter2")))
    with serve(tools.app()) as base:
        response = await post(base, signer.issue(make_scope(), ttl_s=600), GET)
    assert response.status_code == 503
    assert "hunter2" not in response.text
