"""The cloud browser (docs/architecture.md, Section 14.1; roadmap M5.1).

- ``browser_contexts`` maps a user and a browser target to the persistent context of the user at
  the provider (a Browserbase context for ``cloud``). The provider keeps the cookies, so the row
  has no secret. A member reads only their own rows. Only the control plane writes the table.
- The default profile gets the toolset ``browser``.

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The default list after the workflows migration (0029) and the same list with ``browser``. An
# exact match keeps the profiles that a person changed.
OLD_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search", "subagents", "connections", "integrations", "delegation", "tasks", "cron",'
    ' "code_exec", "workflows"]'
)
NEW_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search", "subagents", "connections", "integrations", "delegation", "tasks", "cron",'
    ' "code_exec", "workflows", "browser"]'
)


def upgrade() -> None:
    op.create_table(
        "browser_contexts",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("provider_context_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "target IN ('cloud', 'my_chrome')", name=op.f("ck_browser_contexts_target")
        ),
        sa.CheckConstraint(
            "char_length(provider_context_id) BETWEEN 1 AND 200",
            name=op.f("ck_browser_contexts_provider_context_id_length"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_browser_contexts_org_id_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_browser_contexts_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_browser_contexts")),
        sa.UniqueConstraint(
            "org_id", "user_id", "target", name=op.f("uq_browser_contexts_org_id_user_id_target")
        ),
    )
    # A member reads only their own contexts, like a job. No policy allows writes: the control
    # plane writes as the table owner.
    op.execute("ALTER TABLE public.browser_contexts ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.browser_contexts FROM PUBLIC, anon, authenticated")
    op.execute("GRANT SELECT ON TABLE public.browser_contexts TO authenticated")
    op.execute(
        "CREATE POLICY browser_contexts_select ON public.browser_contexts FOR SELECT"
        " TO authenticated USING (sama_private.is_org_member(org_id)"
        " AND user_id IN (SELECT sama_private.my_user_ids()))"
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
    op.drop_table("browser_contexts")
