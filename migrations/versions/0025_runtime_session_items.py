"""Runtime session items: the conversation of the OpenAI runtime (docs/architecture.md,
Section 5.3).

``runtime_session_items`` has the Responses API items of each session that runs on the OpenAI
Agents SDK. The runner in the sandbox has no database credential, so it reads and writes the
items through the tool server (``POST /session-items``) with its run token.

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "runtime_session_items"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("conversation", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("item", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("seq >= 0", name=op.f("ck_runtime_session_items_seq")),
        sa.CheckConstraint(
            "char_length(conversation) BETWEEN 1 AND 100",
            name=op.f("ck_runtime_session_items_conversation"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id"],
            ["sessions.org_id", "sessions.id"],
            name=op.f("fk_runtime_session_items_org_id_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_runtime_session_items_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runtime_session_items")),
        sa.UniqueConstraint(
            "session_id",
            "conversation",
            "seq",
            name=op.f("uq_runtime_session_items_session_id_conversation_seq"),
        ),
    )
    op.create_index(
        op.f("ix_runtime_session_items_org_id_session_id"), TABLE, ["org_id", "session_id"]
    )

    # A member reads the items of the sessions that the member can see, as the messages. No
    # policy allows a write: the control plane writes as the table owner.
    op.execute(f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE ALL ON TABLE public.{TABLE} FROM PUBLIC, anon, authenticated")
    op.execute(f"GRANT SELECT ON TABLE public.{TABLE} TO authenticated")
    op.execute(
        f"CREATE POLICY {TABLE}_select ON public.{TABLE} FOR SELECT TO authenticated"
        " USING (sama_private.can_read_session(org_id, session_id))"
    )


def downgrade() -> None:
    op.drop_table(TABLE)
