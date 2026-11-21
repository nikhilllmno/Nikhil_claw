"""Busy controls and handoff (docs/architecture.md, Sections 4.3 and 11.1).

``run_steers`` holds the notes of ``/steer`` for a running run. The tool gate gives the pending
notes to the runtime at the next tool call. When the run completes first, the notes become a
new message of the session.

``session_handoffs`` has one row for each ``/handoff`` of a private session to another channel.
``run_ids`` are the open runs that the handoff gave to the target chat. Only the follower of
the newest handoff of a run sends the output of the run.

A member reads the rows of the sessions that the member can see, like ``context_pins``. Only
the control plane writes the tables.

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHANNELS = "('web', 'slack', 'telegram', 'extension')"


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "run_steers",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requeued_run_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'requeued', 'dropped')",
            name=op.f("ck_run_steers_status"),
        ),
        sa.CheckConstraint(
            "char_length(text) BETWEEN 1 AND 4000", name=op.f("ck_run_steers_text_length")
        ),
        sa.CheckConstraint(
            "(status = 'delivered') = (delivered_at IS NOT NULL)",
            name=op.f("ck_run_steers_delivered"),
        ),
        sa.CheckConstraint(
            "(status = 'requeued') = (requeued_run_id IS NOT NULL)",
            name=op.f("ck_run_steers_requeued"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id"],
            ["sessions.org_id", "sessions.id"],
            name=op.f("fk_run_steers_org_id_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            name=op.f("fk_run_steers_org_id_session_id_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_run_steers_org_id_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_run_steers_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_steers")),
    )
    op.create_index(
        op.f("ix_run_steers_org_id_run_id_created_at"),
        "run_steers",
        ["org_id", "run_id", "created_at"],
    )
    op.create_index(
        op.f("ix_run_steers_run_id"),
        "run_steers",
        ["run_id"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "session_handoffs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("from_channel", sa.Text(), nullable=False),
        sa.Column("from_key", sa.Text(), nullable=True),
        sa.Column("to_channel", sa.Text(), nullable=False),
        sa.Column("to_key", sa.Text(), nullable=True),
        sa.Column(
            "run_ids",
            postgresql.ARRAY(sa.UUID()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint(
            f"from_channel IN {CHANNELS}", name=op.f("ck_session_handoffs_from_channel")
        ),
        sa.CheckConstraint(
            f"to_channel IN {CHANNELS}", name=op.f("ck_session_handoffs_to_channel")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id", "session_id"],
            ["sessions.org_id", "sessions.workspace_id", "sessions.id"],
            name=op.f("fk_session_handoffs_org_id_workspace_id_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_session_handoffs_org_id_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_session_handoffs_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_handoffs")),
    )
    op.create_index(
        op.f("ix_session_handoffs_org_id_session_id_id"),
        "session_handoffs",
        ["org_id", "session_id", "id"],
    )

    # A member reads the notes and the handoffs of the sessions that the member can see. No
    # policy allows a write: the control plane writes as the table owner.
    for table in ("run_steers", "session_handoffs"):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO authenticated")
        op.execute(
            f"CREATE POLICY {table}_select ON public.{table} FOR SELECT TO authenticated"
            " USING (sama_private.can_read_session(org_id, session_id))"
        )


def downgrade() -> None:
    op.drop_index(op.f("ix_session_handoffs_org_id_session_id_id"), table_name="session_handoffs")
    op.drop_table("session_handoffs")
    op.drop_index(op.f("ix_run_steers_run_id"), table_name="run_steers")
    op.drop_index(op.f("ix_run_steers_org_id_run_id_created_at"), table_name="run_steers")
    op.drop_table("run_steers")
