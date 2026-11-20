"""Connections (docs/architecture.md, Section 10.3).

``connections`` holds one configured account or endpoint of a connector for a workspace. A
``personal`` connection belongs to one user, and a ``workspace`` connection is shared. The row
has no secret: a Composio connection keeps its credentials at Composio, and a form connection
keeps them in the vault under the name ``connection:<id>``. The vault name check accepts that
form.

Row level security: a member of the workspace (or an org admin, through
``can_see_workspace``) sees the workspace connections. Only the owner sees a personal
connection. The control plane writes the table as the owner of the table.

The default profile gets the toolsets ``connections`` and ``integrations``. The default group set
does not change.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_VAULT_NAMES = (
    "name IN ('anthropic_api_key', 'openai_api_key', 'brave_api_key', 'telegram_bot_token',"
    " 'slack_bot_token', 'slack_signing_secret')"
)
NEW_VAULT_NAMES = (
    "name IN ('anthropic_api_key', 'openai_api_key', 'brave_api_key', 'telegram_bot_token',"
    " 'slack_bot_token', 'slack_signing_secret', 'composio_api_key')"
    " OR name ~ '^connection:[0-9a-f-]{36}$'"
)
CONNECTION_POLICY = (
    "sama_private.is_org_member(org_id)"
    " AND sama_private.can_see_workspace(org_id, workspace_id)"
    " AND (scope = 'workspace' OR owner_user_id IN (SELECT sama_private.my_user_ids()))"
)
# The default list after migration 0019 and the same list with the two new toolsets. An exact
# match keeps the profiles that a person changed.
OLD_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search", "subagents"]'
)
NEW_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search", "subagents", "connections", "integrations"]'
)


def upgrade() -> None:
    op.create_table(
        "connections",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=True),
        sa.Column("connector", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("secret_ref", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column(
            "tool_cache",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "policy_overrides",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_health_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.CheckConstraint("scope IN ('personal', 'workspace')", name=op.f("ck_connections_scope")),
        sa.CheckConstraint(
            "kind IN ('composio', 'mcp_remote', 'mcp_hosted', 'database', 'http_api', 'plugin')",
            name=op.f("ck_connections_kind"),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'initiated', 'connected', 'failed', 'expired', 'disabled')",
            name=op.f("ck_connections_state"),
        ),
        sa.CheckConstraint(
            "(scope = 'personal') = (owner_user_id IS NOT NULL)", name=op.f("ck_connections_owner")
        ),
        sa.CheckConstraint(
            "connector ~ '^[a-z][a-z0-9_-]{0,39}$'", name=op.f("ck_connections_connector")
        ),
        sa.CheckConstraint(
            "secret_ref IS NULL OR secret_ref = 'connection:' || id::text",
            name=op.f("ck_connections_secret_ref"),
        ),
        sa.CheckConstraint(
            "last_error IS NULL OR char_length(last_error) <= 500",
            name=op.f("ck_connections_last_error_length"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_connections_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_connections_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "owner_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_connections_org_id_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "created_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_connections_org_id_created_by_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connections")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_connections_org_id_id")),
        sa.UniqueConstraint(
            "org_id",
            "workspace_id",
            "connector",
            "owner_user_id",
            name=op.f("uq_connections_org_id_workspace_id_connector_owner_user_id"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.execute("ALTER TABLE public.connections ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.connections FROM PUBLIC, anon, authenticated")
    op.execute("GRANT SELECT ON TABLE public.connections TO authenticated")
    op.execute(
        "CREATE POLICY connections_select ON public.connections FOR SELECT TO authenticated"
        f" USING ({CONNECTION_POLICY})"
    )
    op.drop_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", type_="check")
    op.create_check_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", NEW_VAULT_NAMES)
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{NEW_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{OLD_TOOLSETS}'::jsonb"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{OLD_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{NEW_TOOLSETS}'::jsonb"
    )
    op.execute(
        "DELETE FROM public.vault_secrets"
        " WHERE name LIKE 'connection:%' OR name = 'composio_api_key'"
    )
    op.drop_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", type_="check")
    op.create_check_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", OLD_VAULT_NAMES)
    op.drop_table("connections")
