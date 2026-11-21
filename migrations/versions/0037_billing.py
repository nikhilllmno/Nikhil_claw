"""Billing and quotas (docs/architecture.md, Section 17.5).

``org_billing`` has one row for each org with a Stripe customer: the subscription, its status
and period, and the state of the usage report (an outbox, so a crash never bills the same
usage two times). ``billing_events`` has one row for each Stripe event that sama applied, so a
retry of the same event does nothing.

Org admins read the rows of their org through the Data API. Only the control plane writes the
tables.

The upgrade does not change ``orgs.limits``. The model quotas of the free plan apply only
while billing is on: the control plane adds them at start (``BillingService.start``).

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("org_billing", "billing_events")


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "org_billing",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("stripe_customer_id", sa.Text(), nullable=False),
        sa.Column("stripe_subscription_id", sa.Text(), nullable=True),
        sa.Column("plan", sa.Text(), server_default="free", nullable=False),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("usage_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("usage_reported_cents", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("usage_pending_cents", sa.BigInteger(), nullable=True),
        sa.Column("usage_pending_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("usage_reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checkout_session_id", sa.Text(), nullable=True),
        sa.Column("checkout_url", sa.Text(), nullable=True),
        sa.Column("checkout_plan", sa.Text(), nullable=True),
        sa.Column("checkout_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint(
            "usage_reported_cents >= 0", name=op.f("ck_org_billing_usage_reported_cents")
        ),
        sa.CheckConstraint(
            "usage_pending_cents IS NULL OR usage_pending_cents > usage_reported_cents",
            name=op.f("ck_org_billing_usage_pending_cents"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_org_billing_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_org_billing")),
        sa.UniqueConstraint("org_id", name=op.f("uq_org_billing_org_id")),
        sa.UniqueConstraint("stripe_customer_id", name=op.f("uq_org_billing_stripe_customer_id")),
        sa.UniqueConstraint(
            "stripe_subscription_id", name=op.f("uq_org_billing_stripe_subscription_id")
        ),
    )
    op.create_table(
        "billing_events",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("stripe_event_id", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_billing_events_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_events")),
        sa.UniqueConstraint("stripe_event_id", name=op.f("uq_billing_events_stripe_event_id")),
    )
    op.create_index(
        op.f("ix_billing_events_org_id_created_at"), "billing_events", ["org_id", "created_at"]
    )
    # Org admins read the billing of their org. No policy allows a write: the control plane
    # writes as the table owner.
    for table in TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO authenticated")
        op.execute(
            f"CREATE POLICY {table}_select ON public.{table} FOR SELECT TO authenticated"
            " USING (sama_private.is_org_admin(org_id))"
        )


def downgrade() -> None:
    op.drop_index(op.f("ix_billing_events_org_id_created_at"), table_name="billing_events")
    op.drop_table("billing_events")
    op.drop_table("org_billing")
