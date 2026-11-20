"""Context management: compactions, context pins, and compact runs (docs/architecture.md,
Section 6.3).

``session_compactions`` has one row for each compaction of a session. It points to the archive
session that holds the transcript from before the compaction. ``context_pins`` has the texts
that a person pinned to a session. ``runs.compact`` marks a ``/compact`` run. The default
profile gets the toolset ``tool_search``.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The default list after the attachments migration (0015) and the same list with
# ``tool_search``. An exact match keeps the profiles that a person changed.
OLD_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media"]'
)
NEW_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search"]'
)


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.add_column(
        "runs", sa.Column("compact", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.add_column("runs", sa.Column("compact_focus", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_runs_compact_is_chat"), "runs", "NOT compact OR kind = 'chat'"
    )
    op.create_check_constraint(
        op.f("ck_runs_compact_focus"), "runs", "compact OR compact_focus IS NULL"
    )

    op.create_table(
        "session_compactions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("tokens_before", sa.Integer(), nullable=True),
        sa.Column("tokens_after", sa.Integer(), nullable=True),
        sa.Column("archive_session_id", sa.UUID(), nullable=True),
        sa.Column("archive_entries", sa.Integer(), server_default="0", nullable=False),
        sa.Column("archive_truncated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "trigger IN ('auto', 'manual')", name=op.f("ck_session_compactions_trigger")
        ),
        sa.CheckConstraint(
            "(completed_at IS NULL) = (tokens_before IS NULL AND tokens_after IS NULL)",
            name=op.f("ck_session_compactions_completed"),
        ),
        sa.CheckConstraint(
            "(tokens_before IS NULL OR tokens_before >= 0)"
            " AND (tokens_after IS NULL OR tokens_after >= 0)",
            name=op.f("ck_session_compactions_tokens"),
        ),
        sa.CheckConstraint(
            "archive_entries >= 0", name=op.f("ck_session_compactions_archive_entries")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id"],
            ["sessions.org_id", "sessions.id"],
            name=op.f("fk_session_compactions_org_id_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            name=op.f("fk_session_compactions_org_id_session_id_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "archive_session_id"],
            ["sessions.org_id", "sessions.id"],
            name=op.f("fk_session_compactions_org_id_archive_session_id_sessions"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_session_compactions_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_compactions")),
        sa.UniqueConstraint(
            "archive_session_id", name=op.f("uq_session_compactions_archive_session_id")
        ),
    )
    op.create_index(
        op.f("ix_session_compactions_org_id_session_id_created_at"),
        "session_compactions",
        ["org_id", "session_id", "created_at"],
    )
    op.create_index(op.f("ix_session_compactions_run_id"), "session_compactions", ["run_id"])

    op.create_table(
        "context_pins",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), server_default="user", nullable=False),
        sa.Column("injected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("injected_run_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint("source IN ('user', 'message')", name=op.f("ck_context_pins_source")),
        sa.CheckConstraint(
            "char_length(text) BETWEEN 1 AND 2000", name=op.f("ck_context_pins_text_length")
        ),
        sa.CheckConstraint(
            "(injected_at IS NULL) = (injected_run_id IS NULL)",
            name=op.f("ck_context_pins_injected"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id"],
            ["sessions.org_id", "sessions.id"],
            name=op.f("fk_context_pins_org_id_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "created_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_context_pins_org_id_created_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_context_pins_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_pins")),
    )
    op.create_index(
        op.f("ix_context_pins_org_id_session_id_created_at"),
        "context_pins",
        ["org_id", "session_id", "created_at"],
    )

    # A member reads the compactions and the pins of the sessions that the member can see. No
    # policy allows a write: the control plane writes as the table owner.
    for table in ("session_compactions", "context_pins"):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO authenticated")
        op.execute(
            f"CREATE POLICY {table}_select ON public.{table} FOR SELECT TO authenticated"
            " USING (sama_private.can_read_session(org_id, session_id))"
        )

    # The default profile of the first org (M1.15) gets the new toolset.
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{NEW_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{OLD_TOOLSETS}'::jsonb"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{OLD_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{NEW_TOOLSETS}'::jsonb"
    )
    op.drop_table("context_pins")
    op.drop_table("session_compactions")
    # The old schema has no compact run. Old code would read it as a chat run, so no compact
    # run stays open, and none keeps a state that old code could resume from.
    op.execute(
        "UPDATE public.runs SET status = 'cancelled', failure_code = 'cancelled',"
        " failure_message = 'the compact run was removed by a downgrade',"
        " lease_owner = NULL, lease_expires_at = NULL, pending_approval_id = NULL,"
        " finished_at = COALESCE(finished_at, now())"
        " WHERE compact AND status IN ('queued', 'running', 'paused')"
    )
    op.drop_constraint(op.f("ck_runs_compact_focus"), "runs", type_="check")
    op.drop_constraint(op.f("ck_runs_compact_is_chat"), "runs", type_="check")
    op.drop_column("runs", "compact_focus")
    op.drop_column("runs", "compact")
