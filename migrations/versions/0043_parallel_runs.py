"""Busy modes and parallel runs (docs/architecture.md, Section 4.3; roadmap M7.1).

- ``agent_profiles.busy_mode`` is what a new message does while a run of its session is open:
  ``steer`` (the default: the message joins the open run at its next step), ``parallel`` (the
  message starts a second run at the same time), or ``queue`` (the message waits).
- ``agent_profiles.max_parallel_runs`` is the most open chat runs of one session (1 to 10).
- ``runs.allow_parallel`` is true for a run that can start next to an open run of its session:
  the profile was in the mode ``parallel`` at the enqueue, or the person asked for it (``/bg``).
- ``runs.side`` is true for a run that started while another chat run of its session was open.
  A side run starts a new runtime session and does not give its runtime state to the session.
- ``runs.context_seq`` is the newest ``messages.seq`` that the runtime state of a run has seen,
  and ``runs.state_seq`` is that value of the state that the run resumed.

The unique index ``ix_runs_one_active_per_session`` becomes ``ix_runs_one_main_per_session``:
a session has at most one open run that is not a side run. No table is added, so no RLS policy
changes.

Revision ID: 0043
Revises: 0042
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_profiles",
        sa.Column("busy_mode", sa.Text(), server_default="steer", nullable=False),
    )
    op.add_column(
        "agent_profiles",
        sa.Column("max_parallel_runs", sa.Integer(), server_default="3", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_agent_profiles_busy_mode"),
        "agent_profiles",
        "busy_mode IN ('steer', 'parallel', 'queue')",
    )
    op.create_check_constraint(
        op.f("ck_agent_profiles_max_parallel_runs"),
        "agent_profiles",
        "max_parallel_runs BETWEEN 1 AND 10",
    )
    op.add_column(
        "runs",
        sa.Column("allow_parallel", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "runs", sa.Column("side", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.add_column("runs", sa.Column("context_seq", sa.BigInteger(), nullable=True))
    op.add_column("runs", sa.Column("state_seq", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        op.f("ck_runs_side_is_chat"), "runs", "NOT side OR (kind = 'chat' AND NOT compact)"
    )
    op.drop_index("ix_runs_one_active_per_session", table_name="runs")
    op.create_index(
        "ix_runs_one_main_per_session",
        "runs",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('running', 'paused') AND NOT side"),
    )
    op.create_index(
        "ix_runs_active_per_session",
        "runs",
        ["session_id"],
        postgresql_where=sa.text("status IN ('running', 'paused')"),
    )


def downgrade() -> None:
    # Before the downgrade, end the side runs that are not ended (the old index allows one open
    # run per session), and drop the runtime state of every side run: the old state query has
    # no side flag, so a side state could become the conversation of the session.
    op.execute(
        "UPDATE runs SET status = 'failed', failure_code = 'internal',"
        " failure_message = 'the downgrade ended the parallel run', lease_owner = NULL,"
        " lease_expires_at = NULL, pending_approval_id = NULL, resume_decision = NULL,"
        " resume_reason = NULL, finished_at = now()"
        " WHERE side AND status IN ('queued', 'running', 'paused')"
    )
    op.execute("UPDATE runs SET runtime_state = NULL WHERE side")
    op.drop_index("ix_runs_active_per_session", table_name="runs")
    op.drop_index("ix_runs_one_main_per_session", table_name="runs")
    op.create_index(
        "ix_runs_one_active_per_session",
        "runs",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('running', 'paused')"),
    )
    op.drop_constraint(op.f("ck_runs_side_is_chat"), "runs", type_="check")
    op.drop_column("runs", "state_seq")
    op.drop_column("runs", "context_seq")
    op.drop_column("runs", "side")
    op.drop_column("runs", "allow_parallel")
    op.drop_constraint(op.f("ck_agent_profiles_max_parallel_runs"), "agent_profiles", type_="check")
    op.drop_constraint(op.f("ck_agent_profiles_busy_mode"), "agent_profiles", type_="check")
    op.drop_column("agent_profiles", "max_parallel_runs")
    op.drop_column("agent_profiles", "busy_mode")
