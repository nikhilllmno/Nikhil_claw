"""Embeddings of messages, for the meaning part of session search (Section 7.1).

``OpenAIEmbedder`` calls the embeddings endpoint with the key of the org. ``EmbedWorker`` is
a loop of the control plane. It embeds each new message. Without an OpenAI key, nothing here
runs, and session search uses the full-text index alone.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import EMBEDDING_DIMENSIONS, Message, MessageEmbedding
from sama.vault.crypto import VaultError
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


class EmbedError(Exception):
    """The embedding call failed. ``retryable`` is false for a request that the provider
    refuses, for example a bad input. A retry cannot help then."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class Embedder(Protocol):
    async def embed(self, org_id: uuid.UUID, texts: Sequence[str]) -> list[list[float]] | None:
        """Gives one vector for each text. Gives ``None`` if the org has no key. Raises
        ``EmbedError``."""
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
    ) -> None:
        self._keys = keys
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
        try:
            response = await self._http.post(
                f"{self._base_url}/embeddings",
                headers={"Authorization": f"Bearer {key.get_secret_value()}"},
                json={"model": self._model, "input": list(texts)},
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
    - A transient failure (network, 429, 5xx) backs off, up to ``BACKOFF_MAX_S``.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        embedder: Embedder,
        *,
        model: str = EMBEDDING_MODEL,
        batch: int = BATCH,
        no_key_retry_s: float = NO_KEY_RETRY_S,
    ) -> None:
        self._engine = engine
        self._embedder = embedder
        self._model = model
        self._batch = batch
        self._no_key_retry_s = no_key_retry_s
        self._no_key: dict[uuid.UUID, float] = {}

    def _waiting(self) -> list[uuid.UUID]:
        now = time.monotonic()
        self._no_key = {org: until for org, until in self._no_key.items() if until > now}
        return list(self._no_key)

    async def run_once(self) -> int:
        """Handles one batch. Gives the number of messages that it marked. Raises
        ``EmbedError`` for a transient failure."""
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
            # Each org of this batch has no key now, and the next batch skips these orgs.

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
        texts = [row.body[:EMBED_INPUT_CHARS] for row in rows]
        try:
            vectors = await self._embedder.embed(org_id, texts)
        except EmbedError as error:
            if error.retryable:
                raise
            log.warning("embedding refused for org=%s: %s", org_id, error)
            if len(rows) == 1:
                return await self._mark([rows[0].id])
            # One bad message can refuse the whole batch. Find it by one call for each.
            return sum([await self._embed_org(org_id, [row]) for row in rows])
        if vectors is None:
            self._no_key[org_id] = time.monotonic() + self._no_key_retry_s
            return 0
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
            except EmbedError as error:
                delay = min(delay * 2, BACKOFF_MAX_S)
                log.warning("the embed worker backs off for %.0f s: %s", delay, error)
            except Exception:
                delay = min(delay * 2, BACKOFF_MAX_S)
                log.exception("the embed worker failed")
            if handled >= self._batch:
                continue  # A full batch: more messages wait.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
