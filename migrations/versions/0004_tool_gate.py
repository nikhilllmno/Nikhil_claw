"""The tool gate: approvals, policy rules, and unattended runs (docs/architecture.md,
Section 16.1).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICIES = {
    "approvals": "sama_private.can_read_session(org_id, session_id)",
    # A member reads the org and profile rules and their own rules. An admin reads all.
    "tool_policies": (
        "sama_private.is_org_member(org_id) AND (user_id IS NULL"
        " OR user_id IN (SELECT sama_private.my_user_ids())"
        " OR sama_private.is_org_admin(org_id))"
    ),
}


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("unattended", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("tool_calls", sa.Column("arguments_digest", sa.Text(), nullable=True))
    op.create_table(
        "approvals",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("call_id", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column(
            "arguments",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("arguments_digest", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("decided_by_user_id", sa.UUID(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'pending') = (decided_at IS NULL)", name=op.f("ck_approvals_decided_at")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired')",
            name=op.f("ck_approvals_status"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "decided_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_approvals_org_id_decided_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            name=op.f("fk_approvals_org_id_session_id_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id"],
            ["sessions.org_id", "sessions.id"],
            name=op.f("fk_approvals_org_id_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_approvals_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
        sa.UniqueConstraint("run_id", "call_id", name=op.f("uq_approvals_run_id_call_id")),
    )
    op.create_index(op.f("ix_approvals_org_id_session_id"), "approvals", ["org_id", "session_id"])
    op.create_index(op.f("ix_approvals_status_expires_at"), "approvals", ["status", "expires_at"])
    op.create_table(
        "tool_policies",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("agent_profile_id", sa.UUID(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('allow', 'ask', 'deny')", name=op.f("ck_tool_policies_action")
        ),
        sa.CheckConstraint(
            "length(tool) BETWEEN 1 AND 200", name=op.f("ck_tool_policies_tool_length")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            name=op.f("fk_tool_policies_org_id_agent_profile_id_agent_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_tool_policies_org_id_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_tool_policies_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_policies")),
        sa.UniqueConstraint(
            "org_id",
            "agent_profile_id",
            "user_id",
            "tool",
            name=op.f("uq_tool_policies_org_id_agent_profile_id_user_id_tool"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    # Row-level security as in 0001. No policy allows writes: the control plane writes as the
    # table owner.
    for table, using in POLICIES.items():
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO authenticated")
        op.execute(
            f"CREATE POLICY {table}_select ON public.{table} FOR SELECT TO authenticated"
            f" USING ({using})"
        )


def downgrade() -> None:
    op.drop_table("tool_policies")
    op.drop_table("approvals")
    op.drop_column("tool_calls", "arguments_digest")
    op.drop_column("runs", "unattended")
