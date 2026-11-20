"""Background review: run kind, review counters, review proposals (docs/architecture.md,
Section 9.4).

A review is a run of the kind ``review``. A session counts the user turns and the tool calls
since its last review. Two partial unique indexes keep at most one waiting review per session;
the index of the active run keeps at most one running review. A review proposes a change of
shared memory or of a shared skill in ``review_proposals``. The change waits for the approval
of a person.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_profiles",
        sa.Column("background_review", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column("agent_profiles", sa.Column("review_model", sa.Text(), nullable=True))
    for column in ("review_user_turns", "review_tool_iterations"):
        op.add_column(
            "sessions",
            sa.Column(column, sa.Integer(), server_default="0", nullable=False),
        )
    op.add_column(
        "sessions",
        sa.Column("review_cursor_seq", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column("runs", sa.Column("kind", sa.Text(), server_default="chat", nullable=False))
    op.add_column("runs", sa.Column("review_from_seq", sa.BigInteger(), nullable=True))
    op.add_column("runs", sa.Column("review_to_seq", sa.BigInteger(), nullable=True))
    op.create_check_constraint(op.f("ck_runs_kind"), "runs", "kind IN ('chat', 'review')")
    op.create_check_constraint(
        op.f("ck_runs_review_range"),
        "runs",
        "(kind = 'review') = (review_from_seq IS NOT NULL AND review_to_seq IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_runs_review_unattended"), "runs", "kind <> 'review' OR unattended"
    )
    op.create_index(
        "ix_runs_one_queued_review_per_session",
        "runs",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'review' AND status = 'queued'"),
    )
    op.create_unique_constraint(op.f("uq_approvals_org_id_id"), "approvals", ["org_id", "id"])

    op.create_table(
        "review_proposals",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("approval_id", sa.UUID(), nullable=False),
        sa.Column("acting_user_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('memory', 'skill')", name=op.f("ck_review_proposals_kind")),
        sa.CheckConstraint(
            "scope IN ('agent', 'workspace', 'org')", name=op.f("ck_review_proposals_scope")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'applied', 'failed', 'discarded')",
            name=op.f("ck_review_proposals_status"),
        ),
        sa.CheckConstraint(
            "(status = 'applied') = (applied_at IS NOT NULL)",
            name=op.f("ck_review_proposals_applied_at"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            name=op.f("fk_review_proposals_org_id_session_id_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "approval_id"],
            ["approvals.org_id", "approvals.id"],
            name=op.f("fk_review_proposals_org_id_approval_id_approvals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "acting_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_review_proposals_org_id_acting_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_review_proposals_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_proposals")),
        sa.UniqueConstraint("approval_id", name=op.f("uq_review_proposals_approval_id")),
    )
    op.create_index(
        op.f("ix_review_proposals_org_id_session_id"),
        "review_proposals",
        ["org_id", "session_id"],
    )
    op.create_index(
        "ix_review_proposals_pending",
        "review_proposals",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    # A member reads the proposals of the sessions that the member can see. No policy allows a
    # write: the control plane writes as the table owner.
    op.execute("ALTER TABLE public.review_proposals ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.review_proposals FROM PUBLIC, anon, authenticated")
    op.execute("GRANT SELECT ON TABLE public.review_proposals TO authenticated")
    op.execute(
        "CREATE POLICY review_proposals_select ON public.review_proposals FOR SELECT"
        " TO authenticated USING (sama_private.can_read_session(org_id, session_id))"
    )


def downgrade() -> None:
    op.drop_table("review_proposals")
    # The old schema has no run kind. Old code would start a waiting review as a chat run, so
    # no review stays active. A review run is not deleted: the rows of ``model_calls`` of a
    # run go with the run (``ON DELETE CASCADE``), and the budgets of the org and the user are
    # the sum of these rows. The old code reads the run as an ended chat run without a message.
    op.execute("DELETE FROM public.approvals WHERE status = 'pending' AND call_id LIKE 'review:%'")
    op.execute(
        "UPDATE public.runs SET status = 'cancelled', failure_code = 'cancelled',"
        " failure_message = 'the background review was removed by a downgrade',"
        " lease_owner = NULL, lease_expires_at = NULL, pending_approval_id = NULL,"
        " finished_at = COALESCE(finished_at, now())"
        " WHERE kind = 'review' AND status IN ('queued', 'running', 'paused')"
    )
    # The old queue resumes a session from the newest completed run that has a runtime state,
    # and it cannot tell a review run from a chat run. So no review run keeps a state.
    op.execute("UPDATE public.runs SET runtime_state = NULL WHERE kind = 'review'")
    op.drop_constraint(op.f("uq_approvals_org_id_id"), "approvals", type_="unique")
    op.drop_index("ix_runs_one_queued_review_per_session", table_name="runs")
    op.drop_constraint(op.f("ck_runs_review_unattended"), "runs", type_="check")
    op.drop_constraint(op.f("ck_runs_review_range"), "runs", type_="check")
    op.drop_constraint(op.f("ck_runs_kind"), "runs", type_="check")
    op.drop_column("runs", "review_to_seq")
    op.drop_column("runs", "review_from_seq")
    op.drop_column("runs", "kind")
    op.drop_column("sessions", "review_cursor_seq")
    op.drop_column("sessions", "review_tool_iterations")
    op.drop_column("sessions", "review_user_turns")
    op.drop_column("agent_profiles", "review_model")
    op.drop_column("agent_profiles", "background_review")
