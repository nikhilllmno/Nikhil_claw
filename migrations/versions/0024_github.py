"""GitHub: the installations of the sama GitHub App, the repository allowlist, and the push
grants (docs/architecture.md, Section 15.1).

Every member of an org sees which installations and repositories the org connected. Only org
admins see the push grants, like the audit log. No policy allows writes: the control plane
writes as the table owner. No row has a token.

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICIES = {
    "github_installations": "sama_private.is_org_member(org_id)",
    "github_repos": "sama_private.is_org_member(org_id)",
    "github_push_grants": "sama_private.is_org_admin(org_id)",
}


def _timestamp(name: str, *, nullable: bool = False, default: bool = True) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.text("now()") if default else None,
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        "github_installations",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("account_login", sa.Text(), nullable=False),
        sa.Column("account_type", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            "account_type IN ('User', 'Organization', 'Enterprise')",
            name=op.f("ck_github_installations_account_type"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "created_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_github_installations_org_id_created_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_github_installations_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_github_installations")),
        sa.UniqueConstraint(
            "installation_id", name=op.f("uq_github_installations_installation_id")
        ),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_github_installations_org_id_id")),
    )
    op.create_table(
        "github_repos",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("installation_ref", sa.UUID(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column(
            "protected_paths",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        _timestamp("created_at"),
        sa.CheckConstraint(
            "full_name = lower(owner || '/' || name)", name=op.f("ck_github_repos_full_name")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(protected_paths) = 'array'",
            name=op.f("ck_github_repos_protected_paths"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "created_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_github_repos_org_id_created_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "installation_ref"],
            ["github_installations.org_id", "github_installations.id"],
            name=op.f("fk_github_repos_org_id_installation_ref_github_installations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_github_repos_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_github_repos")),
        sa.UniqueConstraint("org_id", "full_name", name=op.f("uq_github_repos_org_id_full_name")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_github_repos_org_id_id")),
    )
    op.create_table(
        "github_push_grants",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("repo_id", sa.UUID(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("commit_sha", sa.Text(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        _timestamp("created_at"),
        _timestamp("expires_at", default=False),
        _timestamp("used_at", nullable=True, default=False),
        sa.CheckConstraint(
            "commit_sha ~ '^[0-9a-f]{40}$'", name=op.f("ck_github_push_grants_commit_sha")
        ),
        sa.CheckConstraint("ref LIKE 'refs/heads/sama/%'", name=op.f("ck_github_push_grants_ref")),
        sa.ForeignKeyConstraint(
            ["org_id", "repo_id"],
            ["github_repos.org_id", "github_repos.id"],
            name=op.f("fk_github_push_grants_org_id_repo_id_github_repos"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            name=op.f("fk_github_push_grants_org_id_session_id_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_github_push_grants_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_github_push_grants")),
    )
    op.create_index(
        op.f("ix_github_push_grants_org_id_repo_id_ref_commit_sha"),
        "github_push_grants",
        ["org_id", "repo_id", "ref", "commit_sha"],
    )
    for table, condition in POLICIES.items():
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO authenticated")
        op.execute(
            f"CREATE POLICY {table}_select ON public.{table} FOR SELECT TO authenticated"
            f" USING ({condition})"
        )


def downgrade() -> None:
    op.drop_table("github_push_grants")
    op.drop_table("github_repos")
    op.drop_table("github_installations")
