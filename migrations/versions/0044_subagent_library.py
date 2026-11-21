"""The sub-agent library (docs/architecture.md, Section 13.3; roadmap M7.4).

- ``library_agents`` holds the sub-agents of the library: org, workspace, and personal (``user``)
  agents with a name, a description, a prompt, toolsets, a model, and limits.
- ``library_agent_versions`` holds each version of an agent, for the history and the revert.
- ``agent_profiles.library_subagents`` names the library agents that a profile uses.
- ``sessions.library_agent`` holds the frozen spec of the library agent that a session runs as
  (a child of ``delegate_task(agent=...)`` or a test run).

A member reads the org agents, the agents of the workspaces that the member can see, and the
own personal agents. A version has the visibility of its agent. Only the control plane writes
the two tables.

Revision ID: 0044
Revises: 0043
Create Date: 2026-11-21
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp(name: str) -> sa.Column[Any]:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "library_agents",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column(
            "toolsets",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("max_turns", sa.Integer(), nullable=True),
        sa.Column("max_cost_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("updated_by_user_id", sa.UUID(), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            "scope IN ('org', 'workspace', 'user')", name=op.f("ck_library_agents_scope")
        ),
        sa.CheckConstraint(
            "(scope = 'org' AND workspace_id IS NULL AND user_id IS NULL)"
            " OR (scope = 'workspace' AND workspace_id IS NOT NULL AND user_id IS NULL)"
            " OR (scope = 'user' AND workspace_id IS NULL AND user_id IS NOT NULL)",
            name=op.f("ck_library_agents_scope_columns"),
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 40", name=op.f("ck_library_agents_name_length")
        ),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 1 AND 300",
            name=op.f("ck_library_agents_description_length"),
        ),
        sa.CheckConstraint(
            "char_length(prompt) BETWEEN 1 AND 8000", name=op.f("ck_library_agents_prompt_length")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(toolsets) = 'array'", name=op.f("ck_library_agents_toolsets_is_array")
        ),
        sa.CheckConstraint(
            "max_turns IS NULL OR max_turns BETWEEN 1 AND 50",
            name=op.f("ck_library_agents_max_turns"),
        ),
        sa.CheckConstraint(
            "max_cost_usd IS NULL OR (max_cost_usd > 0 AND max_cost_usd <= 10)",
            name=op.f("ck_library_agents_max_cost"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_library_agents_version")),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_library_agents_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_library_agents_org_id_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "created_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_library_agents_org_id_created_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "updated_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_library_agents_org_id_updated_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_library_agents_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_library_agents")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_library_agents_org_id_id")),
        sa.UniqueConstraint(
            "org_id",
            "scope",
            "workspace_id",
            "user_id",
            "name",
            name=op.f("uq_library_agents_org_id_scope_workspace_id_user_id_name"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_table(
        "library_agent_versions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("reverts_version", sa.Integer(), nullable=True),
        sa.Column("author_user_id", sa.UUID(), nullable=True),
        _timestamp("created_at"),
        sa.CheckConstraint(
            "source IN ('create', 'edit', 'revert')", name=op.f("ck_library_agent_versions_source")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(spec) = 'object'", name=op.f("ck_library_agent_versions_spec_is_object")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_library_agent_versions_version")),
        sa.ForeignKeyConstraint(
            ["org_id", "agent_id"],
            ["library_agents.org_id", "library_agents.id"],
            name=op.f("fk_library_agent_versions_org_id_agent_id_library_agents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_library_agent_versions_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_library_agent_versions")),
        sa.UniqueConstraint(
            "org_id",
            "agent_id",
            "version",
            name=op.f("uq_library_agent_versions_org_id_agent_id_version"),
        ),
    )
    op.add_column(
        "agent_profiles",
        sa.Column(
            "library_subagents",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_agent_profiles_library_subagents_is_array"),
        "agent_profiles",
        "jsonb_typeof(library_subagents) = 'array'",
    )
    op.add_column(
        "sessions",
        sa.Column("library_agent", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    # A member reads the org agents, the agents of the workspaces that the member can see, and
    # the own personal agents. A version has the visibility of its agent. No policy allows
    # writes: the control plane writes as the table owner.
    for table in ("library_agents", "library_agent_versions"):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO authenticated")
    op.execute(
        "CREATE POLICY library_agents_select ON public.library_agents FOR SELECT"
        " TO authenticated USING (sama_private.is_org_member(org_id) AND ("
        "scope = 'org'"
        " OR (scope = 'workspace' AND sama_private.can_see_workspace(org_id, workspace_id))"
        " OR (scope = 'user' AND user_id IN (SELECT sama_private.my_user_ids()))))"
    )
    op.execute(
        "CREATE POLICY library_agent_versions_select ON public.library_agent_versions"
        " FOR SELECT TO authenticated USING (EXISTS (SELECT 1 FROM public.library_agents a"
        " WHERE a.org_id = library_agent_versions.org_id"
        " AND a.id = library_agent_versions.agent_id))"
    )


def downgrade() -> None:
    op.drop_column("sessions", "library_agent")
    op.drop_constraint(
        op.f("ck_agent_profiles_library_subagents_is_array"), "agent_profiles", type_="check"
    )
    op.drop_column("agent_profiles", "library_subagents")
    op.drop_table("library_agent_versions")
    op.drop_table("library_agents")
