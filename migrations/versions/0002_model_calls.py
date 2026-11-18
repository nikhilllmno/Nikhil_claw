"""Model calls of the model proxy (docs/architecture.md, Section 16.6).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_calls",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_write_long_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("web_search_requests", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0"
            " AND cache_write_tokens >= 0 AND cache_write_long_tokens >= 0"
            " AND web_search_requests >= 0 AND cost_usd >= 0 AND duration_ms >= 0",
            name=op.f("ck_model_calls_counts"),
        ),
        sa.CheckConstraint(
            "endpoint IN ('messages', 'count_tokens', 'responses', 'chat_completions')",
            name=op.f("ck_model_calls_endpoint"),
        ),
        sa.CheckConstraint(
            "provider IN ('anthropic', 'openai')", name=op.f("ck_model_calls_provider")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            name=op.f("fk_model_calls_org_id_session_id_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_model_calls_org_id_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id", "session_id"],
            ["sessions.org_id", "sessions.workspace_id", "sessions.id"],
            name=op.f("fk_model_calls_org_id_workspace_id_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_model_calls_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_calls")),
    )
    for columns in (
        ["org_id", "created_at"],
        ["org_id", "run_id"],
        ["org_id", "session_id"],
        ["org_id", "user_id", "created_at"],
    ):
        op.create_index(
            op.f(f"ix_model_calls_{'_'.join(columns)}"), "model_calls", columns, unique=False
        )
    # Row-level security as in 0001: members who can read the session can read its calls.
    # No policy allows writes: the model proxy writes as the table owner.
    op.execute("ALTER TABLE public.model_calls ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.model_calls FROM PUBLIC, anon, authenticated")
    op.execute("GRANT SELECT ON TABLE public.model_calls TO authenticated")
    op.execute(
        "CREATE POLICY model_calls_select ON public.model_calls FOR SELECT TO authenticated"
        " USING (sama_private.can_read_session(org_id, session_id))"
    )
    op.execute("REVOKE ALL ON SEQUENCE public.model_calls_id_seq FROM PUBLIC, anon, authenticated")


def downgrade() -> None:
    op.drop_table("model_calls")
