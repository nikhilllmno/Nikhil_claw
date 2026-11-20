"""Embeddings of messages, for the meaning part of session search (Section 7.1).

``OpenAIEmbedder`` calls the embeddings endpoint with the key of the org. ``EmbedWorker`` is
a loop of the control plane. It embeds each new message. Without an OpenAI key, nothing here
runs, and session search uses the full-text index alone.

``OpenAIEmbedder`` is the only way out of the process for the text of a message or a query. It
redacts each complete text first, and cuts the redacted text after that. A secret that the
redactor learned after the message was stored does not leave the process, and a cut cannot
split a secret into a part that the redactor does not know.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import EMBEDDING_DIMENSIONS, Message, MessageEmbedding
from sama.vault.crypto import VaultError
from sama.vault.redact import Redactor, default_redactor
from sama.vault.store import OrgKeys, SecretName

log = logging.getLogger("sama.search")

EMBEDDING_MODEL: Final = "text-embedding-3-small"
OPENAI_URL: Final = "https://api.openai.com/v1"
EMBED_INPUT_CHARS: Final = 6_000
"""The embedding model reads at most 8,191 tokens. 6,000 characters stay below this limit for
all languages."""
EMBED_ROLES: Final = ("user", "assistant")
BATCH: Final = 64
INTERVAL_S: Final = 5.0
BACKOFF_MAX_S: Final = 300.0
NO_KEY_RETRY_S: Final = 600.0
"""The worker asks again for the key of an org without a key after this time."""
ORG_BACKOFF_S: Final = 5.0
"""The first wait after a transient failure of one org. Each next failure doubles it, up to
``BACKOFF_MAX_S``."""


def prepare(text: str, redactor: Redactor) -> str:
    """The text that leaves the process: the complete text, redacted, then cut."""
    return redactor.text(text)[:EMBED_INPUT_CHARS]


class EmbedError(Exception):
    """The embedding call failed. ``retryable`` is false for a request that the provider
    refuses, for example a bad input. A retry cannot help then."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class Embedder(Protocol):
    async def embed(self, org_id: uuid.UUID, texts: Sequence[str]) -> list[list[float]] | None:
        """Gives one vector for each text. A caller gives the complete texts. The embedder
        redacts them and cuts them to ``EMBED_INPUT_CHARS``. Gives ``None`` if the org has no
        key. Raises ``EmbedError``."""
        ...


class OpenAIEmbedder:
    """The OpenAI embeddings endpoint. The key stays in the control plane."""

    def __init__(
        self,
        keys: OrgKeys,
        *,
        model: str = EMBEDDING_MODEL,
        base_url: str = OPENAI_URL,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = 30.0,
        redactor: Redactor | None = None,
    ) -> None:
        self._keys = keys
        self._redactor = redactor or default_redactor()
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=timeout_s, trust_env=False)

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def model(self) -> str:
        return self._model

    async def embed(self, org_id: uuid.UUID, texts: Sequence[str]) -> list[list[float]] | None:
        try:
            key = await self._keys.get(org_id, SecretName.OPENAI_API_KEY)
        except VaultError:
            log.warning("the OpenAI key of org=%s does not decrypt", org_id)
            return None
        if key is None:
            return None
        # Redact the complete texts, then cut. This is the one place where text leaves.
        inputs = [prepare(text, self._redactor) for text in texts]
        try:
            response = await self._http.post(
                f"{self._base_url}/embeddings",
                headers={"Authorization": f"Bearer {key.get_secret_value()}"},
                json={"model": self._model, "input": inputs},
            )
        except httpx.HTTPError as error:
            raise EmbedError(f"embedding failed: {type(error).__name__}") from None
        status = response.status_code
        if status != 200:
            permanent = 400 <= status < 500 and status not in (401, 403, 408, 429)
            raise EmbedError(f"embedding failed: HTTP {status}", retryable=not permanent)
        try:
            rows = sorted(response.json()["data"], key=lambda row: row["index"])
            vectors = [[float(x) for x in row["embedding"]] for row in rows]
        except (ValueError, KeyError, TypeError):
            raise EmbedError("embedding failed: a bad response") from None
        if len(vectors) != len(texts) or any(len(v) != EMBEDDING_DIMENSIONS for v in vectors):
            raise EmbedError("embedding failed: a bad response")
        return vectors


@dataclass(frozen=True, slots=True)
class Pending:
    id: uuid.UUID
    org_id: uuid.UUID
    session_id: uuid.UUID
    role: str
    body: str


class EmbedWorker:
    """Embeds the messages that have no ``embedded_at``, oldest first.

    - A message with no text, or from a role that search skips, gets ``embedded_at`` and no
      embedding.
    - An org without a key waits: the worker leaves its messages and tries again later, so
      one org without a key does not block the others.
    - A refused message gets ``embedded_at`` and no embedding, so it does not block the queue.
    - A transient failure (network, 429, 5xx) of one org puts that org on a backoff of its own,
      from ``ORG_BACKOFF_S`` up to ``BACKOFF_MAX_S``. The pending query skips an org that
      waits, so the failing org does not take the batch and the other orgs go on.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        embedder: Embedder,
        *,
        model: str = EMBEDDING_MODEL,
        batch: int = BATCH,
        no_key_retry_s: float = NO_KEY_RETRY_S,
        backoff_s: float = ORG_BACKOFF_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._embedder = embedder
        self._model = model
        self._batch = batch
        self._no_key_retry_s = no_key_retry_s
        self._backoff_s = backoff_s
        self._clock = clock
        self._until: dict[uuid.UUID, float] = {}
        self._failures: dict[uuid.UUID, int] = {}

    def _waiting(self) -> list[uuid.UUID]:
        """The orgs that wait: without a key, or on a backoff after a failure."""
        now = self._clock()
        self._until = {org: until for org, until in self._until.items() if until > now}
        return list(self._until)

    def _fail(self, org_id: uuid.UUID) -> float:
        failures = self._failures.get(org_id, 0) + 1
        self._failures[org_id] = failures
        wait = min(self._backoff_s * float(2 ** min(failures - 1, 16)), BACKOFF_MAX_S)
        self._until[org_id] = self._clock() + wait
        return wait

    async def run_once(self) -> int:
        """Handles one batch. Gives the number of messages that it marked. A transient failure
        of an org does not raise: the org waits, and the other orgs go on."""
        while True:
            rows = await self._pending()
            if not rows:
                return 0
            todo = [row for row in rows if row.role in EMBED_ROLES and row.body.strip()]
            marked = await self._mark([row.id for row in rows if row not in todo])
            by_org: dict[uuid.UUID, list[Pending]] = {}
            for row in todo:
                by_org.setdefault(row.org_id, []).append(row)
            for org_id, group in by_org.items():
                marked += await self._embed_org(org_id, group)
            if marked:
                return marked
            # Each org of this batch waits now, and the next batch skips these orgs.

    async def _pending(self) -> list[Pending]:
        waiting = self._waiting()
        query = (
            select(
                Message.id,
                Message.org_id,
                Message.session_id,
                Message.role,
                Message.content["text"].astext.label("body"),
            )
            .where(Message.embedded_at.is_(None))
            .order_by(Message.created_at, Message.seq)
            .limit(self._batch)
        )
        if waiting:
            query = query.where(Message.org_id.not_in(waiting))
        async with self._engine.connect() as connection:
            return [
                Pending(row.id, row.org_id, row.session_id, row.role, row.body or "")
                for row in (await connection.execute(query)).all()
            ]

    async def _embed_org(self, org_id: uuid.UUID, rows: list[Pending]) -> int:
        if org_id in self._waiting():
            return 0
        # The complete texts: the embedder redacts them before it cuts them.
        texts = [row.body for row in rows]
        try:
            vectors = await self._embedder.embed(org_id, texts)
        except EmbedError as error:
            if error.retryable:
                wait = self._fail(org_id)
                log.warning("embedding failed for org=%s, it waits %.0f s: %s", org_id, wait, error)
                return 0
            log.warning("embedding refused for org=%s: %s", org_id, error)
            if len(rows) == 1:
                return await self._mark([rows[0].id])
            # One bad message can refuse the whole batch. Find it by one call for each.
            return sum([await self._embed_org(org_id, [row]) for row in rows])
        if vectors is None:
            self._until[org_id] = self._clock() + self._no_key_retry_s
            return 0
        self._failures.pop(org_id, None)
        values = [
            {
                "org_id": org_id,
                "session_id": row.session_id,
                "message_id": row.id,
                "embedding": vector,
                "model": self._model,
            }
            for row, vector in zip(rows, vectors, strict=True)
        ]
        async with self._engine.begin() as connection:
            await connection.execute(
                insert(MessageEmbedding)
                .values(values)
                .on_conflict_do_nothing(index_elements=["org_id", "message_id"])
            )
            await connection.execute(
                update(Message)
                .where(Message.id.in_([row.id for row in rows]))
                .values(embedded_at=func.now())
            )
        return len(values)

    async def _mark(self, ids: Sequence[uuid.UUID]) -> int:
        if not ids:
            return 0
        async with self._engine.begin() as connection:
            await connection.execute(
                update(Message).where(Message.id.in_(ids)).values(embedded_at=func.now())
            )
        return len(ids)

    async def serve(self, stop: asyncio.Event, *, interval_s: float = INTERVAL_S) -> None:
        delay = interval_s
        while not stop.is_set():
            handled = 0
            try:
                handled = await self.run_once()
                delay = interval_s
            except Exception:
                delay = min(delay * 2, BACKOFF_MAX_S)
                log.exception("the embed worker failed")
            if handled >= self._batch:
                continue  # A full batch: more messages wait.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
