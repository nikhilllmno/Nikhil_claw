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
from collections.abc import Awaitable, Callable, Sequence
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
# An archive keeps the tool calls and the tool results too (Section 6.3).
ARCHIVE_ROLES: Final = ("user", "assistant", "tool", "system")
ARCHIVE_LINEAGE: Final = "compaction"
MAX_ARCHIVES_SHOWN: Final = 20
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
        return await self.visible_to_user(connection, org_id, _uuid(claims.user_id))

    # -- search -----------------------------------------------------------------------------

    async def search(
        self, claims: RunTokenClaims, query: str | None, limit: int
    ) -> list[SessionHit]:
        """Gives at most ``limit`` sessions, best first, each with up to two snippets. An empty
        query gives the latest sessions without snippets."""
        current = _uuid(claims.session_id)

        async def scope(connection: AsyncConnection) -> ColumnElement[bool]:
            return and_(await self.visible(connection, claims), Session.id != current)

        return await self._search(_uuid(claims.org_id), scope, query, limit)

    async def search_as_user(
        self,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        query: str | None,
        limit: int,
        *,
        workspace_id: uuid.UUID | None = None,
        agent_profile_id: uuid.UUID | None = None,
    ) -> list[SessionHit]:
        """The search of the web app (Section 11.4). The user sees what a private run of the
        user sees: the shared sessions and the own private sessions in the workspaces that the
        user sees. ``workspace_id`` limits the result to one workspace, and
        ``agent_profile_id`` to the sessions of one agent profile (an API key, Section 10.7)."""

        async def scope(connection: AsyncConnection) -> ColumnElement[bool]:
            visible = await self.visible_to_user(connection, org_id, user_id)
            if workspace_id is not None:
                visible = and_(visible, Session.workspace_id == workspace_id)
            if agent_profile_id is not None:
                visible = and_(visible, Session.agent_profile_id == agent_profile_id)
            return visible

        return await self._search(org_id, scope, query, limit)

    async def visible_to_user(
        self, connection: AsyncConnection, org_id: uuid.UUID, user_id: uuid.UUID
    ) -> ColumnElement[bool]:
        """The sessions that a user sees, as a SQL condition on ``Session``. It is the rule of
        a private run of the user (``visible``)."""
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

    async def _search(
        self,
        org_id: uuid.UUID,
        scope: Callable[[AsyncConnection], Awaitable[ColumnElement[bool]]],
        query: str | None,
        limit: int,
    ) -> list[SessionHit]:
        """The search of ``search`` and ``search_as_user``. ``scope`` gives the sessions that
        the caller may see. It runs again on each connection, after the slow embedding call."""
        text_query = (query or "").strip()
        if len(text_query) > MAX_QUERY_CHARS:
            raise SessionSearchError(f"the query has more than {MAX_QUERY_CHARS} characters")
        limit = max(1, min(limit, MAX_LIMIT))
        # An archive repeats the messages of its live session. The search skips it, and
        # ``session_read`` of the live session names its archives.
        not_archive = or_(Session.lineage.is_(None), Session.lineage != ARCHIVE_LINEAGE)
        if not text_query:
            async with self._engine.connect() as connection:
                visible = await scope(connection)
                return await self._recent(connection, and_(visible, not_archive), limit)
        tsquery = func.websearch_to_tsquery(_SIMPLE, text_query)
        # The query embedding is a slow call. It runs before the grants load, so that a grant
        # that changes during the call counts: every read below uses the fresh rule.
        vector = await self._query_vector(org_id, text_query)
        async with self._engine.connect() as connection:
            others = and_(await scope(connection), not_archive)
            ranked = [await self._full_text(connection, others, tsquery)]
            if vector is not None:
                ranked.append(await self._by_meaning(connection, others, vector))
            chosen = pick(fuse(ranked), limit)
            if not chosen:
                return []
            # The chosen ids are not an authorization. The rule applies again to each row.
            return await self._hits(connection, others, tsquery, chosen)

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

    async def _query_vector(self, org_id: uuid.UUID, query: str) -> list[float] | None:
        """The embedding of the query, or ``None``: no embedder, no embeddings in the org, no
        key, a slow call, or a failed call. The search then uses the full-text list alone. The
        query is redacted here and again by the embedder."""
        embedder = self._embedder
        if embedder is None:
            return None
        async with self._engine.connect() as connection:
            if not await self._has_embeddings(connection, org_id):
                return None
        try:
            async with asyncio.timeout(self._embed_timeout_s):
                vectors = await embedder.embed(org_id, [redact(query)])
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
        visible: ColumnElement[bool],
        tsquery: Any,
        chosen: Sequence[tuple[uuid.UUID, list[uuid.UUID]]],
    ) -> list[SessionHit]:
        """Loads the chosen sessions and messages. ``visible`` applies to both reads."""
        session_ids = [session_id for session_id, _ in chosen]
        message_ids = [message_id for _, ids in chosen for message_id in ids]
        sessions = {
            row.id: row
            for row in await connection.execute(
                select(*_session_columns()).where(visible, Session.id.in_(session_ids))
            )
        }
        rows = await connection.execute(
            select(
                Message.id,
                Message.session_id,
                Message.role,
                Message.created_at,
                Message.content["text"].astext.label("body"),
            )
            .join(Session, and_(Session.org_id == Message.org_id, Session.id == Message.session_id))
            .where(visible, Message.id.in_(message_ids))
        )
        snippets: dict[uuid.UUID, Snippet] = {}
        for row in rows:
            if row.session_id not in sessions:
                continue
            text = await self._snippet(connection, tsquery, row.body or "")
            snippets[row.id] = Snippet(row.id, row.role, row.created_at, text)
        hits = []
        for session_id, ids in chosen:
            session = sessions.get(session_id)
            if session is None:
                continue
            found = tuple(snippets[i] for i in ids if i in snippets)
            hits.append(_hit(session, found))
        return hits

    async def _snippet(self, connection: AsyncConnection, tsquery: Any, body: str) -> str:
        """The snippet of a message. The complete text is redacted first. Highlight and cut
        come after that, so that the marks and the cut cannot split a secret into parts that
        the redactor does not know. The last redaction of the output stays as a second guard."""
        safe = redact(body)[:HEADLINE_SOURCE_CHARS]
        highlighted = (
            await connection.execute(
                select(
                    func.ts_headline(
                        _SIMPLE, bindparam("body", safe, type_=Text), tsquery, _HEADLINE_OPTIONS
                    )
                )
            )
        ).scalar_one()
        return redact(highlighted.strip())

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
            roles = ARCHIVE_ROLES if row.lineage == ARCHIVE_LINEAGE else SEARCH_ROLES
            counted = and_(
                Message.org_id == row.org_id,
                Message.session_id == wanted,
                Message.role.in_(roles),
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
            archives = tuple(
                found.id
                for found in await connection.execute(
                    select(Session.id)
                    .where(
                        visible,
                        Session.parent_session_id == wanted,
                        Session.lineage == ARCHIVE_LINEAGE,
                    )
                    .order_by(Session.created_at, Session.id)
                    .limit(MAX_ARCHIVES_SHOWN)
                )
            )
        return SessionPage(_hit(row), total, offset, page, archives)


def _session_columns() -> tuple[Any, ...]:
    return (
        Session.id,
        Session.org_id,
        Session.workspace_id,
        Session.title,
        Session.audience,
        Session.created_at,
        Session.updated_at,
        Session.lineage,
        Session.parent_session_id,
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
        row.lineage,
        row.parent_session_id,
    )
