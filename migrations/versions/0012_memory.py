"""Memory: notes of the agent, the user, and the workspace, and their history
(docs/architecture.md, Section 8).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_TOOLSETS = '["core", "todo", "web", "clarify", "github"]'
NEW_TOOLSETS = '["core", "todo", "web", "clarify", "github", "memory"]'

# A member reads the notes of a workspace that the member can see (workspace notes and the
# notes of an agent profile in it). Only the user reads the user profile. The revision policy
# asks the memories table, so it runs with the RLS of that table. No policy allows writes: the
# control plane writes as the table owner.
MEMORY_POLICY = (
    "sama_private.is_org_member(org_id) AND CASE scope"
    " WHEN 'user' THEN user_id IN (SELECT sama_private.my_user_ids())"
    " ELSE sama_private.can_see_workspace(org_id, workspace_id) END"
)
REVISION_POLICY = (
    "EXISTS (SELECT 1 FROM public.memories AS m"
    " WHERE m.org_id = memory_revisions.org_id AND m.id = memory_revisions.memory_id)"
)


def upgrade() -> None:
    op.create_table(
        "memories",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("agent_profile_id", sa.UUID(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column(
            "entries",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
            "scope IN ('agent', 'user', 'workspace')", name=op.f("ck_memories_scope")
        ),
        sa.CheckConstraint(
            "(scope = 'agent' AND workspace_id IS NOT NULL AND agent_profile_id IS NOT NULL"
            " AND user_id IS NULL)"
            " OR (scope = 'user' AND user_id IS NOT NULL AND workspace_id IS NULL"
            " AND agent_profile_id IS NULL)"
            " OR (scope = 'workspace' AND workspace_id IS NOT NULL AND agent_profile_id IS NULL"
            " AND user_id IS NULL)",
            name=op.f("ck_memories_scope_keys"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(entries) = 'array'", name=op.f("ck_memories_entries_array")
        ),
        sa.CheckConstraint("revision >= 0", name=op.f("ck_memories_revision")),
        sa.ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            name=op.f("fk_memories_org_id_agent_profile_id_agent_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_memories_org_id_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_memories_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_memories_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memories")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_memories_org_id_id")),
        sa.UniqueConstraint(
            "org_id",
            "scope",
            "workspace_id",
            "agent_profile_id",
            "user_id",
            name=op.f("uq_memories_org_id_scope_workspace_id_agent_profile_id_user_id"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_table(
        "memory_revisions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("memory_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entries", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("author_kind", sa.Text(), nullable=False),
        sa.Column("author_user_id", sa.UUID(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("reverted_to", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('add', 'patch', 'remove', 'batch', 'edit', 'revert')",
            name=op.f("ck_memory_revisions_action"),
        ),
        sa.CheckConstraint(
            "author_kind IN ('user', 'agent')", name=op.f("ck_memory_revisions_author_kind")
        ),
        sa.CheckConstraint(
            "author_kind <> 'user' OR author_user_id IS NOT NULL",
            name=op.f("ck_memory_revisions_user_has_id"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(entries) = 'array'", name=op.f("ck_memory_revisions_entries_array")
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_memory_revisions_seq")),
        sa.ForeignKeyConstraint(
            ["org_id", "memory_id"],
            ["memories.org_id", "memories.id"],
            name=op.f("fk_memory_revisions_org_id_memory_id_memories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_memory_revisions_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_revisions")),
        sa.UniqueConstraint(
            "org_id", "memory_id", "seq", name=op.f("uq_memory_revisions_org_id_memory_id_seq")
        ),
    )
    for table, policy in (("memories", MEMORY_POLICY), ("memory_revisions", REVISION_POLICY)):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO authenticated")
        op.execute(
            f"CREATE POLICY {table}_select ON public.{table} FOR SELECT TO authenticated"
            f" USING ({policy})"
        )
    # The default profile of the first org (M1.15) gets the new toolset.
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{NEW_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{OLD_TOOLSETS}'::jsonb"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{OLD_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{NEW_TOOLSETS}'::jsonb"
    )
    op.drop_table("memory_revisions")
    op.drop_table("memories")
