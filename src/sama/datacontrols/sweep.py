"""The retention sweep (docs/architecture.md, Section 17.4).

For each org with a retention (``effective_days``), the sweep deletes the sessions with no
activity in that time. The last activity of a session is the latest of its ``updated_at``,
its last message, and the start or the end of its last run. A session stays when:

- a run of it has not ended;
- a scheduled job posts to it (``jobs.session_id``);
- a ``compaction`` or ``delegation`` child of it has newer activity.

The sweep deletes one session (with its children) in each transaction, with
``delete_sessions``. A lock wait or a running run skips the session until the next sweep. One
pass deletes at most ``MAX_PER_ORG`` sessions in an org. A session with more children than
that limit goes alone, in a pass of its own. The audit log, the org and user rows, memory,
skills, and the other settings stay.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import bindparam, insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.datacontrols.delete import (
    UUIDS,
    busy,
    delete_sessions,
    lock_timeout,
    session_closure,
)
from sama.datacontrols.retention import effective_days
from sama.db.models import AuditEntry, Org, Session

log: Final = logging.getLogger("sama.datacontrols.sweep")

BATCH: Final = 50
"""The candidates of one query."""
MAX_PER_ORG: Final = 500
"""The most sessions that one sweep deletes in one org. The next sweep does the rest."""
_STALE: Final = """
    s.updated_at < :cutoff
    AND NOT EXISTS (
        SELECT 1 FROM messages m
        WHERE m.org_id = s.org_id AND m.session_id = s.id AND m.created_at >= :cutoff)
    AND NOT EXISTS (
        SELECT 1 FROM runs r
        WHERE r.org_id = s.org_id AND r.session_id = s.id
          AND (r.status NOT IN ('completed', 'failed', 'cancelled')
               OR COALESCE(r.finished_at, r.created_at) >= :cutoff))
    AND NOT EXISTS (
        SELECT 1 FROM jobs j WHERE j.org_id = s.org_id AND j.session_id = s.id)
"""
"""True for a session ``s`` with no activity since ``:cutoff``. The statuses are
``ENDED_RUN_STATUSES``."""

# The queries join constant SQL only. The values are bound parameters.
_CANDIDATES: Final = text(
    "SELECT s.id FROM sessions s"  # noqa: S608
    " WHERE s.org_id = :org"
    " AND (s.lineage IS NULL OR s.lineage IN ('branch', 'retry'))"
    " AND NOT (s.id = ANY(:skip))"
    " AND " + _STALE + " ORDER BY s.created_at, s.id LIMIT :limit"
).bindparams(bindparam("skip", type_=UUIDS))

_FRESH: Final = text(
    "SELECT count(*) FROM sessions s WHERE s.org_id = :org AND s.id = ANY(:ids)"  # noqa: S608
    + " AND NOT ("
    + _STALE
    + ")"
).bindparams(bindparam("ids", type_=UUIDS))


class _OverBudget(Exception):
    """The session and its children are more than the sessions that the pass can delete."""


class RetentionSweep:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def serve(self, stop: asyncio.Event, *, interval_s: float = 3600.0) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                log.exception("the retention sweep failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_s)

    async def run_once(self, *, now: datetime | None = None) -> int:
        """Sweeps every org with a retention. Returns how many sessions it deleted."""
        now = now or datetime.now(UTC)
        async with self._engine.connect() as connection:
            orgs = (await connection.execute(select(Org.id, Org.plan, Org.retention_days))).all()
        total = 0
        for org in orgs:
            days = effective_days(org.plan, org.retention_days)
            if days is None:
                continue
            try:
                total += await self.sweep_org(org.id, now - timedelta(days=days))
            except Exception:
                log.exception("the retention sweep of org %s failed", org.id)
        return total

    async def sweep_org(self, org_id: uuid.UUID, cutoff: datetime) -> int:
        """Deletes the sessions of the org with no activity since ``cutoff``."""
        skip: list[uuid.UUID] = []
        deleted = 0
        runs = 0
        full = False
        while not full:
            async with self._engine.connect() as connection:
                roots = list(
                    (
                        await connection.execute(
                            _CANDIDATES,
                            {"org": org_id, "cutoff": cutoff, "skip": skip, "limit": BATCH},
                        )
                    ).scalars()
                )
            if not roots:
                break
            for root in roots:
                # The first delete of a pass has no limit: a session goes with all its children.
                budget = MAX_PER_ORG - deleted if deleted else None
                try:
                    removed = await self._delete(org_id, root, cutoff, budget)
                except _OverBudget:
                    full = True
                    break
                if removed is None:
                    skip.append(root)
                    continue
                deleted += removed[0]
                runs += removed[1]
                full = deleted >= MAX_PER_ORG
                if full:
                    break
        if deleted:
            async with self._engine.begin() as connection:
                await connection.execute(
                    insert(AuditEntry).values(
                        org_id=org_id,
                        actor_kind="system",
                        action="data.retention",
                        target_type="org",
                        target_id=str(org_id),
                        details={
                            "sessions": deleted,
                            "runs": runs,
                            "cutoff": cutoff.isoformat(),
                        },
                    )
                )
        return deleted

    async def _delete(
        self, org_id: uuid.UUID, root: uuid.UUID, cutoff: datetime, budget: int | None
    ) -> tuple[int, int] | None:
        """Deletes one root session and its children. None: the session stays for now.
        Raises ``_OverBudget`` when the closure has more than ``budget`` sessions."""
        try:
            async with self._engine.begin() as connection:
                await lock_timeout(connection)
                closure = await session_closure(connection, org_id, [root])
                if not closure:
                    return None
                if budget is not None and len(closure) > budget:
                    raise _OverBudget
                # The lock comes before the check: a new message or run of a locked session
                # waits for the delete, then fails.
                await connection.execute(
                    select(Session.id)
                    .where(Session.org_id == org_id, Session.id.in_(closure))
                    .with_for_update()
                )
                if await _fresh(connection, org_id, closure, cutoff):
                    await connection.rollback()
                    return None
                removed = await delete_sessions(connection, org_id, closure)
                if removed.running:
                    await connection.rollback()
                    return None
                return len(removed.sessions), removed.runs
        except DBAPIError as error:
            if busy(error):
                return None
            raise


async def _fresh(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    ids: Sequence[uuid.UUID],
    cutoff: datetime,
) -> bool:
    count = (
        await connection.execute(_FRESH, {"org": org_id, "ids": list(ids), "cutoff": cutoff})
    ).scalar_one()
    return bool(count)
