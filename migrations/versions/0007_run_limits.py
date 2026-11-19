"""Indexes for the per-org run limits of the queue (docs/architecture.md, Section 4.3).

The message rate counts the runs of an org in the last 60 seconds. The concurrency limit
counts the running runs of an org.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(op.f("ix_runs_org_id_created_at"), "runs", ["org_id", "created_at"])
    op.create_index(
        "ix_runs_running_per_org",
        "runs",
        ["org_id"],
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("ix_runs_running_per_org", table_name="runs")
    op.drop_index(op.f("ix_runs_org_id_created_at"), table_name="runs")
