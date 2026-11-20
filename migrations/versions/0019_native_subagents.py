"""Native sub-agents (docs/architecture.md, Section 13.1).

``agent_profiles.subagents`` has the native sub-agents of a profile: a JSON array of sub-agent
definitions. The column is on an existing table, so the row level security of that table
already covers it. The default profile gets the toolset ``subagents``. The toolset alone gives
no sub-agent, because the list is empty.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The default list after the context management migration (0018) and the same list with
# ``subagents``. An exact match keeps the profiles that a person changed.
OLD_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search"]'
)
NEW_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search", "subagents"]'
)


def upgrade() -> None:
    op.add_column(
        "agent_profiles",
        sa.Column(
            "subagents",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_agent_profiles_subagents_is_array"),
        "agent_profiles",
        "jsonb_typeof(subagents) = 'array'",
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
    op.drop_constraint(op.f("ck_agent_profiles_subagents_is_array"), "agent_profiles", "check")
    op.drop_column("agent_profiles", "subagents")
