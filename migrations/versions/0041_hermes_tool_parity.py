"""Hermes tool parity (docs/architecture.md, Sections 8.2, 9.8, 10.2.2, and 13.2; roadmap M7.5).

- ``agent_profiles.delegation_model``: the model of the child runs of ``delegate_task``. ``NULL``
  means the model of the profile, as before.
- The default profile gets the toolset ``moa`` (an exact match of the default list of 0036).
- ``agent_profiles.memory_provider``: ``builtin`` (the default) or ``mem0``. With ``mem0``, sama
  also sends the turns of private chat sessions to Mem0 and the agent has ``memory_recall``.
- ``model_calls`` accepts the provider ``gemini`` and the endpoint ``generate_content``
  (``video_analyze`` and ``moa``).
- ``data_purges`` gets the kind ``mem0``: a user or org delete deletes the Mem0 memories of the
  users through the purge worker (Section 17.4).
- The vault accepts the names ``gemini_api_key``, ``x_bearer_token``, and ``mem0_api_key``.
- ``skill_settings`` holds the settings of a skill for one workspace. The ``config`` block of
  ``SKILL.md`` declares them. A member who sees the workspace reads the row. Only the control
  plane writes the table.

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_PROVIDERS = "provider IN ('anthropic', 'openai')"
NEW_PROVIDERS = "provider IN ('anthropic', 'openai', 'gemini')"
OLD_ENDPOINTS = (
    "endpoint IN ('messages', 'count_tokens', 'responses', 'chat_completions', 'images', 'speech')"
)
NEW_ENDPOINTS = (
    "endpoint IN ('messages', 'count_tokens', 'responses', 'chat_completions', 'images', 'speech',"
    " 'generate_content')"
)
OLD_PURGE_KINDS = "kind IN ('sandboxes', 'traces', 'browser_contexts')"
NEW_PURGE_KINDS = "kind IN ('sandboxes', 'traces', 'browser_contexts', 'mem0')"
_PATTERNS = (
    " OR name ~ '^connection:[0-9a-f-]{36}$' OR name ~ '^trigger:[0-9a-f-]{36}$'"
    " OR name ~ '^browser_login:[0-9a-f-]{36}$'"
)
OLD_VAULT_NAMES = (
    "name IN ('anthropic_api_key', 'openai_api_key', 'brave_api_key', 'telegram_bot_token',"
    " 'slack_bot_token', 'slack_signing_secret', 'composio_api_key', 'composio_webhook_secret',"
    " 'whatsapp_api_token', 'whatsapp_app_secret', 'whatsapp_link_secret', 'sendgrid_api_key',"
    " 'email_link_secret')" + _PATTERNS
)
NEW_VAULT_NAMES = (
    "name IN ('anthropic_api_key', 'openai_api_key', 'brave_api_key', 'telegram_bot_token',"
    " 'slack_bot_token', 'slack_signing_secret', 'composio_api_key', 'composio_webhook_secret',"
    " 'whatsapp_api_token', 'whatsapp_app_secret', 'whatsapp_link_secret', 'sendgrid_api_key',"
    " 'email_link_secret', 'gemini_api_key', 'x_bearer_token', 'mem0_api_key')" + _PATTERNS
)


# The default list after the browser migration (0036) and the same list with ``moa``. An exact
# match keeps the profiles that a person changed.
OLD_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search", "subagents", "connections", "integrations", "delegation", "tasks", "cron",'
    ' "code_exec", "workflows", "browser"]'
)
NEW_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search", "subagents", "connections", "integrations", "delegation", "tasks", "cron",'
    ' "code_exec", "workflows", "browser", "moa"]'
)


def _check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")
    op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, condition)


def upgrade() -> None:
    op.add_column("agent_profiles", sa.Column("delegation_model", sa.Text(), nullable=True))
    op.add_column(
        "agent_profiles",
        sa.Column("memory_provider", sa.Text(), server_default="builtin", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_agent_profiles_memory_provider"),
        "agent_profiles",
        "memory_provider IN ('builtin', 'mem0')",
    )
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{NEW_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{OLD_TOOLSETS}'::jsonb"
    )
    _check("model_calls", "provider", NEW_PROVIDERS)
    _check("model_calls", "endpoint", NEW_ENDPOINTS)
    _check("data_purges", "kind", NEW_PURGE_KINDS)
    _check("vault_secrets", "name", NEW_VAULT_NAMES)
    op.create_table(
        "skill_settings",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("skill_name", sa.Text(), nullable=False),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("updated_by_user_id", sa.UUID(), nullable=True),
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
            "jsonb_typeof(settings) = 'object'", name=op.f("ck_skill_settings_settings_is_object")
        ),
        sa.CheckConstraint(
            "char_length(skill_name) BETWEEN 1 AND 64",
            name=op.f("ck_skill_settings_skill_name_length"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "updated_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_skill_settings_org_id_updated_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_skill_settings_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_skill_settings_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_settings")),
        sa.UniqueConstraint(
            "org_id",
            "workspace_id",
            "skill_name",
            name=op.f("uq_skill_settings_org_id_workspace_id_skill_name"),
        ),
    )
    # A member who sees the workspace reads the settings, as the skills of the workspace. No
    # policy allows writes: the control plane writes as the table owner.
    op.execute("ALTER TABLE public.skill_settings ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.skill_settings FROM PUBLIC, anon, authenticated")
    op.execute("GRANT SELECT ON TABLE public.skill_settings TO authenticated")
    op.execute(
        "CREATE POLICY skill_settings_select ON public.skill_settings FOR SELECT"
        " TO authenticated USING (sama_private.can_see_workspace(org_id, workspace_id))"
    )


def downgrade() -> None:
    op.drop_table("skill_settings")
    op.execute(
        "DELETE FROM public.vault_secrets"
        " WHERE name IN ('gemini_api_key', 'x_bearer_token', 'mem0_api_key')"
    )
    _check("vault_secrets", "name", OLD_VAULT_NAMES)
    op.execute("DELETE FROM public.data_purges WHERE kind = 'mem0'")
    _check("data_purges", "kind", OLD_PURGE_KINDS)
    op.execute("DELETE FROM public.model_calls WHERE provider = 'gemini'")
    _check("model_calls", "endpoint", OLD_ENDPOINTS)
    _check("model_calls", "provider", OLD_PROVIDERS)
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{OLD_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{NEW_TOOLSETS}'::jsonb"
    )
    op.drop_constraint(op.f("ck_agent_profiles_memory_provider"), "agent_profiles", type_="check")
    op.drop_column("agent_profiles", "memory_provider")
    op.drop_column("agent_profiles", "delegation_model")
