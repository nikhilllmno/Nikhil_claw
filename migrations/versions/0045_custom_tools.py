"""Custom tools (docs/architecture.md, Section 10.8; roadmap M7.3).

- ``custom_tools`` holds one row for each custom tool of the org, of a workspace, or of a user:
  the name and the version that runs. ``current_version`` is null until a version is approved.
- ``custom_tool_versions`` holds the versions: the kind (``http`` or ``script``), the
  description, the JSON schema of the arguments, the policy, the request or the script, and the
  review (``approved``, ``pending``, or ``rejected``). A version never changes, except its
  review.

Members read the tools of the org, of the workspaces that they see, and their own user tools,
like the skills. Only the control plane writes the two tables. The credential of an HTTP tool
is in the vault (``custom_tool:<id>``). Profiles with the default toolsets of 0041 get the
toolset ``custom_tools``.

Revision ID: 0045
Revises: 0044
Create Date: 2026-11-21
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CUSTOM_TOOL_NAMES = "name ~ '^custom_tool:[0-9a-f-]{36}$'"
"""The vault names of the credentials of custom tools. The migration adds them to the check of
``vault_secrets`` as it is now, so it does not depend on the order of other migrations that
change the same check."""
_CUSTOM_IN_DEF = re.compile(r"\s+OR\s+\(name ~ '\^custom_tool:\[0-9a-f-\]\{36\}\$'::text\)")

# The default list after the coding agents migration (0041) and the same list with
# ``custom_tools``.
# An exact match keeps the profiles that a person changed.
OLD_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search", "subagents", "connections", "integrations", "delegation", "tasks", "cron",'
    ' "code_exec", "workflows", "browser", "coding_agents"]'
)
NEW_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search", "subagents", "connections", "integrations", "delegation", "tasks", "cron",'
    ' "code_exec", "workflows", "browser", "coding_agents", "custom_tools"]'
)


def upgrade() -> None:
    op.create_table(
        "custom_tools",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=True),
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
            "(scope = 'org' AND workspace_id IS NULL AND user_id IS NULL)"
            " OR (scope = 'workspace' AND workspace_id IS NOT NULL AND user_id IS NULL)"
            " OR (scope = 'user' AND workspace_id IS NULL AND user_id IS NOT NULL)",
            name=op.f("ck_custom_tools_scope_columns"),
        ),
        sa.CheckConstraint(
            "name ~ '^[a-z][a-z0-9_]{1,39}$'", name=op.f("ck_custom_tools_name_format")
        ),
        sa.CheckConstraint(
            "scope IN ('org', 'workspace', 'user')", name=op.f("ck_custom_tools_scope")
        ),
        sa.CheckConstraint(
            "current_version IS NULL OR current_version >= 1",
            name=op.f("ck_custom_tools_current_version"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "created_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_custom_tools_org_id_created_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_custom_tools_org_id_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_custom_tools_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_custom_tools_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_custom_tools")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_custom_tools_org_id_id")),
        sa.UniqueConstraint(
            "org_id",
            "scope",
            "workspace_id",
            "user_id",
            "name",
            name=op.f("uq_custom_tools_org_id_scope_workspace_id_user_id_name"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_table(
        "custom_tool_versions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("tool_id", sa.UUID(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "input_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("policy", sa.Text(), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("change", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("review", sa.Text(), nullable=False),
        sa.Column("author_user_id", sa.UUID(), nullable=True),
        sa.Column("reviewed_by_user_id", sa.UUID(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "change IN ('create', 'edit', 'revert')", name=op.f("ck_custom_tool_versions_change")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(config) = 'object'", name=op.f("ck_custom_tool_versions_config_object")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(input_schema) = 'object'",
            name=op.f("ck_custom_tool_versions_input_schema_object"),
        ),
        sa.CheckConstraint("kind IN ('http', 'script')", name=op.f("ck_custom_tool_versions_kind")),
        sa.CheckConstraint(
            "policy IN ('allow', 'ask', 'deny')", name=op.f("ck_custom_tool_versions_policy")
        ),
        sa.CheckConstraint(
            "review IN ('approved', 'pending', 'rejected')",
            name=op.f("ck_custom_tool_versions_review"),
        ),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 1 AND 1000",
            name=op.f("ck_custom_tool_versions_description_length"),
        ),
        sa.CheckConstraint(
            "char_length(summary) <= 500", name=op.f("ck_custom_tool_versions_summary_length")
        ),
        sa.CheckConstraint("version_no >= 1", name=op.f("ck_custom_tool_versions_version_no")),
        sa.ForeignKeyConstraint(
            ["org_id", "tool_id"],
            ["custom_tools.org_id", "custom_tools.id"],
            name=op.f("fk_custom_tool_versions_org_id_tool_id_custom_tools"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_custom_tool_versions_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_custom_tool_versions")),
        sa.UniqueConstraint(
            "org_id",
            "tool_id",
            "version_no",
            name=op.f("uq_custom_tool_versions_org_id_tool_id_version_no"),
        ),
    )
    # No policy allows writes: the control plane writes as the table owner.
    for table in ("custom_tools", "custom_tool_versions"):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO authenticated")
    # The read rule of the skills (0013): an org tool for each member, a workspace tool for the
    # members who see the workspace, a user tool for its user only.
    op.execute(
        "CREATE POLICY custom_tools_select ON public.custom_tools FOR SELECT TO authenticated"
        " USING (sama_private.is_org_member(org_id) AND ("
        "scope = 'org'"
        " OR (scope = 'workspace' AND sama_private.can_see_workspace(org_id, workspace_id))"
        " OR (scope = 'user' AND user_id IN (SELECT sama_private.my_user_ids()))))"
    )
    op.execute(
        "CREATE POLICY custom_tool_versions_select ON public.custom_tool_versions FOR SELECT"
        " TO authenticated USING (EXISTS (SELECT 1 FROM public.custom_tools t"
        " WHERE t.org_id = custom_tool_versions.org_id AND t.id = custom_tool_versions.tool_id))"
    )
    current = _vault_check()
    op.drop_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", type_="check")
    op.create_check_constraint(
        op.f("ck_vault_secrets_name"), "vault_secrets", f"{current} OR {CUSTOM_TOOL_NAMES}"
    )
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{NEW_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{OLD_TOOLSETS}'::jsonb"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{OLD_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{NEW_TOOLSETS}'::jsonb"
    )
    op.execute("DELETE FROM public.vault_secrets WHERE name LIKE 'custom_tool:%'")
    current = _CUSTOM_IN_DEF.sub("", _vault_check())
    op.drop_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", type_="check")
    op.create_check_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", current)
    op.drop_table("custom_tool_versions")
    op.drop_table("custom_tools")


def _vault_check() -> str:
    """The expression of the check ``ck_vault_secrets_name`` as Postgres stores it."""
    found = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conname = 'ck_vault_secrets_name'"
                " AND conrelid = 'public.vault_secrets'::regclass"
            )
        )
        .scalar_one()
    )
    return str(found).removeprefix("CHECK ").strip()
