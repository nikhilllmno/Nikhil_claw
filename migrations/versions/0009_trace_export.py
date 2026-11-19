"""Trace export: the runs that wait for Langfuse (docs/architecture.md, Section 18.2).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PENDING = "trace_exported_at IS NULL AND status IN ('completed', 'failed', 'cancelled')"


def upgrade() -> None:
    op.add_column("runs", sa.Column("trace_exported_at", sa.DateTime(timezone=True)))
    # Runs that ended before this migration have no trace. They are not sent.
    op.execute(
        "UPDATE runs SET trace_exported_at = now() "
        "WHERE status IN ('completed', 'failed', 'cancelled')"
    )
    op.create_index(
        "ix_runs_trace_pending", "runs", ["finished_at"], postgresql_where=sa.text(PENDING)
    )


def downgrade() -> None:
    op.drop_index("ix_runs_trace_pending", table_name="runs")
    op.drop_column("runs", "trace_exported_at")
