"""The sama vault: encrypted secrets per org (docs/architecture.md, Section 16.3).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "vault_secrets",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("wrapped_key", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
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
        sa.CheckConstraint(
            "name IN ('anthropic_api_key', 'openai_api_key', 'brave_api_key',"
            " 'telegram_bot_token')",
            name=op.f("ck_vault_secrets_name"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_vault_secrets_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vault_secrets")),
        sa.UniqueConstraint("org_id", "name", name=op.f("uq_vault_secrets_org_id_name")),
    )
    # Only org admins see which secrets exist. The rows hold no clear text. No policy allows
    # writes: the control plane writes as the table owner.
    op.execute("ALTER TABLE public.vault_secrets ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.vault_secrets FROM PUBLIC, anon, authenticated")
    op.execute("GRANT SELECT ON TABLE public.vault_secrets TO authenticated")
    op.execute(
        "CREATE POLICY vault_secrets_select ON public.vault_secrets FOR SELECT TO authenticated"
        " USING (sama_private.is_org_admin(org_id))"
    )


def downgrade() -> None:
    op.drop_table("vault_secrets")
