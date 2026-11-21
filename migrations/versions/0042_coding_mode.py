"""Coding mode: attached repos, plan mode, and plans (docs/architecture.md, Section 15.5;
roadmap M7.9).

- ``sessions.plan_mode``: while it is true, the tool gate refuses the tools that change files.
- ``session_repos`` holds the repositories that a person attached to a session. A ``pending``
  repository clones into the session sandbox when the next run starts.
- ``session_plans`` holds the plans that the agent proposed, and the decision of a person.

A member reads the rows of the sessions that the member can see, like ``context_pins``. Only the
control plane writes the tables.

A profile with the toolset ``github`` also gets the toolset ``plan`` (``plan_propose``). The
tool only stores a plan that a person decides, so it adds no risk. A session with a profile
snapshot gets it from the next session (Section 11.4).

Revision ID: 0042
Revises: 0041
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("session_repos", "session_plans")


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("plan_mode", sa.Boolean(), server_default=sa.false(), nullable=False),
    )

    op.create_table(
        "session_repos",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=True),
        sa.Column("folder", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("proxied", sa.Boolean(), nullable=True),
        sa.Column("commit", sa.Text(), nullable=True),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'failed')", name=op.f("ck_session_repos_status")
        ),
        sa.CheckConstraint(
            "(status = 'failed') = (error IS NOT NULL)",
            name=op.f("ck_session_repos_failed_has_error"),
        ),
        sa.CheckConstraint(
            "status <> 'ready' OR commit IS NOT NULL",
            name=op.f("ck_session_repos_ready_has_commit"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id"],
            ["sessions.org_id", "sessions.id"],
            name=op.f("fk_session_repos_org_id_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "created_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_session_repos_org_id_created_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_session_repos_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_repos")),
        sa.UniqueConstraint(
            "session_id", "folder", name=op.f("uq_session_repos_session_id_folder")
        ),
    )
    op.create_index(
        op.f("ix_session_repos_org_id_session_id"), "session_repos", ["org_id", "session_id"]
    )

    op.create_table(
        "session_plans",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("proposed_body", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="proposed", nullable=False),
        sa.Column("edited", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column("decided_by_user_id", sa.UUID(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'superseded')",
            name=op.f("ck_session_plans_status"),
        ),
        sa.CheckConstraint(
            "char_length(body) BETWEEN 1 AND 20000", name=op.f("ck_session_plans_body_length")
        ),
        sa.CheckConstraint(
            "char_length(proposed_body) BETWEEN 1 AND 20000",
            name=op.f("ck_session_plans_proposed_body_length"),
        ),
        sa.CheckConstraint(
            "feedback IS NULL OR char_length(feedback) <= 4000",
            name=op.f("ck_session_plans_feedback_length"),
        ),
        sa.CheckConstraint(
            "(status IN ('approved', 'rejected')) = (decided_at IS NOT NULL)",
            name=op.f("ck_session_plans_decided"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id"],
            ["sessions.org_id", "sessions.id"],
            name=op.f("fk_session_plans_org_id_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "decided_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_session_plans_org_id_decided_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_session_plans_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_plans")),
        sa.UniqueConstraint(
            "session_id", "version", name=op.f("uq_session_plans_session_id_version")
        ),
    )
    op.create_index(
        op.f("ix_session_plans_org_id_session_id_version"),
        "session_plans",
        ["org_id", "session_id", "version"],
    )

    op.execute(
        "UPDATE public.agent_profiles SET toolsets = toolsets || '[\"plan\"]'::jsonb"
        " WHERE toolsets ? 'github' AND NOT toolsets ? 'plan'"
    )

    # A member reads the repos and the plans of the sessions that the member can see. No policy
    # allows a write: the control plane writes as the table owner.
    for table in TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO authenticated")
        op.execute(
            f"CREATE POLICY {table}_select ON public.{table} FOR SELECT TO authenticated"
            " USING (sama_private.can_read_session(org_id, session_id))"
        )


def downgrade() -> None:
    op.execute(
        "UPDATE public.agent_profiles SET toolsets = toolsets - 'plan' WHERE toolsets ? 'plan'"
    )
    op.drop_index(op.f("ix_session_plans_org_id_session_id_version"), table_name="session_plans")
    op.drop_table("session_plans")
    op.drop_index(op.f("ix_session_repos_org_id_session_id"), table_name="session_repos")
    op.drop_table("session_repos")
    op.drop_column("sessions", "plan_mode")
