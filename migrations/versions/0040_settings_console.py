"""The settings console (docs/architecture.md, Sections 11.4, 12.2, and 17.2; roadmap M7.10).

- ``org_role_permissions`` holds the permissions that an org owner turns off or on for the
  roles ``admin`` and ``member``. A permission without a row is allowed, so an existing org keeps
  its behaviour. An org member reads the rows of the org.
- ``trigger_deliveries`` is the delivery log of a trigger: one row for each delivery to a known
  trigger, accepted or not, and for each test send. It has no payload. A user who sees the
  workspace of the trigger reads the rows. ``trigger_events`` stays the table of the accepted
  events and the replay guard.
- ``agent_profiles.reasoning_effort`` is how much the model thinks: ``low``, ``medium``, or
  ``high``. ``NULL`` is the default of the model.

Only the control plane writes the two tables.

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OUTCOMES = (
    "'accepted', 'duplicate', 'bad_signature', 'disabled', 'bad_body', 'rate_limited',"
    " 'rejected', 'test_ok'"
)


def upgrade() -> None:
    op.create_table(
        "org_role_permissions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("permission", sa.Text(), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("updated_by_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('admin', 'member')", name=op.f("ck_org_role_permissions_role")
        ),
        sa.CheckConstraint(
            "char_length(permission) BETWEEN 1 AND 80",
            name=op.f("ck_org_role_permissions_permission_length"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "updated_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_org_role_permissions_org_id_updated_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_org_role_permissions_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_org_role_permissions")),
        sa.UniqueConstraint(
            "org_id",
            "role",
            "permission",
            name=op.f("uq_org_role_permissions_org_id_role_permission"),
        ),
    )
    op.create_table(
        "trigger_deliveries",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("trigger_id", sa.UUID(), nullable=False),
        sa.Column("delivery_id", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("test", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("workflow_instance_id", sa.UUID(), nullable=True),
        sa.CheckConstraint(f"outcome IN ({OUTCOMES})", name=op.f("ck_trigger_deliveries_outcome")),
        sa.CheckConstraint(
            "delivery_id IS NULL OR char_length(delivery_id) <= 200",
            name=op.f("ck_trigger_deliveries_delivery_id_length"),
        ),
        sa.CheckConstraint(
            "error IS NULL OR char_length(error) <= 500",
            name=op.f("ck_trigger_deliveries_error_length"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_trigger_deliveries_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "trigger_id"],
            ["triggers.org_id", "triggers.id"],
            name=op.f("fk_trigger_deliveries_org_id_trigger_id_triggers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trigger_deliveries")),
    )
    op.create_index(
        "ix_trigger_deliveries_trigger_id_received_at",
        "trigger_deliveries",
        ["trigger_id", sa.text("received_at DESC")],
    )
    op.add_column("agent_profiles", sa.Column("reasoning_effort", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_agent_profiles_reasoning_effort"),
        "agent_profiles",
        "reasoning_effort IS NULL OR reasoning_effort IN ('low', 'medium', 'high')",
    )
    # No policy allows writes: the control plane writes as the table owner.
    for table in ("org_role_permissions", "trigger_deliveries"):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO authenticated")
    op.execute(
        "CREATE POLICY org_role_permissions_select ON public.org_role_permissions FOR SELECT"
        " TO authenticated USING (sama_private.is_org_member(org_id))"
    )
    op.execute(
        "CREATE POLICY trigger_deliveries_select ON public.trigger_deliveries FOR SELECT"
        " TO authenticated USING (EXISTS (SELECT 1 FROM public.triggers t"
        " WHERE t.org_id = trigger_deliveries.org_id AND t.id = trigger_deliveries.trigger_id"
        " AND sama_private.can_see_workspace(t.org_id, t.workspace_id)))"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_agent_profiles_reasoning_effort"), "agent_profiles", type_="check")
    op.drop_column("agent_profiles", "reasoning_effort")
    op.drop_index("ix_trigger_deliveries_trigger_id_received_at", table_name="trigger_deliveries")
    op.drop_table("trigger_deliveries")
    op.drop_table("org_role_permissions")
