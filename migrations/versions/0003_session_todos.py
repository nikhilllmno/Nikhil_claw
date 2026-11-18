"""The task list of a session, for the ``todo`` tool (docs/architecture.md, Section 16.7).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_todos",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column(
            "items",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(items) = 'array'", name=op.f("ck_session_todos_items_array")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id", "session_id"],
            ["sessions.org_id", "sessions.workspace_id", "sessions.id"],
            name=op.f("fk_session_todos_org_id_workspace_id_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_session_todos_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("org_id", "session_id", name=op.f("pk_session_todos")),
    )
    # Row-level security as in 0001: members who can read the session can read its list.
    # No policy allows writes: the tool server writes as the table owner.
    op.execute("ALTER TABLE public.session_todos ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.session_todos FROM PUBLIC, anon, authenticated")
    op.execute("GRANT SELECT ON TABLE public.session_todos TO authenticated")
    op.execute(
        "CREATE POLICY session_todos_select ON public.session_todos FOR SELECT TO authenticated"
        " USING (sama_private.can_read_session(org_id, session_id))"
    )


def downgrade() -> None:
    op.drop_table("session_todos")
