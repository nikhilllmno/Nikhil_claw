"""The browser vault and the URL policy of each org (docs/architecture.md, Sections 14.1 and
16.4; roadmap M5.3).

- ``browser_logins`` holds the saved logins of the browser: a name and the site. The user name
  and the password are in the vault (``browser_login:<id>``), never in this table. A member reads
  the logins of the org and their own logins. An org admin reads all logins of the org.
- ``browser_url_policies`` holds the allow list and the block list of hosts of each org. An org
  admin reads the row.

Only the control plane writes the two tables. The vault accepts the names
``browser_login:<id>``.

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_VAULT_NAMES = (
    "name IN ('anthropic_api_key', 'openai_api_key', 'brave_api_key', 'telegram_bot_token',"
    " 'slack_bot_token', 'slack_signing_secret', 'composio_api_key', 'composio_webhook_secret')"
    " OR name ~ '^connection:[0-9a-f-]{36}$' OR name ~ '^trigger:[0-9a-f-]{36}$'"
)
NEW_VAULT_NAMES = OLD_VAULT_NAMES + " OR name ~ '^browser_login:[0-9a-f-]{36}$'"


def upgrade() -> None:
    op.create_table(
        "browser_logins",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("site", sa.Text(), nullable=False),
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
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 80", name=op.f("ck_browser_logins_name_length")
        ),
        sa.CheckConstraint(
            "char_length(site) BETWEEN 1 AND 300", name=op.f("ck_browser_logins_site_length")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_browser_logins_org_id_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "created_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_browser_logins_org_id_created_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_browser_logins_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_browser_logins")),
    )
    op.create_index(
        "uq_browser_logins_org_name",
        "browser_logins",
        ["org_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
    )
    op.create_index(
        "uq_browser_logins_user_name",
        "browser_logins",
        ["org_id", "user_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_table(
        "browser_url_policies",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column(
            "allow_hosts",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "block_hosts",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("updated_by_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cardinality(allow_hosts) <= 200",
            name=op.f("ck_browser_url_policies_allow_hosts_count"),
        ),
        sa.CheckConstraint(
            "cardinality(block_hosts) <= 200",
            name=op.f("ck_browser_url_policies_block_hosts_count"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "updated_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_browser_url_policies_org_id_updated_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_browser_url_policies_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("org_id", name=op.f("pk_browser_url_policies")),
    )
    # No policy allows writes: the control plane writes as the table owner.
    for table in ("browser_logins", "browser_url_policies"):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO authenticated")
    # A member reads the org logins and their own logins. An admin reads every login of the org.
    op.execute(
        "CREATE POLICY browser_logins_select ON public.browser_logins FOR SELECT"
        " TO authenticated USING (sama_private.is_org_member(org_id) AND ("
        "user_id IS NULL OR user_id IN (SELECT sama_private.my_user_ids())"
        " OR sama_private.is_org_admin(org_id)))"
    )
    op.execute(
        "CREATE POLICY browser_url_policies_select ON public.browser_url_policies FOR SELECT"
        " TO authenticated USING (sama_private.is_org_admin(org_id))"
    )
    op.drop_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", type_="check")
    op.create_check_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", NEW_VAULT_NAMES)


def downgrade() -> None:
    op.execute("DELETE FROM public.vault_secrets WHERE name LIKE 'browser_login:%'")
    op.drop_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", type_="check")
    op.create_check_constraint(op.f("ck_vault_secrets_name"), "vault_secrets", OLD_VAULT_NAMES)
    op.drop_table("browser_url_policies")
    op.drop_index("uq_browser_logins_user_name", table_name="browser_logins")
    op.drop_index("uq_browser_logins_org_name", table_name="browser_logins")
    op.drop_table("browser_logins")
