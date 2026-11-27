"""The ``run.completed`` backfill for the product metrics (docs/architecture.md, Section 18.4).

Since migration ``0053``, ``RunQueue.complete`` writes the audit row ``run.completed`` for each
completed run. This backfill writes the same row for the completed runs that do not have one:
the runs from before the change, and the runs that an old worker completed during the deploy.
Run it after the deploy, when no worker of the old version is left:
``python -m sama.admin backfill-run-completed``.

Each batch is a short transaction of its own, so the backfill never holds a long lock. A second
run adds no copy. The details have flags and ids only, no message text.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

DEFAULT_BATCH: Final = 500
VIA: Final = "backfill"

_BATCH: Final = text(
    """
INSERT INTO audit_log (
    org_id, workspace_id, actor_kind, actor_user_id, action, target_type, target_id, run_id,
    details, created_at
)
SELECT r.org_id, s.workspace_id,
       CASE WHEN r.triggered_by_user_id IS NULL THEN 'system' ELSE 'user' END,
       r.triggered_by_user_id, 'run.completed', 'run', r.id::text, r.id,
       jsonb_build_object(
           'session_id', r.session_id::text, 'kind', r.kind, 'compact', r.compact,
           'side', r.side, 'runtime', r.runtime, 'human_origin', r.human_origin,
           'unattended', r.unattended, 'via', CAST(:via AS text)
       ),
       coalesce(r.finished_at, r.created_at)
FROM runs r
JOIN sessions s ON s.org_id = r.org_id AND s.id = r.session_id
WHERE r.status = 'completed'
  AND NOT EXISTS (
      SELECT 1 FROM audit_log a
      WHERE a.org_id = r.org_id AND a.run_id = r.id AND a.action = 'run.completed'
  )
ORDER BY r.finished_at NULLS FIRST, r.id
LIMIT :batch
"""
)


async def backfill_run_completed(engine: AsyncEngine, *, batch: int = DEFAULT_BATCH) -> int:
    """Writes ``run.completed`` for each completed run without it. Returns how many rows it
    wrote. ``created_at`` of a row is the end of its run."""
    if batch < 1:
        raise ValueError("the batch must be 1 or more")
    total = 0
    while True:
        async with engine.begin() as connection:
            written = (await connection.execute(_BATCH, {"via": VIA, "batch": batch})).rowcount
        total += written
        if written < batch:
            return total
