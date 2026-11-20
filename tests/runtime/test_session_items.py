"""The session items of the OpenAI adapter: the wire models, the reference ``apply``, and the
memory and remote stores (docs/architecture.md, Section 5.3)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from sama.runtime.session_items import (
    MAX_SESSION_ITEMS,
    MAX_WIRE_ITEMS,
    MemorySessionItems,
    RemoteSessionItems,
    SessionItemsAnswer,
    SessionItemsError,
    SessionItemsRequest,
    apply,
    parse_session_items,
)
from tests.support import make_scope


def _item(n: int) -> dict[str, Any]:
    return {"role": "user", "content": f"m{n}"}


def _req(op: str, **fields: Any) -> SessionItemsRequest:
    return SessionItemsRequest.model_validate({"op": op, "conversation": "c-1", **fields})


def test_each_op_takes_only_its_fields() -> None:
    with pytest.raises(ValidationError, match="takes no items"):
        _req("get", items=[_item(1)])
    with pytest.raises(ValidationError, match="only op get"):
        _req("pop", limit=1)
    with pytest.raises(ValidationError, match="trim needs a count"):
        _req("trim")
    with pytest.raises(ValidationError, match="only op trim"):
        _req("get", count=1)
    with pytest.raises(ValidationError):
        _req("get", conversation="has space")
    with pytest.raises(ValidationError):
        _req("drop")
    assert _req("trim", count=0).count == 0


def test_apply_is_a_list_with_a_limit() -> None:
    items: list[dict[str, Any]] = []
    assert apply(items, _req("add", items=[_item(1), _item(2), _item(3)])).count == 3
    assert apply(items, _req("get")).items == [_item(1), _item(2), _item(3)]
    assert apply(items, _req("get", limit=2)).items == [_item(2), _item(3)]
    limit_zero = apply(items, _req("get", limit=0))
    assert limit_zero.items == []
    assert limit_zero.count == 3
    popped = apply(items, _req("pop"))
    assert popped.items == [_item(3)]
    assert popped.count == 2
    assert apply(items, _req("trim", count=1)).count == 1
    assert items == [_item(1)]
    assert apply(items, _req("replace", items=[_item(9)])).count == 1
    assert items == [_item(9)]
    assert apply(items, _req("clear")).count == 0
    assert apply(items, _req("pop")).items == []


def test_apply_refuses_too_many_items() -> None:
    items = [_item(0)] * (MAX_SESSION_ITEMS - 1)
    with pytest.raises(SessionItemsError, match="too many"):
        apply(items, _req("add", items=[_item(1), _item(2)]))
    assert len(items) == MAX_SESSION_ITEMS - 1


def test_parse_refuses_a_long_list_before_the_model() -> None:
    raw = json.dumps({"op": "add", "conversation": "c", "items": [{}] * (MAX_WIRE_ITEMS + 1)})
    with pytest.raises(ValueError, match="too many items"):
        parse_session_items(raw.encode())
    with pytest.raises(ValueError, match="nested too deeply"):
        parse_session_items(b"[" * 100_000 + b"]" * 100_000)
    with pytest.raises(ValidationError):
        parse_session_items(b"{}")
    assert parse_session_items(b'{"op":"pop","conversation":"c"}').op == "pop"


async def test_the_memory_store_keeps_one_list_per_session_and_conversation() -> None:
    store = MemorySessionItems()
    one = make_scope("run-1")
    other = make_scope("run-2").model_copy(update={"session_id": "s-2"})
    await store.call(one, _req("add", items=[_item(1)]))
    await store.call(one, _req("add", conversation="c-2", items=[_item(2)]))
    await store.call(other, _req("add", items=[_item(3)]))
    assert store.items(one.org_id, one.session_id, "c-1") == (_item(1),)
    assert store.items(one.org_id, one.session_id, "c-2") == (_item(2),)
    assert store.items(other.org_id, "s-2", "c-1") == (_item(3),)
    got = await store.call(one, _req("get"))
    got.items[0]["content"] = "changed"
    assert store.items(one.org_id, one.session_id, "c-1") == (_item(1),)


def _transport(status: int, body: object, seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


async def test_the_remote_store_sends_the_run_token_and_no_scope() -> None:
    seen: list[httpx.Request] = []
    answer = SessionItemsAnswer(items=[_item(1)], count=1).model_dump(mode="json")
    store = RemoteSessionItems(
        "http://tools/session-items", "tok-1", transport=_transport(200, answer, seen)
    )
    got = await store.call(make_scope(), _req("get", limit=5))
    assert got.items == [_item(1)]
    assert seen[0].headers["authorization"] == "Bearer tok-1"
    body = json.loads(seen[0].content)
    assert body == {"op": "get", "conversation": "c-1", "limit": 5}
    assert "tok-1" not in repr(store)
    assert "http://tools/session-items" in repr(store)


async def test_the_remote_store_maps_a_conflict_and_raises_other_errors() -> None:
    seen: list[httpx.Request] = []
    conflict = RemoteSessionItems(
        "http://t/s", "tok", transport=_transport(409, {"error": "the run is not live"}, seen)
    )
    with pytest.raises(SessionItemsError, match="the run is not live"):
        await conflict.call(make_scope(), _req("pop"))
    plain = RemoteSessionItems("http://t/s", "tok", transport=_transport(409, [], seen))
    with pytest.raises(SessionItemsError, match="refused"):
        await plain.call(make_scope(), _req("pop"))
    broken = RemoteSessionItems("http://t/s", "tok", transport=_transport(503, {}, seen))
    with pytest.raises(httpx.HTTPStatusError):
        await broken.call(make_scope(), _req("pop"))
