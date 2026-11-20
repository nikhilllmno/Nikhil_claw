"""Delegation and the task board (docs/architecture.md, Sections 12.4 and 13.2).

``sessions.parent_run_id`` names the parent run of a delegated child session. The key
``(org_id, parent_session_id, parent_run_id)`` proves that the run is a run of the parent
session. ``tasks`` has the cards of the task board, and ``task_comments`` has their comments.
A card has the audience of a session, so the select policy uses ``session_visible``. A comment
uses the new function ``can_read_task``. No policy allows writes: the control plane writes as
the table owner.

The default profile gets the toolsets ``delegation`` and ``tasks``, and the default group
toolsets get ``tasks``. An exact match keeps the profiles that a person changed.

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search", "subagents", "connections", "integrations"]'
)
NEW_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search", "subagents", "connections", "integrations", "delegation", "tasks"]'
)
OLD_GROUP_TOOLSETS = '["todo", "web", "clarify", "memory", "sessions", "media"]'
NEW_GROUP_TOOLSETS = '["todo", "web", "clarify", "memory", "sessions", "media", "tasks"]'

CAN_READ_TASK = """
CREATE FUNCTION sama_private.can_read_task(p_org uuid, p_task uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ''
AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.tasks t
        WHERE t.org_id = p_org AND t.id = p_task
          AND sama_private.session_visible(t.org_id, t.workspace_id, t.audience, t.owner_user_id)
    )
$$
"""

POLICIES = {
    "tasks": "sama_private.session_visible(org_id, workspace_id, audience, owner_user_id)",
    "task_comments": "sama_private.can_read_task(org_id, task_id)",
}


def _timestamp(name: str) -> sa.Column[object]:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _sessions() -> None:
    op.add_column("sessions", sa.Column("parent_run_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_sessions_org_id_parent_session_id_parent_run_id_runs"),
        "sessions",
        "runs",
        ["org_id", "parent_session_id", "parent_run_id"],
        ["org_id", "session_id", "id"],
    )
    op.create_check_constraint(
        op.f("ck_sessions_parent_run_is_delegation"),
        "sessions",
        "parent_run_id IS NULL OR lineage IS NOT DISTINCT FROM 'delegation'",
    )
    op.create_index(
        "ix_sessions_parent_run",
        "sessions",
        ["org_id", "parent_run_id"],
        postgresql_where=sa.text("parent_run_id IS NOT NULL"),
    )


def _tasks() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.Text(), server_default="todo", nullable=False),
        sa.Column("assignee_profile_id", sa.UUID(), nullable=True),
        sa.Column("assignee_user_id", sa.UUID(), nullable=True),
        sa.Column("parent_task_id", sa.UUID(), nullable=True),
        sa.Column(
            "depends_on",
            postgresql.ARRAY(sa.UUID()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("audience", sa.Text(), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=True),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("created_by_run_id", sa.UUID(), nullable=True),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('todo', 'running', 'blocked', 'done', 'cancelled')",
            name=op.f("ck_tasks_status"),
        ),
        sa.CheckConstraint("audience IN ('private', 'shared')", name=op.f("ck_tasks_audience")),
        sa.CheckConstraint(
            "audience <> 'private' OR owner_user_id IS NOT NULL",
            name=op.f("ck_tasks_private_has_owner"),
        ),
        sa.CheckConstraint(
            "assignee_profile_id IS NULL OR assignee_user_id IS NULL",
            name=op.f("ck_tasks_one_assignee"),
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 200", name=op.f("ck_tasks_title_length")
        ),
        sa.CheckConstraint("char_length(body) <= 20000", name=op.f("ck_tasks_body_length")),
        sa.CheckConstraint("cardinality(depends_on) <= 20", name=op.f("ck_tasks_depends_on_size")),
        sa.CheckConstraint(
            "parent_task_id IS NULL OR parent_task_id <> id", name=op.f("ck_tasks_not_own_parent")
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_tasks_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_tasks_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "assignee_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            name=op.f("fk_tasks_org_id_assignee_profile_id_agent_profiles"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "assignee_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_tasks_org_id_assignee_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "owner_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_tasks_org_id_owner_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "created_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_tasks_org_id_created_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id", "parent_task_id"],
            ["tasks.org_id", "tasks.workspace_id", "tasks.id"],
            name=op.f("fk_tasks_org_id_workspace_id_parent_task_id_tasks"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id", "session_id"],
            ["sessions.org_id", "sessions.workspace_id", "sessions.id"],
            name=op.f("fk_tasks_org_id_workspace_id_session_id_sessions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_tasks_org_id_id")),
        sa.UniqueConstraint(
            "org_id", "workspace_id", "id", name=op.f("uq_tasks_org_id_workspace_id_id")
        ),
    )
    op.create_index(
        op.f("ix_tasks_org_id_workspace_id_status"),
        "tasks",
        ["org_id", "workspace_id", "status"],
    )
    op.create_index(
        "ix_tasks_dispatch",
        "tasks",
        ["created_at"],
        postgresql_where=sa.text("status = 'todo' AND assignee_profile_id IS NOT NULL"),
    )
    op.create_index(
        "ix_tasks_running", "tasks", ["org_id"], postgresql_where=sa.text("status = 'running'")
    )
    op.create_table(
        "task_comments",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("author_kind", sa.Text(), nullable=False),
        sa.Column("author_user_id", sa.UUID(), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        _timestamp("created_at"),
        sa.CheckConstraint(
            "author_kind IN ('user', 'agent', 'system')", name=op.f("ck_task_comments_author_kind")
        ),
        sa.CheckConstraint(
            "char_length(body) BETWEEN 1 AND 4000", name=op.f("ck_task_comments_body_length")
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_task_comments_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id", "task_id"],
            ["tasks.org_id", "tasks.workspace_id", "tasks.id"],
            name=op.f("fk_task_comments_org_id_workspace_id_task_id_tasks"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "author_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_task_comments_org_id_author_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_comments")),
    )
    op.create_index(
        op.f("ix_task_comments_org_id_task_id_created_at"),
        "task_comments",
        ["org_id", "task_id", "created_at"],
    )


def _secure() -> None:
    op.execute(CAN_READ_TASK)
    op.execute("REVOKE ALL ON FUNCTION sama_private.can_read_task(uuid, uuid) FROM PUBLIC, anon")
    op.execute("GRANT EXECUTE ON FUNCTION sama_private.can_read_task(uuid, uuid) TO authenticated")
    for table, rule in POLICIES.items():
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO authenticated")
        op.execute(
            f"CREATE POLICY {table}_select ON public.{table} FOR SELECT TO authenticated"
            f" USING ({rule})"
        )


def upgrade() -> None:
    _sessions()
    _tasks()
    _secure()
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{NEW_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{OLD_TOOLSETS}'::jsonb"
    )
    op.execute(
        f"UPDATE public.agent_profiles SET group_toolsets = '{NEW_GROUP_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE group_toolsets = '{OLD_GROUP_TOOLSETS}'::jsonb"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE public.agent_profiles SET group_toolsets = '{OLD_GROUP_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE group_toolsets = '{NEW_GROUP_TOOLSETS}'::jsonb"
    )
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{OLD_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{NEW_TOOLSETS}'::jsonb"
    )
    op.drop_table("task_comments")
    op.drop_table("tasks")
    op.execute("DROP FUNCTION sama_private.can_read_task(uuid, uuid)")
    op.drop_index("ix_sessions_parent_run", table_name="sessions")
    op.drop_constraint(op.f("ck_sessions_parent_run_is_delegation"), "sessions", "check")
    op.drop_constraint(
        op.f("fk_sessions_org_id_parent_session_id_parent_run_id_runs"), "sessions", "foreignkey"
    )
    op.drop_column("sessions", "parent_run_id")
