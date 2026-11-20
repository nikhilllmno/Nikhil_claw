"""Worker bots and chat proposals (docs/architecture.md, Sections 9.4 and 11.6).

``worker_bots`` binds an agent profile to one Slack channel with a daily budget. A session that
a bot answers has the id of the bot in ``sessions.worker_bot_id``, so the spend of a bot in one
day is the cost of the model calls of its sessions. The column is not a foreign key: a deleted
bot leaves its sessions.

``review_proposals`` gets the kind ``soul`` (a change of the soul of a profile) and the column
``origin``: ``review`` for a background review, ``chat`` for a change that the agent proposes in
a chat run because the user of the run does not have the role.

The new table has row level security like ``channel_groups``: a member reads the bots of a
workspace that the member can see. Only the control plane writes it.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS = "kind IN ('memory', 'skill')"
NEW_KINDS = "kind IN ('memory', 'skill', 'soul')"


def upgrade() -> None:
    op.create_table(
        "worker_bots",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("agent_profile_id", sa.UUID(), nullable=False),
        sa.Column("integration_id", sa.UUID(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("daily_budget_usd", sa.Numeric(12, 4), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
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
        sa.CheckConstraint("status IN ('active', 'disabled')", name=op.f("ck_worker_bots_status")),
        sa.CheckConstraint("daily_budget_usd > 0", name=op.f("ck_worker_bots_daily_budget_usd")),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 80", name=op.f("ck_worker_bots_name")),
        sa.ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            name=op.f("fk_worker_bots_org_id_agent_profile_id_agent_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "created_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_worker_bots_org_id_created_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "integration_id"],
            ["integrations.org_id", "integrations.id"],
            name=op.f("fk_worker_bots_org_id_integration_id_integrations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_worker_bots_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_worker_bots_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worker_bots")),
        sa.UniqueConstraint(
            "integration_id", "external_id", name=op.f("uq_worker_bots_integration_id_external_id")
        ),
    )
    op.create_index(
        op.f("ix_worker_bots_org_id_workspace_id"),
        "worker_bots",
        ["org_id", "workspace_id"],
        unique=False,
    )
    op.execute("ALTER TABLE public.worker_bots ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.worker_bots FROM PUBLIC, anon, authenticated")
    op.execute("GRANT SELECT ON TABLE public.worker_bots TO authenticated")
    op.execute(
        "CREATE POLICY worker_bots_select ON public.worker_bots FOR SELECT TO authenticated"
        " USING (sama_private.can_see_workspace(org_id, workspace_id))"
    )

    op.add_column("sessions", sa.Column("worker_bot_id", sa.UUID(), nullable=True))
    op.create_index(
        op.f("ix_sessions_org_id_worker_bot_id"),
        "sessions",
        ["org_id", "worker_bot_id"],
        unique=False,
        postgresql_where=sa.text("worker_bot_id IS NOT NULL"),
    )

    op.add_column(
        "review_proposals",
        sa.Column("origin", sa.Text(), server_default=sa.text("'review'"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_review_proposals_origin"), "review_proposals", "origin IN ('review', 'chat')"
    )
    op.drop_constraint(op.f("ck_review_proposals_kind"), "review_proposals", type_="check")
    op.create_check_constraint(op.f("ck_review_proposals_kind"), "review_proposals", NEW_KINDS)


def downgrade() -> None:
    # The old schema has no soul proposal and no chat proposal. Their approvals stay, and the
    # applier of the old code does not know them, so they are removed with the proposals.
    op.execute(
        "DELETE FROM public.approvals WHERE id IN (SELECT approval_id FROM"
        " public.review_proposals WHERE kind = 'soul' OR origin = 'chat')"
    )
    op.drop_constraint(op.f("ck_review_proposals_kind"), "review_proposals", type_="check")
    op.create_check_constraint(op.f("ck_review_proposals_kind"), "review_proposals", OLD_KINDS)
    op.drop_constraint(op.f("ck_review_proposals_origin"), "review_proposals", type_="check")
    op.drop_column("review_proposals", "origin")
    op.drop_index(op.f("ix_sessions_org_id_worker_bot_id"), table_name="sessions")
    op.drop_column("sessions", "worker_bot_id")
    op.drop_index(op.f("ix_worker_bots_org_id_workspace_id"), table_name="worker_bots")
    op.drop_table("worker_bots")
