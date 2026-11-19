"""Integrations: each org connects its own chat apps (docs/architecture.md, Section 11.6).

The links of the old shared Telegram bot are deleted. That bot never went live, and a link
without an integration cannot work.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHANNEL_TABLES = ("channel_identities", "channel_groups", "pairing_codes")
OLD_VAULT_NAMES = (
    "name IN ('anthropic_api_key', 'openai_api_key', 'brave_api_key', 'telegram_bot_token')"
)
NEW_VAULT_NAMES = (
    "name IN ('anthropic_api_key', 'openai_api_key', 'brave_api_key', 'telegram_bot_token',"
    " 'slack_bot_token', 'slack_signing_secret')"
)


def upgrade() -> None:
    op.create_table(
        "integrations",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("secret_hash", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.CheckConstraint("kind IN ('telegram', 'slack')", name=op.f("ck_integrations_kind")),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'failed')", name=op.f("ck_integrations_status")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "created_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_integrations_org_id_created_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_integrations_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integrations")),
        sa.UniqueConstraint("kind", "external_id", name=op.f("uq_integrations_kind_external_id")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_integrations_org_id_id")),
        sa.UniqueConstraint("org_id", "kind", name=op.f("uq_integrations_org_id_kind")),
    )
    # Every member sees which chat apps the org connected. The row has no clear text. No
    # policy allows writes: the control plane writes as the table owner.
    op.execute("ALTER TABLE public.integrations ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.integrations FROM PUBLIC, anon, authenticated")
    op.execute("GRANT SELECT ON TABLE public.integrations TO authenticated")
    op.execute(
        "CREATE POLICY integrations_select ON public.integrations FOR SELECT TO authenticated"
        " USING (sama_private.is_org_member(org_id))"
    )

    # The links of the shared bot have no integration.
    for table in CHANNEL_TABLES:
        op.execute(f"DELETE FROM public.{table}")  # noqa: S608
    op.execute("DELETE FROM public.session_bindings WHERE channel = 'telegram'")
    for table in CHANNEL_TABLES:
        op.add_column(table, sa.Column("integration_id", sa.UUID(), nullable=False))
        op.create_foreign_key(
            op.f(f"fk_{table}_org_id_integration_id_integrations"),
            table,
            "integrations",
            ["org_id", "integration_id"],
            ["org_id", "id"],
            ondelete="CASCADE",
        )
    for table in ("channel_identities", "channel_groups"):
        op.drop_constraint(op.f(f"uq_{table}_channel_external_id"), table, type_="unique")
        op.create_unique_constraint(
            op.f(f"uq_{table}_integration_id_external_id"), table, ["integration_id", "external_id"]
        )

    op.drop_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", type_="check")
    op.create_check_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", NEW_VAULT_NAMES)


def downgrade() -> None:
    op.execute("DELETE FROM public.vault_secrets WHERE name LIKE 'slack_%'")
    op.drop_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", type_="check")
    op.create_check_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", OLD_VAULT_NAMES)
    for table in CHANNEL_TABLES:
        op.execute(f"DELETE FROM public.{table}")  # noqa: S608
    op.execute("DELETE FROM public.session_bindings WHERE channel = 'telegram'")
    for table in ("channel_identities", "channel_groups"):
        op.drop_constraint(op.f(f"uq_{table}_integration_id_external_id"), table, type_="unique")
        op.create_unique_constraint(
            op.f(f"uq_{table}_channel_external_id"), table, ["channel", "external_id"]
        )
    for table in CHANNEL_TABLES:
        op.drop_constraint(
            op.f(f"fk_{table}_org_id_integration_id_integrations"), table, type_="foreignkey"
        )
        op.drop_column(table, "integration_id")
    op.drop_table("integrations")
