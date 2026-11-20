"""The OpenAI embedder and the embed worker (Section 7.1)."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import EMBEDDING_DIMENSIONS, Message, MessageEmbedding
from sama.search import EmbedError, EmbedWorker, OpenAIEmbedder
from sama.vault.store import OrgKeys, SecretName
from tests.search.rig import World, say

ORG = uuid.uuid4()


def keys(value: str | None = "sk-test-key") -> OrgKeys:
    return OrgKeys(None, {SecretName.OPENAI_API_KEY: None if value is None else SecretStr(value)})


def embedder(
    handler: Callable[[httpx.Request], httpx.Response], key: str | None = "sk-test-key"
) -> OpenAIEmbedder:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAIEmbedder(keys(key), http=http, base_url="https://openai.test/v1")


def reply(count: int, size: int = EMBEDDING_DIMENSIONS) -> httpx.Response:
    # The rows come back in reverse order. The embedder sorts them by index.
    data = [{"index": i, "embedding": [float(i)] * size} for i in reversed(range(count))]
    return httpx.Response(200, json={"data": data})


async def test_the_embedder_sends_the_key_and_sorts_the_rows() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply(2)

    vectors = await embedder(handler).embed(ORG, ["one", "two"])
    assert vectors is not None
    assert [v[0] for v in vectors] == [0.0, 1.0]
    assert seen[0].url == "https://openai.test/v1/embeddings"
    assert seen[0].headers["authorization"] == "Bearer sk-test-key"
    assert b'"text-embedding-3-small"' in seen[0].content


async def test_an_org_without_a_key_gets_none_and_no_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no call without a key")

    assert await embedder(handler, key=None).embed(ORG, ["one"]) is None


@pytest.mark.parametrize(
    ("status", "retryable"), [(400, False), (422, False), (401, True), (429, True), (503, True)]
)
async def test_a_failed_call_says_if_a_retry_can_help(status: int, retryable: bool) -> None:
    with pytest.raises(EmbedError) as error:
        await embedder(lambda request: httpx.Response(status)).embed(ORG, ["one"])
    assert error.value.retryable is retryable
    assert "sk-test-key" not in str(error.value)


@pytest.mark.parametrize(
    "response",
    [
        reply(1, size=3),
        reply(2),
        httpx.Response(200, json={"nope": 1}),
        httpx.Response(200, text="not json"),
    ],
    ids=["wrong size", "wrong count", "no data", "not json"],
)
async def test_a_bad_response_is_an_error(response: httpx.Response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    with pytest.raises(EmbedError):
        await embedder(handler).embed(ORG, ["one"])


async def test_a_network_error_can_be_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(EmbedError) as error:
        await embedder(handler).embed(ORG, ["one"])
    assert error.value.retryable


class Fake:
    """Gives a vector for each text. ``blind`` orgs have no key. ``refuse`` is a text that the
    provider refuses."""

    def __init__(self, *, blind: Sequence[uuid.UUID] = (), refuse: str | None = None) -> None:
        self.blind = set(blind)
        self.refuse = refuse
        self.calls: list[tuple[uuid.UUID, list[str]]] = []
        self.transient = False

    async def embed(self, org_id: uuid.UUID, texts: Sequence[str]) -> list[list[float]] | None:
        self.calls.append((org_id, list(texts)))
        if self.transient:
            raise EmbedError("down")
        if org_id in self.blind:
            return None
        if self.refuse is not None and self.refuse in texts:
            raise EmbedError("refused", retryable=False)
        return [[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1) for _ in texts]


async def drain(worker: EmbedWorker) -> None:
    while await worker.run_once():
        pass


async def state(engine: AsyncEngine, message: uuid.UUID) -> tuple[bool, bool]:
    """Gives ``(handled, has an embedding)`` of a message."""
    async with engine.connect() as connection:
        handled = (
            await connection.execute(select(Message.embedded_at).where(Message.id == message))
        ).scalar_one()
        count = (
            await connection.execute(
                select(func.count()).where(MessageEmbedding.message_id == message)
            )
        ).scalar_one()
    return handled is not None, count == 1


async def test_the_worker_embeds_new_messages_once(engine: AsyncEngine, world: World) -> None:
    ids = world.ids
    first = await say(engine, world.org, ids["s1"], "deploy with fly")
    second = await say(engine, world.org, ids["s1"], "done", role="assistant")
    fake = Fake()
    worker = EmbedWorker(engine, fake)
    await drain(worker)
    assert await state(engine, first) == (True, True)
    assert await state(engine, second) == (True, True)
    calls = len(fake.calls)
    await drain(worker)
    assert len(fake.calls) == calls


async def test_the_worker_skips_empty_and_system_messages(
    engine: AsyncEngine, world: World
) -> None:
    ids = world.ids
    empty = await say(engine, world.org, ids["s1"], "   ")
    system = await say(engine, world.org, ids["s1"], "internal note", role="system")
    fake = Fake()
    await drain(EmbedWorker(engine, fake))
    assert await state(engine, empty) == (True, False)
    assert await state(engine, system) == (True, False)
    assert all("internal note" not in texts for _, texts in fake.calls)


async def test_an_org_without_a_key_waits_and_does_not_block_the_others(
    engine: AsyncEngine, world: World
) -> None:
    assert world.other is not None
    blind = await say(engine, world.org, world.ids["s1"], "no key here")
    seen = await say(engine, world.other.org, world.ids["other_org"], "other org has a key")
    fake = Fake(blind=[world.org])
    worker = EmbedWorker(engine, fake, batch=1)
    await drain(worker)
    assert await state(engine, blind) == (False, False)
    assert await state(engine, seen) == (True, True)
    # The worker does not ask again at once.
    calls = len(fake.calls)
    await drain(worker)
    assert len(fake.calls) == calls
    # With a key, the next run (a new worker has no memory of the wait) embeds the message.
    await drain(EmbedWorker(engine, Fake()))
    assert await state(engine, blind) == (True, True)


async def test_a_refused_message_is_marked_and_does_not_block_the_queue(
    engine: AsyncEngine, world: World
) -> None:
    ids = world.ids
    good = await say(engine, world.org, ids["s1"], "a good message")
    bad = await say(engine, world.org, ids["s1"], "a poison message")
    await drain(EmbedWorker(engine, Fake(refuse="a poison message")))
    assert await state(engine, good) == (True, True)
    assert await state(engine, bad) == (True, False)


async def test_a_transient_error_leaves_the_messages_for_the_next_run(
    engine: AsyncEngine, world: World
) -> None:
    message = await say(engine, world.org, world.ids["s1"], "try later")
    fake = Fake()
    fake.transient = True
    worker = EmbedWorker(engine, fake)
    with pytest.raises(EmbedError):
        await worker.run_once()
    assert await state(engine, message) == (False, False)
    fake.transient = False
    await drain(worker)
    assert await state(engine, message) == (True, True)


async def test_a_long_text_is_cut_before_the_call(engine: AsyncEngine, world: World) -> None:
    await say(engine, world.org, world.ids["s1"], "x" * 50_000)
    fake = Fake()
    await drain(EmbedWorker(engine, fake))
    sent = [t for org, texts in fake.calls if org == world.org for t in texts]
    assert sent
    assert max(len(t) for t in sent) <= 6_000
