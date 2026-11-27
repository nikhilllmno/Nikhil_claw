"""Internal orgs for the product metrics (docs/architecture.md, Section 18.4).

``orgs.is_internal`` marks an org of the operator team or of test traffic. The product metrics
(activation and retention) leave these orgs out. The default is false: a customer org from the
self-serve sign-up is not internal.

The backfill marks each org without ``created_by_auth_user_id`` as internal. Only
``python -m sama.admin bootstrap`` makes such an org (Section 19.4), and only an operator runs
it. A sign-up org always has the column (Section 17.2). An operator changes the flag with
``python -m sama.admin internal set``. The table ``orgs`` is small, so the lock of the change is
short.

The ``run.completed`` audit rows of the runs from before are not in this migration. The
operator writes them after the deploy with ``python -m sama.admin backfill-run-completed``, in
short batches, when no old worker is left (``sama.observability.activation``).

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


def upgrade() -> None:
    op.add_column(
        "orgs",
        sa.Column("is_internal", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.execute("UPDATE orgs SET is_internal = true WHERE created_by_auth_user_id IS NULL")


def downgrade() -> None:
    op.drop_column("orgs", "is_internal")
