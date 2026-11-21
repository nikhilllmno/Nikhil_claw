"""Coding agent connectors: the runtimes ``claude_code`` and ``codex`` (docs/architecture.md,
Section 15.4; roadmap M7.2).

- ``agent_profiles.runtime`` and ``runs.runtime`` accept ``claude_code`` and ``codex``.
- ``sessions.coding_agent`` is the direct mode of a session: its chat runs go to that coding
  agent until ``/sama``.
- ``runs.coding_agent`` is the coding agent of one ``/claude`` or ``/codex`` run with a task.
- A profile with the default toolsets of migration 0036 gets the toolset ``coding_agents``. A
  profile that a person changed keeps its toolsets.

No new table, so no new RLS policy: the columns follow the policies of their tables.

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_RUNTIMES = "runtime IN ('claude_agent_sdk', 'openai_agents_sdk', 'fake')"
NEW_RUNTIMES = (
    "runtime IN ('claude_agent_sdk', 'openai_agents_sdk', 'claude_code', 'codex', 'fake')"
)
# The default list after the browser migration (0036) and the same list with ``coding_agents``.
OLD_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search", "subagents", "connections", "integrations", "delegation", "tasks", "cron",'
    ' "code_exec", "workflows", "browser"]'
)
NEW_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search", "subagents", "connections", "integrations", "delegation", "tasks", "cron",'
    ' "code_exec", "workflows", "browser", "coding_agents"]'
)
CODING_AGENT = "coding_agent IS NULL OR coding_agent IN ('claude_code', 'codex')"


def upgrade() -> None:
    for table in ("agent_profiles", "runs"):
        op.drop_constraint(op.f(f"ck_{table}_runtime"), table, type_="check")
        op.create_check_constraint(op.f(f"ck_{table}_runtime"), table, NEW_RUNTIMES)
    for table in ("sessions", "runs"):
        op.add_column(table, sa.Column("coding_agent", sa.Text(), nullable=True))
        op.create_check_constraint(op.f(f"ck_{table}_coding_agent"), table, CODING_AGENT)
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{NEW_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{OLD_TOOLSETS}'::jsonb"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{OLD_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{NEW_TOOLSETS}'::jsonb"
    )
    for table in ("sessions", "runs"):
        op.drop_constraint(op.f(f"ck_{table}_coding_agent"), table, type_="check")
        op.drop_column(table, "coding_agent")
    # A profile or a run of a coding runtime goes back to the Claude runtime.
    op.execute(
        "UPDATE agent_profiles SET runtime = 'claude_agent_sdk' "
        "WHERE runtime IN ('claude_code', 'codex')"
    )
    op.execute(
        "UPDATE runs SET runtime = 'claude_agent_sdk' WHERE runtime IN ('claude_code', 'codex')"
    )
    for table in ("agent_profiles", "runs"):
        op.drop_constraint(op.f(f"ck_{table}_runtime"), table, type_="check")
        op.create_check_constraint(op.f(f"ck_{table}_runtime"), table, OLD_RUNTIMES)
