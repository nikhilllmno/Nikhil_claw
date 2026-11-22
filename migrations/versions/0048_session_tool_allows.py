"""Migration 0048: the approval scope ``session`` (docs/architecture.md, Section 16.1).

``session_tool_allows`` holds the tools that one user allowed for the rest of one session
("Yes, and do not ask again this session"). The tool gate reads each row as a policy rule of the
level ``session``. A ``deny`` rule, an org or profile ``ask`` rule, the hardline list, and plan
mode all win over it. The rule ends with the session.

A member reads the rows of the sessions that the member can see, like ``session_plans``. Only
the control plane writes the table.

Revision ID: 0048
Revises: 0047
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "session_tool_allows"


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("approval_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint(
            "length(tool) BETWEEN 1 AND 200", name=op.f("ck_session_tool_allows_tool_length")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id"],
            ["sessions.org_id", "sessions.id"],
            name=op.f("fk_session_tool_allows_org_id_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_session_tool_allows_org_id_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_session_tool_allows_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_tool_allows")),
        sa.UniqueConstraint(
            "session_id",
            "user_id",
            "tool",
            name=op.f("uq_session_tool_allows_session_id_user_id_tool"),
        ),
    )
    op.create_index(
        op.f("ix_session_tool_allows_org_id_session_id"), TABLE, ["org_id", "session_id"]
    )
    # A member reads the rows of the sessions that the member can see. No policy allows a write:
    # the control plane writes as the table owner.
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE ALL ON TABLE public.{TABLE} FROM PUBLIC, anon, authenticated")
    op.execute(f"GRANT SELECT ON TABLE public.{TABLE} TO authenticated")
    op.execute(
        f"CREATE POLICY {TABLE}_select ON public.{TABLE} FOR SELECT TO authenticated"
        " USING (sama_private.can_read_session(org_id, session_id))"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_session_tool_allows_org_id_session_id"), table_name=TABLE)
    op.drop_table(TABLE)
