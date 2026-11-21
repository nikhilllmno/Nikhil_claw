"""Data controls: retention per org and the purge outbox (docs/architecture.md, Section 17.3).

``orgs.retention_days`` is the retention setting of the org. Null means the default of the
plan. The retention sweep deletes the sessions that had no activity for that many days.

``data_purges`` is an outbox. A user delete or an org delete writes one row for each cleanup
outside Postgres in the same transaction: the E2B sandboxes of the deleted sessions and the
Langfuse traces of the deleted runs. The purge worker does the cleanup and retries it. The
table has no foreign key to ``orgs``, so its rows stay after an org delete. Only org owners
and admins read the rows. Only the control plane writes the table.

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.add_column("orgs", sa.Column("retention_days", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_orgs_retention_days"),
        "orgs",
        "retention_days IS NULL OR retention_days BETWEEN 1 AND 3650",
    )

    op.create_table(
        "data_purges",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "refs", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('sandboxes', 'traces')", name=op.f("ck_data_purges_kind")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_purges")),
    )
    op.create_index(
        op.f("ix_data_purges_next_attempt_at"),
        "data_purges",
        ["next_attempt_at"],
        postgresql_where=sa.text("done_at IS NULL"),
    )
    op.create_index(op.f("ix_data_purges_org_id"), "data_purges", ["org_id"])

    # An org admin reads the purges of the org, like the audit log. No policy allows a write.
    op.execute("ALTER TABLE public.data_purges ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.data_purges FROM PUBLIC, anon, authenticated")
    op.execute("GRANT SELECT ON TABLE public.data_purges TO authenticated")
    op.execute(
        "CREATE POLICY data_purges_select ON public.data_purges FOR SELECT TO authenticated"
        " USING (sama_private.is_org_admin(org_id))"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_data_purges_org_id"), table_name="data_purges")
    op.drop_index(op.f("ix_data_purges_next_attempt_at"), table_name="data_purges")
    op.drop_table("data_purges")
    op.drop_constraint(op.f("ck_orgs_retention_days"), "orgs", type_="check")
    op.drop_column("orgs", "retention_days")
