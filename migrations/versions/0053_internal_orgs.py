"""Internal orgs and the ``run.completed`` backfill for the product metrics
(docs/architecture.md, Section 18.4).

``orgs.is_internal`` marks an org of the operator team or of test traffic. The product metrics
(activation and retention) leave these orgs out. The default is false: a customer org from the
self-serve sign-up is not internal.

The backfill marks each org without ``created_by_auth_user_id`` as internal. Only
``python -m sama.admin bootstrap`` makes such an org (Section 19.4), and only an operator runs
it. A sign-up org always has the column (Section 17.2). An operator changes the flag with
``python -m sama.admin internal set``.

Since this change, each completed run writes the audit entry ``run.completed``. The upgrade
writes the same entry, with ``via`` = ``migration 0053`` and ``created_at`` = the end of the
run, for each completed run that does not have one. Thus the metrics also count the runs from
before. The details have flags and ids only, no message text. ``audit_log`` is append-only, so
the downgrade keeps these entries. A second upgrade adds no copy.

The column is in the table ``orgs``, which has its RLS policy already. A new policy is not
needed: a member reads the column with the rest of the org row, and only the control plane
writes it.

Revision ID: 0053
Revises: 0052
Create Date: 2026-11-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RUN_COMPLETED_BACKFILL = """
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
           'unattended', r.unattended, 'via', 'migration 0053'
       ),
       coalesce(r.finished_at, r.created_at)
FROM runs r
JOIN sessions s ON s.org_id = r.org_id AND s.id = r.session_id
WHERE r.status = 'completed'
  AND NOT EXISTS (
      SELECT 1 FROM audit_log a
      WHERE a.org_id = r.org_id AND a.run_id = r.id AND a.action = 'run.completed'
  )
"""


def upgrade() -> None:
    op.add_column(
        "orgs",
        sa.Column("is_internal", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.execute("UPDATE orgs SET is_internal = true WHERE created_by_auth_user_id IS NULL")
    op.execute(RUN_COMPLETED_BACKFILL)


def downgrade() -> None:
    # The ``run.completed`` entries stay: ``audit_log`` is append-only.
    op.drop_column("orgs", "is_internal")
