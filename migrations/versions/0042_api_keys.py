"""Org API keys for sama as a server (docs/architecture.md, Section 10.7; roadmap M7.6).

``api_keys`` holds the keys of the OpenAI-compatible endpoint and of the sama MCP server for
other agents. Only the SHA-256 hash of a key is here. Org admins read the keys of their org
through the Data API, but not ``key_hash``. Only the control plane writes the table.

Revision ID: 0042
Revises: 0041
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

READABLE = (
    "id, org_id, user_id, workspace_id, agent_profile_id, name, prefix, scopes, approvals,"
    " created_at, expires_at, last_used_at, revoked_at, revoked_by_user_id"
)
"""Every column but ``key_hash``."""


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("agent_profile_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.LargeBinary(), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("approvals", sa.Text(), server_default="deny", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 80", name=op.f("ck_api_keys_name_length")
        ),
        sa.CheckConstraint("octet_length(key_hash) = 32", name=op.f("ck_api_keys_key_hash_length")),
        sa.CheckConstraint(
            "cardinality(scopes) BETWEEN 1 AND 2 AND scopes <@ ARRAY['chat', 'mcp']::text[]",
            name=op.f("ck_api_keys_scopes"),
        ),
        sa.CheckConstraint("approvals IN ('deny', 'pending')", name=op.f("ck_api_keys_approvals")),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_api_keys_org_id_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_api_keys_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            name=op.f("fk_api_keys_org_id_agent_profile_id_agent_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "revoked_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_api_keys_org_id_revoked_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_api_keys_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_api_keys_key_hash")),
    )
    op.create_index(
        op.f("ix_api_keys_org_id_created_at"), "api_keys", ["org_id", "created_at"], unique=False
    )
    # Org admins read the keys of their org, but never the hash. No policy allows a write: the
    # control plane writes as the table owner.
    op.execute("ALTER TABLE public.api_keys ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.api_keys FROM PUBLIC, anon, authenticated")
    op.execute(f"GRANT SELECT ({READABLE}) ON TABLE public.api_keys TO authenticated")
    op.execute(
        "CREATE POLICY api_keys_select ON public.api_keys FOR SELECT TO authenticated"
        " USING (sama_private.is_org_admin(org_id))"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_api_keys_org_id_created_at"), table_name="api_keys")
    op.drop_table("api_keys")
