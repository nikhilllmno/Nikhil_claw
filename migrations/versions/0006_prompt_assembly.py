"""Prompt assembly: the frozen run config of a session and the context files
(docs/architecture.md, Section 6.1).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Members read the org files and the files of the workspaces that they can see, as for
# agent profiles. A profile file also needs its profile: the subquery runs with the RLS of
# agent_profiles, so a file of a profile in a hidden workspace stays hidden. No policy allows
# writes: the control plane writes as the table owner.
POLICY = (
    "sama_private.is_org_member(org_id)"
    " AND (workspace_id IS NULL OR sama_private.can_see_workspace(org_id, workspace_id))"
    " AND (agent_profile_id IS NULL OR EXISTS (SELECT 1 FROM public.agent_profiles AS p"
    " WHERE p.org_id = context_files.org_id AND p.id = context_files.agent_profile_id))"
)


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("run_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("sessions", sa.Column("config_fingerprint", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_sessions_config_has_fingerprint"),
        "sessions",
        "(run_config IS NULL) = (config_fingerprint IS NULL)",
    )
    op.create_table(
        "context_files",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("agent_profile_id", sa.UUID(), nullable=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
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
            "length(path) BETWEEN 1 AND 200 AND path !~ '[\"<>\\r\\n]'",
            name=op.f("ck_context_files_path_text"),
        ),
        sa.CheckConstraint(
            "length(content) <= 100000", name=op.f("ck_context_files_content_length")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            name=op.f("fk_context_files_org_id_agent_profile_id_agent_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_context_files_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_context_files_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_files")),
        sa.UniqueConstraint(
            "org_id",
            "workspace_id",
            "agent_profile_id",
            "path",
            name=op.f("uq_context_files_org_id_workspace_id_agent_profile_id_path"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.execute("ALTER TABLE public.context_files ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.context_files FROM PUBLIC, anon, authenticated")
    op.execute("GRANT SELECT ON TABLE public.context_files TO authenticated")
    op.execute(
        "CREATE POLICY context_files_select ON public.context_files FOR SELECT TO authenticated"
        f" USING ({POLICY})"
    )


def downgrade() -> None:
    op.drop_table("context_files")
    op.drop_constraint(op.f("ck_sessions_config_has_fingerprint"), "sessions", type_="check")
    op.drop_column("sessions", "config_fingerprint")
    op.drop_column("sessions", "run_config")
