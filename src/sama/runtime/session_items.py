"""The conversation items of an OpenAI Agents SDK session (docs/architecture.md, Section 5.3).

The OpenAI adapter keeps the conversation of a sama session as a list of Responses API items
(JSON objects). The runner in the sandbox has no database credentials, so
``RemoteSessionItems`` sends each operation to the sama tool server
(``POST /session-items``) with the run token. The tool server finds the session from the
claims of the run token, so a runner can read and write only the items of its own session.

This module has no SDK import and no database import. ``MemorySessionItems`` keeps the items
in memory, for tests and for a runtime in the test process.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any, Final, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from sama.runtime.contract import RunScope

SESSION_ITEMS_PATH: Final = "/session-items"
MAX_SESSION_ITEMS_BODY_BYTES: Final = 8_000_000
"""The body limit of ``POST /session-items``. The tool server refuses a larger body at once."""
MAX_WIRE_ITEMS: Final = 5_000
"""The most items of one request or one answer."""
MAX_SESSION_ITEMS: Final = 20_000
"""The most items of one session. The store refuses an ``add`` over this limit. A compaction
replaces the items with a few items."""
MAX_SESSION_ITEMS_BYTES: Final = 16_000_000
"""The most bytes of the items of one conversation (``item_bytes``). The store refuses an
``add`` or a ``replace`` over this limit, so a ``get`` answer is also at most this size."""
SESSION_ITEMS_TIMEOUT_S: Final = 30.0
CONVERSATION_PATTERN: Final = r"^[A-Za-z0-9_.:-]{1,100}$"

Op = Literal["get", "add", "pop", "clear", "replace", "trim"]


class SessionItemsRequest(BaseModel):
    """The body of ``POST /session-items``.

    - ``get``: the last ``limit`` items (all items without a limit), oldest first.
    - ``add``: append ``items``.
    - ``pop``: remove the last item and return it.
    - ``clear``: remove all items.
    - ``replace``: remove all items, then append ``items`` (a compaction).
    - ``trim``: remove the items after the first ``count`` items. The adapter uses it to drop
      the items of a run that did not complete.

    ``conversation`` names one list of items in the session. The adapter starts a new
    conversation when a run has no ``runtime_state`` (the first chat run, or a review run), so
    such a run never changes the items of the chat.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: Op
    conversation: str = Field(pattern=CONVERSATION_PATTERN)
    items: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_WIRE_ITEMS)
    limit: int | None = Field(default=None, ge=0, le=MAX_SESSION_ITEMS)
    count: int | None = Field(default=None, ge=0, le=MAX_SESSION_ITEMS)

    @model_validator(mode="after")
    def _fields_match_op(self) -> SessionItemsRequest:
        if self.items and self.op not in {"add", "replace"}:
            raise ValueError(f"op {self.op} takes no items")
        if self.limit is not None and self.op != "get":
            raise ValueError("only op get takes a limit")
        if (self.count is None) != (self.op != "trim"):
            raise ValueError("op trim needs a count, and only op trim takes one")
        return self


class SessionItemsAnswer(BaseModel):
    """The answer of ``POST /session-items``. ``items`` holds the items of ``get`` and the
    item of ``pop``. ``count`` is the number of items of the session after the operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_SESSION_ITEMS)
    count: int = Field(ge=0)


class SessionItemsError(Exception):
    """The store refused an operation (for example, the session has too many items)."""


def parse_session_items(raw: bytes) -> SessionItemsRequest:
    """The model of a request body. Raises ``ValueError`` (a ``ValidationError`` too) for a
    body that is not valid. The list is counted before the model builds the items, as in
    ``sama.runtime.archive.parse_archive``."""
    seen: list[list[tuple[str, Any]]] = []

    def hook(pairs: list[tuple[str, Any]]) -> int:
        seen[:] = [pairs]
        return 0

    try:
        json.loads(raw, object_pairs_hook=hook)
    except RecursionError as error:
        raise ValueError("the body is nested too deeply") from error
    for key, value in seen[0] if seen else ():
        if key == "items" and isinstance(value, list) and len(value) > MAX_WIRE_ITEMS:
            raise ValueError("the body has too many items")
    return SessionItemsRequest.model_validate_json(raw)


class SessionItems(Protocol):
    """The item store of the OpenAI adapter, for the session of one run scope."""

    async def call(self, scope: RunScope, request: SessionItemsRequest) -> SessionItemsAnswer: ...


def item_bytes(items: Sequence[dict[str, Any]]) -> int:
    """The size of items as the memory store counts it: the UTF-8 bytes of the JSON text of
    each item. The Postgres store also checks the ``jsonb`` text after a write, because a
    number such as ``1e308`` is longer in it."""
    return sum(len(json.dumps(item, ensure_ascii=False).encode()) for item in items)


def check_size(stored: int, added: Sequence[dict[str, Any]]) -> None:
    """Raises ``SessionItemsError`` if the items go over ``MAX_SESSION_ITEMS_BYTES``."""
    if stored + item_bytes(added) > MAX_SESSION_ITEMS_BYTES:
        raise SessionItemsError("the session items are too large")


def apply(items: list[dict[str, Any]], request: SessionItemsRequest) -> SessionItemsAnswer:
    """Applies one operation to a list of items in place. The memory store and the tests of a
    database store use it as the reference."""
    out: list[dict[str, Any]] = []
    if request.op == "get":
        if request.limit is None:
            out = list(items)
        elif request.limit > 0:
            out = list(items[-request.limit :])
    elif request.op == "add":
        if len(items) + len(request.items) > MAX_SESSION_ITEMS:
            raise SessionItemsError("the session has too many items")
        check_size(item_bytes(items), request.items)
        items.extend(request.items)
    elif request.op == "pop":
        if items:
            out = [items.pop()]
    elif request.op == "clear":
        items.clear()
    elif request.op == "replace":
        check_size(0, request.items)
        items[:] = list(request.items)
    else:
        assert request.count is not None  # noqa: S101 -- the model checks it
        del items[request.count :]
    return SessionItemsAnswer(items=out, count=len(items))


class MemorySessionItems:
    """Items in memory, one list per org, session, and conversation. For tests and local
    development."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    def items(self, org_id: str, session_id: str, conversation: str) -> Sequence[dict[str, Any]]:
        return tuple(self._items.get((org_id, session_id, conversation), ()))

    async def call(self, scope: RunScope, request: SessionItemsRequest) -> SessionItemsAnswer:
        async with self._lock:
            key = (scope.org_id, scope.session_id, request.conversation)
            items = self._items.setdefault(key, [])
            # A copy of each item, so a caller cannot change the stored items.
            answer = apply(items, request.model_copy(deep=True))
            return answer.model_copy(deep=True)


class RemoteSessionItems:
    """A ``SessionItems`` that sends each operation to ``POST <url>`` with the run token. The
    tool server takes the session from the token, so the scope is not on the wire."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout_s: float = SESSION_ITEMS_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        self._token = token
        self._timeout_s = timeout_s
        self._transport = transport

    def __repr__(self) -> str:
        return f"RemoteSessionItems(url={self._url!r}, token=<hidden>)"

    async def call(self, scope: RunScope, request: SessionItemsRequest) -> SessionItemsAnswer:
        async with httpx.AsyncClient(
            timeout=self._timeout_s, transport=self._transport, follow_redirects=False
        ) as client:
            response = await client.post(
                self._url,
                content=request.model_dump_json(exclude_defaults=True),
                headers={
                    "authorization": f"Bearer {self._token}",
                    "content-type": "application/json",
                },
            )
        if response.status_code == 409:
            raise SessionItemsError(_error_text(response))
        response.raise_for_status()
        return SessionItemsAnswer.model_validate_json(response.content)


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "the store refused the operation"
    error = body.get("error") if isinstance(body, dict) else None
    return str(error) if error else "the store refused the operation"
