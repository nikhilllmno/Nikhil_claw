"""Compaction records and the transcript archive (docs/architecture.md, Section 6.3).

Two steps write ``session_compactions``:

1. ``ContextArchive.archive`` runs when the runtime is about to compact (the ``PreCompact``
   hook of the Claude runtime, through ``POST /archive``). It stores the transcript as an
   archive session and makes the row.
2. ``record_compacted`` runs when the ``compacted`` event of the run is stored. It sets the
   token counts on the row that step 1 made, or makes a row if no archive came. It also makes
   all pins of the session due for the next run.

The archive session is a child of the live session with the lineage ``compaction``. The live
session keeps its id, so the chat binding, the sandbox, and the approvals stay as they are.
The archive session has the audience, the owner, and the workspace of the live session, so
the visibility rule of search and read (Section 7.1) holds for it without a new rule.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.context.pins import forget_injections
from sama.db.models import AuditEntry, Message, Run, Session, SessionCompaction
from sama.runtime.archive import ArchiveAnswer, TranscriptArchive, fit
from sama.tenancy.run_token import RunTokenClaims
from sama.vault.redact import redact

MAX_ARCHIVES_PER_RUN: Final = 10
"""A run that asks for more archives is refused: the run token must not fill the table."""
TITLE_CHARS: Final = 80


class ArchiveError(Exception):
    """The archive is refused. The message is safe to show to the runner."""


def _ids(claims: RunTokenClaims) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    try:
        return uuid.UUID(claims.org_id), uuid.UUID(claims.session_id), uuid.UUID(claims.run_id)
    except ValueError:
        raise ArchiveError("the run token has no valid run") from None


class ContextArchive:
    """Stores the transcript that a runtime gives before it compacts."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def archive(self, claims: RunTokenClaims, body: TranscriptArchive) -> ArchiveAnswer:
        org_id, session_id, run_id = _ids(claims)
        # Redact the whole text first. The cut comes after, because a cut can split a secret
        # into parts that the redactor does not know.
        redacted = [entry.model_copy(update={"text": redact(entry.text)}) for entry in body.entries]
        entries, truncated = fit(redacted)
        entries = [entry for entry in entries if entry.text.strip()]
        if not entries:
            return ArchiveAnswer(entries=0)
        async with self._engine.begin() as connection:
            run = (
                await connection.execute(
                    select(Run.status).where(
                        Run.org_id == org_id, Run.session_id == session_id, Run.id == run_id
                    )
                )
            ).scalar_one_or_none()
            if run != "running":
                raise ArchiveError("the run is not running")
            live = (
                await connection.execute(
                    select(
                        Session.workspace_id,
                        Session.agent_profile_id,
                        Session.audience,
                        Session.owner_user_id,
                        Session.title,
                        Session.status,
                    )
                    .where(Session.org_id == org_id, Session.id == session_id)
                    .with_for_update()
                )
            ).one_or_none()
            if live is None or live.status != "active":
                raise ArchiveError("the session is not active")
            earlier = (
                await connection.execute(
                    select(func.count()).where(
                        SessionCompaction.org_id == org_id,
                        SessionCompaction.session_id == session_id,
                    )
                )
            ).scalar_one()
            this_run = (
                await connection.execute(
                    select(func.count()).where(
                        SessionCompaction.org_id == org_id, SessionCompaction.run_id == run_id
                    )
                )
            ).scalar_one()
            if this_run >= MAX_ARCHIVES_PER_RUN:
                raise ArchiveError("the run made too many archives")
            title = " ".join(redact(live.title or "session").split())[:TITLE_CHARS]
            archive_id = uuid.uuid4()
            await connection.execute(
                insert(Session).values(
                    id=archive_id,
                    org_id=org_id,
                    workspace_id=live.workspace_id,
                    agent_profile_id=live.agent_profile_id,
                    audience=live.audience,
                    owner_user_id=live.owner_user_id,
                    parent_session_id=session_id,
                    lineage="compaction",
                    title=f"Before compaction {earlier + 1}: {title}",
                    status="archived",
                )
            )
            now = datetime.now(UTC)
            await connection.execute(
                insert(Message),
                [
                    {
                        "org_id": org_id,
                        "session_id": archive_id,
                        "run_id": None,
                        "role": entry.role,
                        "content": {"text": entry.text},
                        # The embed worker skips the archive: the live session has the same
                        # user and assistant messages.
                        "embedded_at": now,
                    }
                    for entry in entries
                ],
            )
            await connection.execute(
                insert(SessionCompaction).values(
                    org_id=org_id,
                    session_id=session_id,
                    run_id=run_id,
                    trigger=body.trigger,
                    archive_session_id=archive_id,
                    archive_entries=len(entries),
                    archive_truncated=truncated,
                )
            )
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=org_id,
                    workspace_id=live.workspace_id,
                    actor_kind="system",
                    action="context.archived",
                    target_type="session",
                    target_id=str(archive_id),
                    run_id=run_id,
                    details={
                        "session_id": str(session_id),
                        "entries": len(entries),
                        "truncated": truncated,
                    },
                )
            )
        return ArchiveAnswer(entries=len(entries), truncated=truncated)


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


async def record_compacted(
    connection: AsyncConnection,
    *,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    payload: Mapping[str, Any],
) -> None:
    """Stores a ``compacted`` run event as a compaction of the session.

    It completes the newest open archive row of the run, or makes a row without an archive. It
    runs in the transaction that stores the event, so an event and its row exist together.
    """
    trigger = "manual" if payload.get("trigger") == "manual" else "auto"
    before, after = _count(payload.get("tokens_before")), _count(payload.get("tokens_after"))
    open_row = (
        await connection.execute(
            select(SessionCompaction.id)
            .where(
                SessionCompaction.org_id == org_id,
                SessionCompaction.run_id == run_id,
                SessionCompaction.completed_at.is_(None),
            )
            .order_by(SessionCompaction.created_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if open_row is not None:
        await connection.execute(
            update(SessionCompaction)
            .where(SessionCompaction.org_id == org_id, SessionCompaction.id == open_row)
            .values(
                trigger=trigger, tokens_before=before, tokens_after=after, completed_at=func.now()
            )
        )
    else:
        await connection.execute(
            insert(SessionCompaction).values(
                org_id=org_id,
                session_id=session_id,
                run_id=run_id,
                trigger=trigger,
                tokens_before=before,
                tokens_after=after,
                completed_at=func.now(),
            )
        )
    await forget_injections(connection, org_id, session_id)
    await connection.execute(
        insert(AuditEntry).values(
            org_id=org_id,
            workspace_id=workspace_id,
            actor_kind="system",
            action="context.compacted",
            target_type="session",
            target_id=str(session_id),
            run_id=run_id,
            details={"trigger": trigger, "tokens_before": before, "tokens_after": after},
        )
    )


async def list_compactions(
    connection: AsyncConnection, org_id: uuid.UUID, session_id: uuid.UUID, limit: int = 50
) -> list[Any]:
    """The compactions of a session, newest first."""
    rows = await connection.execute(
        select(
            SessionCompaction.id,
            SessionCompaction.run_id,
            SessionCompaction.trigger,
            SessionCompaction.tokens_before,
            SessionCompaction.tokens_after,
            SessionCompaction.archive_session_id,
            SessionCompaction.archive_entries,
            SessionCompaction.archive_truncated,
            SessionCompaction.created_at,
            SessionCompaction.completed_at,
        )
        .where(SessionCompaction.org_id == org_id, SessionCompaction.session_id == session_id)
        .order_by(SessionCompaction.created_at.desc(), SessionCompaction.id)
        .limit(limit)
    )
    return list(rows)
