"""Skills: versioned skills of the org, of a workspace, or of a user (docs/architecture.md,
Section 9.2).

The tools of the toolset ``skills`` would have no effect for the default profile of the first
org, so the upgrade adds the toolset to it (the same way as migration 0012).

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_TOOLSETS = '["core", "todo", "web", "clarify", "github", "memory"]'
NEW_TOOLSETS = '["core", "todo", "web", "clarify", "github", "memory", "skills"]'


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
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
        sa.CheckConstraint("current_version >= 1", name=op.f("ck_skills_current_version")),
        sa.CheckConstraint(
            "scope IN ('org', 'workspace', 'user')", name=op.f("ck_skills_scope")
        ),
        sa.CheckConstraint(
            "(scope = 'org' AND workspace_id IS NULL AND user_id IS NULL)"
            " OR (scope = 'workspace' AND workspace_id IS NOT NULL AND user_id IS NULL)"
            " OR (scope = 'user' AND workspace_id IS NULL AND user_id IS NOT NULL)",
            name=op.f("ck_skills_scope_columns"),
        ),
        sa.CheckConstraint("status IN ('active', 'archived')", name=op.f("ck_skills_status")),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_skills_org_id_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_skills_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_skills_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skills")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_skills_org_id_id")),
        sa.UniqueConstraint(
            "org_id",
            "scope",
            "workspace_id",
            "user_id",
            "name",
            name=op.f("uq_skills_org_id_scope_workspace_id_user_id_name"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_table(
        "skill_versions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("skill_id", sa.UUID(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("skill_md", sa.Text(), nullable=False),
        sa.Column(
            "files",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "requires_toolsets",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("change", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("author_kind", sa.Text(), nullable=False),
        sa.Column("author_user_id", sa.UUID(), nullable=True),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "author_kind IN ('user', 'agent', 'system')",
            name=op.f("ck_skill_versions_author_kind"),
        ),
        sa.CheckConstraint(
            "change IN ('create', 'patch', 'edit', 'write_file', 'remove_file', 'archive')",
            name=op.f("ck_skill_versions_change"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(files) = 'object'", name=op.f("ck_skill_versions_files_object")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(requires_toolsets) = 'array'",
            name=op.f("ck_skill_versions_requires_toolsets_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(tags) = 'array'", name=op.f("ck_skill_versions_tags_array")
        ),
        sa.CheckConstraint("version_no >= 1", name=op.f("ck_skill_versions_version_no")),
        sa.ForeignKeyConstraint(
            ["org_id", "skill_id"],
            ["skills.org_id", "skills.id"],
            name=op.f("fk_skill_versions_org_id_skill_id_skills"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_skill_versions_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_versions")),
        sa.UniqueConstraint(
            "org_id",
            "skill_id",
            "version_no",
            name=op.f("uq_skill_versions_org_id_skill_id_version_no"),
        ),
    )

    # A member reads the skills of the org, the skills of the workspaces that the member can
    # see, and the own skills of the member. A version has the visibility of its skill. No
    # policy allows writes: the control plane writes as the table owner.
    for table in ("skills", "skill_versions"):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO authenticated")
    op.execute(
        "CREATE POLICY skills_select ON public.skills FOR SELECT TO authenticated"
        " USING (sama_private.is_org_member(org_id) AND ("
        "scope = 'org'"
        " OR (scope = 'workspace' AND sama_private.can_see_workspace(org_id, workspace_id))"
        " OR (scope = 'user' AND user_id IN (SELECT sama_private.my_user_ids()))))"
    )
    op.execute(
        "CREATE POLICY skill_versions_select ON public.skill_versions FOR SELECT TO authenticated"
        " USING (EXISTS (SELECT 1 FROM public.skills s"
        " WHERE s.org_id = skill_versions.org_id AND s.id = skill_versions.skill_id))"
    )

    # The default profile of the first org (M1.15) gets the new toolset, like in 0012.
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{NEW_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{OLD_TOOLSETS}'::jsonb"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{OLD_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{NEW_TOOLSETS}'::jsonb"
    )
    op.drop_table("skill_versions")
    op.drop_table("skills")
