"""The run queue: leases, cancel requests, approval resumes, and the session sandbox
(docs/architecture.md, Section 4.3).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("lease_owner", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("attempts", sa.Integer(), server_default="0", nullable=False))
    op.add_column(
        "runs", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("runs", sa.Column("pending_approval_id", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("resume_decision", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("resume_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_runs_resume_decision"),
        "runs",
        "resume_decision IS NULL OR resume_decision IN ('approve', 'reject')",
    )
    op.create_check_constraint(
        op.f("ck_runs_lease"),
        "runs",
        "(status = 'running') = (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_runs_paused_has_approval"),
        "runs",
        "status <> 'paused' OR pending_approval_id IS NOT NULL",
    )
    op.create_index(
        "ix_runs_one_active_per_session",
        "runs",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('running', 'paused')"),
    )
    op.create_index(op.f("ix_runs_status_created_at"), "runs", ["status", "created_at"])
    op.add_column("sessions", sa.Column("sandbox_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "sandbox_id")
    op.drop_index(op.f("ix_runs_status_created_at"), table_name="runs")
    op.drop_index("ix_runs_one_active_per_session", table_name="runs")
    op.drop_constraint(op.f("ck_runs_paused_has_approval"), "runs", type_="check")
    op.drop_constraint(op.f("ck_runs_lease"), "runs", type_="check")
    op.drop_constraint(op.f("ck_runs_resume_decision"), "runs", type_="check")
    for column in (
        "resume_reason",
        "resume_decision",
        "pending_approval_id",
        "cancel_requested_at",
        "attempts",
        "lease_expires_at",
        "lease_owner",
    ):
        op.drop_column("runs", column)
