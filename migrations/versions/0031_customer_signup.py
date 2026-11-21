"""Customer sign-up (docs/architecture.md, Section 17.2).

``orgs.created_by_auth_user_id`` is the Supabase Auth user who made the org with self-serve
sign-up. The rate limits of org creation count these rows. An org of ``bootstrap`` has null.

``org_invites`` has one row for each invite to an org. The invite link holds a random token.
The table keeps only the SHA-256 hash of the token. Org admins read the rows of their org
through the Data API, but not ``token_hash``. Only the control plane writes the table.

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

READABLE = (
    "id, org_id, email, role, workspace_id, invited_by_user_id, expires_at, accepted_at,"
    " accepted_user_id, revoked_at, created_at"
)
"""Every column but ``token_hash``."""
PENDING = "accepted_at IS NULL AND revoked_at IS NULL"


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.add_column("orgs", sa.Column("created_by_auth_user_id", sa.UUID(), nullable=True))
    op.create_index(
        op.f("ix_orgs_created_by_auth_user_id_created_at"),
        "orgs",
        ["created_by_auth_user_id", "created_at"],
    )
    op.create_index(op.f("ix_orgs_created_at"), "orgs", ["created_at"])

    op.create_table(
        "org_invites",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("invited_by_user_id", sa.UUID(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_user_id", sa.UUID(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint("role IN ('admin', 'member')", name=op.f("ck_org_invites_role")),
        sa.CheckConstraint(
            "email = lower(email) AND char_length(email) BETWEEN 3 AND 320",
            name=op.f("ck_org_invites_email"),
        ),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32", name=op.f("ck_org_invites_token_hash_length")
        ),
        sa.CheckConstraint(
            "(accepted_at IS NULL) = (accepted_user_id IS NULL)",
            name=op.f("ck_org_invites_accepted"),
        ),
        sa.CheckConstraint(
            "accepted_at IS NULL OR revoked_at IS NULL", name=op.f("ck_org_invites_one_end")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_org_invites_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "invited_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_org_invites_org_id_invited_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "accepted_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_org_invites_org_id_accepted_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_org_invites_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_org_invites")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_org_invites_token_hash")),
    )
    op.create_index(
        op.f("ix_org_invites_org_id_created_at"), "org_invites", ["org_id", "created_at"]
    )
    # One open invite for each email in an org. A new invite revokes the old one first.
    op.create_index(
        op.f("ix_org_invites_org_id_email"),
        "org_invites",
        ["org_id", "email"],
        unique=True,
        postgresql_where=sa.text(PENDING),
    )
    op.create_index(
        op.f("ix_org_invites_email"),
        "org_invites",
        ["email"],
        postgresql_where=sa.text(PENDING),
    )
    # Org admins read the invites of their org, but never the token hash. No policy allows a
    # write: the control plane writes as the table owner.
    op.execute("ALTER TABLE public.org_invites ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.org_invites FROM PUBLIC, anon, authenticated")
    op.execute(f"GRANT SELECT ({READABLE}) ON TABLE public.org_invites TO authenticated")
    op.execute(
        "CREATE POLICY org_invites_select ON public.org_invites FOR SELECT TO authenticated"
        " USING (sama_private.is_org_admin(org_id))"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_org_invites_email"), table_name="org_invites")
    op.drop_index(op.f("ix_org_invites_org_id_email"), table_name="org_invites")
    op.drop_index(op.f("ix_org_invites_org_id_created_at"), table_name="org_invites")
    op.drop_table("org_invites")
    op.drop_index(op.f("ix_orgs_created_at"), table_name="orgs")
    op.drop_index(op.f("ix_orgs_created_by_auth_user_id_created_at"), table_name="orgs")
    op.drop_column("orgs", "created_by_auth_user_id")
