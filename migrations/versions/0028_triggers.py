"""Event triggers (docs/architecture.md, Section 12.2).

``triggers`` holds one event source of a workspace: a signed ``webhook`` with its own URL, or a
``composio`` trigger instance on the account of a connection. Each event starts an unattended
run in a new private session of the trigger owner. ``trigger_events`` holds one row for each
accepted delivery, and its unique ``(trigger_id, delivery_id)`` rejects a replay.

``runs.toolsets`` limits the toolsets of one run. A trigger run gets the toolsets of the trigger,
and the default is the ``safe`` preset (web, media, clarify).

The vault name check accepts ``composio_webhook_secret`` (the signing secret of the Composio
webhook subscription of an org) and ``trigger:<id>`` (the signing secret of one webhook trigger).

Row level security: a member of the workspace (or an org admin) sees its triggers. An event row
shows to the users who can read its session. The control plane writes both tables as the owner.

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_VAULT_NAMES = (
    "name IN ('anthropic_api_key', 'openai_api_key', 'brave_api_key', 'telegram_bot_token',"
    " 'slack_bot_token', 'slack_signing_secret', 'composio_api_key')"
    " OR name ~ '^connection:[0-9a-f-]{36}$'"
)
NEW_VAULT_NAMES = (
    "name IN ('anthropic_api_key', 'openai_api_key', 'brave_api_key', 'telegram_bot_token',"
    " 'slack_bot_token', 'slack_signing_secret', 'composio_api_key', 'composio_webhook_secret')"
    " OR name ~ '^connection:[0-9a-f-]{36}$' OR name ~ '^trigger:[0-9a-f-]{36}$'"
)


def upgrade() -> None:
    op.add_column("runs", sa.Column("toolsets", postgresql.JSONB(astext_type=sa.Text())))
    op.create_check_constraint(
        op.f("ck_runs_toolsets_array"),
        "runs",
        "toolsets IS NULL OR jsonb_typeof(toolsets) = 'array'",
    )
    op.create_table(
        "triggers",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("agent_profile_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column(
            "toolsets",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("""'["web", "media", "clarify"]'::jsonb"""),
            nullable=False,
        ),
        sa.Column("target", sa.Text(), server_default=sa.text("'run'"), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=True),
        sa.Column("composio_trigger_id", sa.Text(), nullable=True),
        sa.Column("composio_slug", sa.Text(), nullable=True),
        sa.Column(
            "composio_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
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
        sa.CheckConstraint("kind IN ('webhook', 'composio')", name=op.f("ck_triggers_kind")),
        sa.CheckConstraint("status IN ('active', 'disabled')", name=op.f("ck_triggers_status")),
        sa.CheckConstraint("target IN ('run')", name=op.f("ck_triggers_target")),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 80", name=op.f("ck_triggers_name")),
        sa.CheckConstraint(
            "char_length(prompt) BETWEEN 1 AND 8000", name=op.f("ck_triggers_prompt")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(toolsets) = 'array'", name=op.f("ck_triggers_toolsets_array")
        ),
        sa.CheckConstraint(
            "(kind = 'composio') = (connection_id IS NOT NULL AND composio_slug IS NOT NULL)",
            name=op.f("ck_triggers_composio_fields"),
        ),
        sa.CheckConstraint(
            "kind = 'composio' OR composio_trigger_id IS NULL",
            name=op.f("ck_triggers_composio_trigger_id"),
        ),
        sa.CheckConstraint(
            "last_error IS NULL OR char_length(last_error) <= 500",
            name=op.f("ck_triggers_last_error_length"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_triggers_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_triggers_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "owner_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_triggers_org_id_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            name=op.f("fk_triggers_org_id_agent_profile_id_agent_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "connection_id"],
            ["connections.org_id", "connections.id"],
            name=op.f("fk_triggers_org_id_connection_id_connections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "created_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_triggers_org_id_created_by_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_triggers")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_triggers_org_id_id")),
        sa.UniqueConstraint("composio_trigger_id", name=op.f("uq_triggers_composio_trigger_id")),
    )
    op.create_index(op.f("ix_triggers_org_id_workspace_id"), "triggers", ["org_id", "workspace_id"])
    op.create_table(
        "trigger_events",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("trigger_id", sa.UUID(), nullable=False),
        sa.Column("delivery_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(delivery_id) BETWEEN 1 AND 200",
            name=op.f("ck_trigger_events_delivery_id"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_trigger_events_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "trigger_id"],
            ["triggers.org_id", "triggers.id"],
            name=op.f("fk_trigger_events_org_id_trigger_id_triggers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id"],
            ["sessions.org_id", "sessions.id"],
            name=op.f("fk_trigger_events_org_id_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            name=op.f("fk_trigger_events_org_id_session_id_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trigger_events")),
        sa.UniqueConstraint(
            "trigger_id", "delivery_id", name=op.f("uq_trigger_events_trigger_id_delivery_id")
        ),
    )
    op.create_index(
        op.f("ix_trigger_events_org_id_session_id"), "trigger_events", ["org_id", "session_id"]
    )
    op.create_index(op.f("ix_trigger_events_received_at"), "trigger_events", ["received_at"])
    for table, policy in (
        ("triggers", "sama_private.can_see_workspace(org_id, workspace_id)"),
        ("trigger_events", "sama_private.can_read_session(org_id, session_id)"),
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO authenticated")
        op.execute(
            f"CREATE POLICY {table}_select ON public.{table} FOR SELECT TO authenticated"
            f" USING ({policy})"
        )
    op.drop_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", type_="check")
    op.create_check_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", NEW_VAULT_NAMES)


def downgrade() -> None:
    op.execute(
        "DELETE FROM public.vault_secrets"
        " WHERE name LIKE 'trigger:%' OR name = 'composio_webhook_secret'"
    )
    op.drop_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", type_="check")
    op.create_check_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", OLD_VAULT_NAMES)
    op.drop_table("trigger_events")
    op.drop_table("triggers")
    op.drop_constraint(op.f("ck_runs_toolsets_array"), "runs", type_="check")
    op.drop_column("runs", "toolsets")
