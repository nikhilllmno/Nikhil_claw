"""Change proposals for profiles, toolsets, and policy (docs/architecture.md, Sections 9.4.2
and 11.4).

``review_proposals`` gets the kinds ``profile``, ``toolsets``, and ``policy``: the agent proposes
them with ``change_propose``, and a person approves them on an approval card.

``profile_changes`` is the history of the changes of an agent profile: approved proposals, edits
in the web app, and reverts. ``before`` and ``after`` hold the changed fields only. A member reads
the history of a profile that the member can see, like ``agent_profiles``. Only the control plane
writes the table.

``sessions.profile_snapshot`` holds the toolsets and the profile policy rules when a session
froze its config. The gate uses it so that a new permission applies from the next session.

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS = "kind IN ('memory', 'skill', 'soul')"
NEW_KINDS = "kind IN ('memory', 'skill', 'soul', 'profile', 'toolsets', 'policy')"
VISIBLE = (
    "sama_private.is_org_member(org_id) AND (workspace_id IS NULL"
    " OR sama_private.can_see_workspace(org_id, workspace_id))"
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_review_proposals_kind"), "review_proposals", type_="check")
    op.create_check_constraint(op.f("ck_review_proposals_kind"), "review_proposals", NEW_KINDS)

    op.add_column(
        "sessions",
        sa.Column("profile_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    op.create_table(
        "profile_changes",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("agent_profile_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.UUID(), nullable=True),
        sa.Column("reverts_change_id", sa.UUID(), nullable=True),
        sa.Column("reverted_by_change_id", sa.UUID(), nullable=True),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("redacted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "before",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "after",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('soul', 'profile', 'toolsets', 'policy')",
            name=op.f("ck_profile_changes_kind"),
        ),
        sa.CheckConstraint(
            "source IN ('proposal', 'edit', 'revert')", name=op.f("ck_profile_changes_source")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(before) = 'object'", name=op.f("ck_profile_changes_before_is_object")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(after) = 'object'", name=op.f("ck_profile_changes_after_is_object")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            name=op.f("fk_profile_changes_org_id_agent_profile_id_agent_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_profile_changes_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "actor_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_profile_changes_org_id_actor_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_profile_changes_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profile_changes")),
    )
    op.create_index(
        op.f("ix_profile_changes_org_id_agent_profile_id_created_at"),
        "profile_changes",
        ["org_id", "agent_profile_id", "created_at"],
        unique=False,
    )
    op.execute("ALTER TABLE public.profile_changes ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.profile_changes FROM PUBLIC, anon, authenticated")
    op.execute("GRANT SELECT ON TABLE public.profile_changes TO authenticated")
    op.execute(
        "CREATE POLICY profile_changes_select ON public.profile_changes FOR SELECT"
        f" TO authenticated USING ({VISIBLE})"
    )


def downgrade() -> None:
    # The old code does not know the new kinds. Their approvals are removed with the proposals.
    op.execute(
        "DELETE FROM public.approvals WHERE id IN (SELECT approval_id FROM"
        " public.review_proposals WHERE kind IN ('profile', 'toolsets', 'policy'))"
    )
    op.drop_constraint(op.f("ck_review_proposals_kind"), "review_proposals", type_="check")
    op.create_check_constraint(op.f("ck_review_proposals_kind"), "review_proposals", OLD_KINDS)
    op.drop_index(
        op.f("ix_profile_changes_org_id_agent_profile_id_created_at"), table_name="profile_changes"
    )
    op.drop_table("profile_changes")
    op.drop_column("sessions", "profile_snapshot")
