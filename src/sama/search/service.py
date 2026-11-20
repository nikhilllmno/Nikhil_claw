"""Session search: finds past sessions that the user may see (Section 7.1).

Two ranked lists feed one result: the full-text index of ``messages`` and, if embeddings
exist, the pgvector index of ``message_embeddings``. Reciprocal rank fusion joins them. No
LLM call runs here.

Every query is scoped by the org and by the visibility rule of ``visible``. The control plane
connects as the table owner, so RLS does not protect these queries. The scope in code does.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import (
    ColumnElement,
    Float,
    Text,
    and_,
    bindparam,
    cast,
    false,
    func,
    literal_column,
    or_,
    select,
)
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import (
    EMBEDDING_DIMENSIONS,
    Message,
    MessageEmbedding,
    Session,
    Vector,
)
from sama.search.core import (
    CANDIDATES,
    HEADLINE_SOURCE_CHARS,
    MAX_DISTANCE,
    MAX_LIMIT,
    MAX_QUERY_CHARS,
    READ_MAX_MESSAGES,
    SNIPPET_START,
    SNIPPET_STOP,
    Candidate,
    PageMessage,
    SessionHit,
    SessionPage,
    SessionSearchError,
    Snippet,
    fuse,
    pick,
    vector_literal,
)
from sama.search.embed import Embedder, EmbedError
from sama.tenancy.access import load_grants
from sama.tenancy.run_token import RunTokenClaims
from sama.vault.redact import redact

log = logging.getLogger("sama.search")

SEARCH_ROLES: Final = ("user", "assistant")
EMBED_TIMEOUT_S: Final = 3.0
"""The wait for the embedding of the query. A slower call gives a full-text result."""
NOT_FOUND: Final = "session not found"

_SIMPLE: Final[ColumnElement[Any]] = literal_column("'simple'")
_HEADLINE_OPTIONS: Final = (
    f"MaxWords=40, MinWords=15, MaxFragments=1, StartSel={SNIPPET_START}, StopSel={SNIPPET_STOP}"
)


def _uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(value)


class SessionSearch:
    """Search and read for the ``session_search`` and ``session_read`` tools.

    ``embedder`` is optional. Without it, or without an OpenAI key, the search uses the
    full-text index alone.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        embedder: Embedder | None = None,
        *,
        embed_timeout_s: float = EMBED_TIMEOUT_S,
    ) -> None:
        self._engine = engine
        self._embedder = embedder
        self._embed_timeout_s = embed_timeout_s

    # -- visibility -------------------------------------------------------------------------

    async def visible(
        self, connection: AsyncConnection, claims: RunTokenClaims
    ) -> ColumnElement[bool]:
        """The sessions that this run may see, as a SQL condition on ``Session``.

        - A private run (it has a user) sees the shared sessions and the own private sessions
          in the workspaces that the user sees. The user must be an org member.
        - A shared run sees the shared sessions of its own workspace and nothing else. It has
          no user, so it never sees a private session, and it never sees the sessions of
          another workspace.
        """
        org_id = _uuid(claims.org_id)
        if claims.user_id is None:
            return and_(
                Session.org_id == org_id,
                Session.audience == "shared",
                Session.workspace_id == _uuid(claims.workspace_id),
            )
        user_id = _uuid(claims.user_id)
        grants = await load_grants(connection, org_id, user_id)
        if not grants.org_member:
            return false()
        audience = or_(
            Session.audience == "shared",
            and_(Session.audience == "private", Session.owner_user_id == user_id),
        )
        if grants.org_admin:
            return and_(Session.org_id == org_id, audience)
        workspaces = [workspace for workspace, _role in grants.rows if workspace is not None]
        return and_(Session.org_id == org_id, audience, Session.workspace_id.in_(workspaces))

    # -- search -----------------------------------------------------------------------------

    async def search(
        self, claims: RunTokenClaims, query: str | None, limit: int
    ) -> list[SessionHit]:
        """Gives at most ``limit`` sessions, best first, each with up to two snippets. An empty
        query gives the latest sessions without snippets."""
        text_query = (query or "").strip()
        if len(text_query) > MAX_QUERY_CHARS:
            raise SessionSearchError(f"the query has more than {MAX_QUERY_CHARS} characters")
        limit = max(1, min(limit, MAX_LIMIT))
        org_id = _uuid(claims.org_id)
        current = _uuid(claims.session_id)
        async with self._engine.connect() as connection:
            visible = await self.visible(connection, claims)
            others = and_(visible, Session.id != current)
            if not text_query:
                return await self._recent(connection, others, limit)
            tsquery = func.websearch_to_tsquery(_SIMPLE, text_query)
            ranked = [await self._full_text(connection, others, tsquery)]
            embedder = self._embedder
            has_vectors = embedder is not None and await self._has_embeddings(connection, org_id)
        if embedder is not None and has_vectors:
            vector = await self._query_vector(embedder, org_id, text_query)
            if vector is not None:
                async with self._engine.connect() as connection:
                    ranked.append(await self._by_meaning(connection, others, vector))
        chosen = pick(fuse(ranked), limit)
        if not chosen:
            return []
        async with self._engine.connect() as connection:
            return await self._hits(connection, org_id, tsquery, chosen)

    async def _recent(
        self, connection: AsyncConnection, visible: ColumnElement[bool], limit: int
    ) -> list[SessionHit]:
        rows = await connection.execute(
            select(*_session_columns())
            .where(visible)
            .order_by(Session.updated_at.desc(), Session.id)
            .limit(limit)
        )
        return [_hit(row) for row in rows]

    async def _full_text(
        self, connection: AsyncConnection, visible: ColumnElement[bool], tsquery: Any
    ) -> list[Candidate]:
        match = Message.search_vector.op("@@")(tsquery)
        rows = await connection.execute(
            select(Message.id, Message.session_id)
            .join(Session, and_(Session.org_id == Message.org_id, Session.id == Message.session_id))
            .where(visible, Message.role.in_(SEARCH_ROLES), match)
            .order_by(func.ts_rank_cd(Message.search_vector, tsquery).desc(), Message.id)
            .limit(CANDIDATES)
        )
        return [Candidate(row.id, row.session_id) for row in rows]

    async def _has_embeddings(self, connection: AsyncConnection, org_id: uuid.UUID) -> bool:
        row = await connection.execute(
            select(MessageEmbedding.id).where(MessageEmbedding.org_id == org_id).limit(1)
        )
        return row.first() is not None

    async def _query_vector(
        self, embedder: Embedder, org_id: uuid.UUID, query: str
    ) -> list[float] | None:
        """The embedding of the query, or ``None``: no key, a slow call, or a failed call. The
        search then uses the full-text list alone."""
        try:
            async with asyncio.timeout(self._embed_timeout_s):
                vectors = await embedder.embed(org_id, [query])
        except (TimeoutError, EmbedError) as error:
            log.warning("session search: no query embedding: %s", type(error).__name__)
            return None
        return vectors[0] if vectors else None

    async def _by_meaning(
        self, connection: AsyncConnection, visible: ColumnElement[bool], vector: list[float]
    ) -> list[Candidate]:
        point = cast(
            bindparam("point", vector_literal(vector), type_=Text), Vector(EMBEDDING_DIMENSIONS)
        )
        distance = MessageEmbedding.embedding.op("<=>", return_type=Float)(point)
        statement = (
            select(MessageEmbedding.message_id, MessageEmbedding.session_id)
            .join(
                Session,
                and_(
                    Session.org_id == MessageEmbedding.org_id,
                    Session.id == MessageEmbedding.session_id,
                ),
            )
            .where(visible, distance <= MAX_DISTANCE)
            .order_by(distance, MessageEmbedding.message_id)
            .limit(CANDIDATES)
        )
        async with connection.begin_nested():
            # The index scan filters after the scan. With iterative scan, pgvector goes on
            # until it has enough rows that pass the filter. pgvector before 0.8 lacks it.
            with contextlib.suppress(DBAPIError):
                async with connection.begin_nested():
                    await connection.exec_driver_sql(
                        "SET LOCAL hnsw.iterative_scan = relaxed_order"
                    )
            rows = await connection.execute(statement)
        return [Candidate(row.message_id, row.session_id) for row in rows]

    async def _hits(
        self,
        connection: AsyncConnection,
        org_id: uuid.UUID,
        tsquery: Any,
        chosen: Sequence[tuple[uuid.UUID, list[uuid.UUID]]],
    ) -> list[SessionHit]:
        session_ids = [session_id for session_id, _ in chosen]
        message_ids = [message_id for _, ids in chosen for message_id in ids]
        sessions = {
            row.id: row
            for row in await connection.execute(
                select(*_session_columns()).where(
                    Session.org_id == org_id, Session.id.in_(session_ids)
                )
            )
        }
        body = func.left(Message.content["text"].astext, HEADLINE_SOURCE_CHARS)
        snippets = {
            row.id: Snippet(
                row.id,
                row.role,
                row.created_at,
                redact(row.snippet.strip()),
            )
            for row in await connection.execute(
                select(
                    Message.id,
                    Message.role,
                    Message.created_at,
                    func.ts_headline(_SIMPLE, body, tsquery, _HEADLINE_OPTIONS).label("snippet"),
                ).where(Message.org_id == org_id, Message.id.in_(message_ids))
            )
        }
        hits = []
        for session_id, ids in chosen:
            row = sessions.get(session_id)
            if row is None:
                continue
            found = tuple(snippets[i] for i in ids if i in snippets)
            hits.append(_hit(row, found))
        return hits

    # -- read -------------------------------------------------------------------------------

    async def read(
        self, claims: RunTokenClaims, session_id: str, offset: int, limit: int
    ) -> SessionPage:
        """Gives a page of messages of a session. A session that the run may not see gives the
        same error as a session that does not exist."""
        try:
            wanted = uuid.UUID(session_id)
        except ValueError:
            raise SessionSearchError(NOT_FOUND) from None
        limit = max(1, min(limit, READ_MAX_MESSAGES))
        offset = max(0, offset)
        async with self._engine.connect() as connection:
            visible = await self.visible(connection, claims)
            row = (
                await connection.execute(
                    select(*_session_columns()).where(visible, Session.id == wanted)
                )
            ).first()
            if row is None:
                raise SessionSearchError(NOT_FOUND)
            counted = and_(
                Message.org_id == row.org_id,
                Message.session_id == wanted,
                Message.role.in_(SEARCH_ROLES),
            )
            total = (
                await connection.execute(select(func.count()).select_from(Message).where(counted))
            ).scalar_one()
            messages = await connection.execute(
                select(
                    Message.seq,
                    Message.role,
                    Message.created_at,
                    Message.content["text"].astext.label("body"),
                )
                .where(counted)
                .order_by(Message.seq)
                .offset(offset)
                .limit(limit)
            )
            page = tuple(
                PageMessage(m.seq, m.role, m.created_at, redact(m.body or "")) for m in messages
            )
        return SessionPage(_hit(row), total, offset, page)


def _session_columns() -> tuple[Any, ...]:
    return (
        Session.id,
        Session.org_id,
        Session.workspace_id,
        Session.title,
        Session.audience,
        Session.created_at,
        Session.updated_at,
    )


def _hit(row: Any, snippets: tuple[Snippet, ...] = ()) -> SessionHit:
    return SessionHit(
        row.id,
        row.workspace_id,
        redact(row.title) if row.title else None,
        row.audience,
        row.created_at,
        row.updated_at,
        snippets,
    )
