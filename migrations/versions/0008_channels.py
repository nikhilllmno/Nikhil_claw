"""Channels: linked chat accounts, linked group chats, and pairing codes
(docs/architecture.md, Section 11.3).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A user reads the own linked accounts and pairing codes. A member reads the groups of the
# workspaces that the member can see. No policy allows writes: the control plane writes as
# the table owner.
POLICIES = {
    "channel_identities": "user_id IN (SELECT sama_private.my_user_ids())",
    "channel_groups": "sama_private.can_see_workspace(org_id, workspace_id)",
    "pairing_codes": "user_id IN (SELECT sama_private.my_user_ids())",
}


def upgrade() -> None:
    op.create_table(
        "channel_groups",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("agent_profile_id", sa.UUID(), nullable=False),
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
            "channel IN ('web', 'slack', 'telegram', 'extension')",
            name=op.f("ck_channel_groups_channel"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            name=op.f("fk_channel_groups_org_id_agent_profile_id_agent_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "created_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_channel_groups_org_id_created_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_channel_groups_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_channel_groups_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_groups")),
        sa.UniqueConstraint(
            "channel", "external_id", name=op.f("uq_channel_groups_channel_external_id")
        ),
    )
    op.create_index(
        op.f("ix_channel_groups_org_id_workspace_id"),
        "channel_groups",
        ["org_id", "workspace_id"],
        unique=False,
    )
    op.create_table(
        "channel_identities",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("agent_profile_id", sa.UUID(), nullable=False),
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
            "channel IN ('web', 'slack', 'telegram', 'extension')",
            name=op.f("ck_channel_identities_channel"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            name=op.f("fk_channel_identities_org_id_agent_profile_id_agent_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_channel_identities_org_id_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_channel_identities_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_channel_identities_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_identities")),
        sa.UniqueConstraint(
            "channel", "external_id", name=op.f("uq_channel_identities_channel_external_id")
        ),
    )
    op.create_index(
        op.f("ix_channel_identities_org_id_user_id"),
        "channel_identities",
        ["org_id", "user_id"],
        unique=False,
    )
    op.create_table(
        "pairing_codes",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("agent_profile_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "channel IN ('web', 'slack', 'telegram', 'extension')",
            name=op.f("ck_pairing_codes_channel"),
        ),
        sa.CheckConstraint("kind IN ('user', 'group')", name=op.f("ck_pairing_codes_kind")),
        sa.ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            name=op.f("fk_pairing_codes_org_id_agent_profile_id_agent_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_pairing_codes_org_id_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_pairing_codes_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_pairing_codes_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pairing_codes")),
        sa.UniqueConstraint("code_hash", name=op.f("uq_pairing_codes_code_hash")),
    )
    op.create_index(
        op.f("ix_pairing_codes_expires_at"), "pairing_codes", ["expires_at"], unique=False
    )
    op.create_index(
        op.f("ix_pairing_codes_org_id_user_id"),
        "pairing_codes",
        ["org_id", "user_id"],
        unique=False,
    )
    for table, policy in POLICIES.items():
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO authenticated")
        op.execute(
            f"CREATE POLICY {table}_select ON public.{table} FOR SELECT TO authenticated"
            f" USING ({policy})"
        )


def downgrade() -> None:
    op.drop_index(op.f("ix_pairing_codes_org_id_user_id"), table_name="pairing_codes")
    op.drop_index(op.f("ix_pairing_codes_expires_at"), table_name="pairing_codes")
    op.drop_table("pairing_codes")
    op.drop_index(op.f("ix_channel_identities_org_id_user_id"), table_name="channel_identities")
    op.drop_table("channel_identities")
    op.drop_index(op.f("ix_channel_groups_org_id_workspace_id"), table_name="channel_groups")
    op.drop_table("channel_groups")
