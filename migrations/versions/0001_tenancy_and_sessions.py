"""Tenancy and session tables with row-level security (docs/architecture.md, Section 17.1).

Revision ID: 0001
Revises:
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "orgs",
    "users",
    "workspaces",
    "memberships",
    "agent_profiles",
    "sessions",
    "runs",
    "messages",
    "run_events",
    "tool_calls",
    "session_bindings",
    "audit_log",
)

# Helper functions for the policies. They are SECURITY DEFINER, so a policy can read
# memberships without a policy loop. They live in sama_private, which the Data API does not
# expose, and only `authenticated` can call them.
FUNCTIONS = (
    """
CREATE FUNCTION sama_private.my_user_ids() RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ''
AS $$
    SELECT u.id FROM public.users u WHERE u.auth_user_id = (SELECT auth.uid())
$$
""",
    """
CREATE FUNCTION sama_private.is_org_member(p_org uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ''
AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.memberships m
        WHERE m.org_id = p_org AND m.workspace_id IS NULL
          AND m.user_id IN (SELECT sama_private.my_user_ids())
    )
$$
""",
    """
CREATE FUNCTION sama_private.is_org_admin(p_org uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ''
AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.memberships m
        WHERE m.org_id = p_org AND m.workspace_id IS NULL AND m.role IN ('owner', 'admin')
          AND m.user_id IN (SELECT sama_private.my_user_ids())
    )
$$
""",
    """
CREATE FUNCTION sama_private.can_see_workspace(p_org uuid, p_workspace uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ''
AS $$
    SELECT sama_private.is_org_admin(p_org) OR (
        sama_private.is_org_member(p_org) AND EXISTS (
            SELECT 1 FROM public.memberships m
            WHERE m.org_id = p_org AND m.workspace_id = p_workspace
              AND m.user_id IN (SELECT sama_private.my_user_ids())
        )
    )
$$
""",
    """
CREATE FUNCTION sama_private.session_visible(
    p_org uuid, p_workspace uuid, p_audience text, p_owner uuid
) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ''
AS $$
    SELECT CASE
        WHEN p_audience = 'private' THEN
            p_owner IN (SELECT sama_private.my_user_ids())
            AND sama_private.can_see_workspace(p_org, p_workspace)
        WHEN p_audience = 'shared' THEN sama_private.can_see_workspace(p_org, p_workspace)
        ELSE false
    END
$$
""",
    """
CREATE FUNCTION sama_private.can_read_session(p_org uuid, p_session uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ''
AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.sessions s
        WHERE s.org_id = p_org AND s.id = p_session
          AND sama_private.session_visible(s.org_id, s.workspace_id, s.audience, s.owner_user_id)
    )
$$
""",
    # audit_log is append-only. Deleting an org sets sama.audit_purge to 'on' first.
    """
CREATE FUNCTION sama_private.audit_log_append_only() RETURNS trigger
LANGUAGE plpgsql SET search_path = ''
AS $$
BEGIN
    IF TG_OP = 'DELETE' AND TG_LEVEL = 'ROW'
        AND current_setting('sama.audit_purge', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'audit_log is append-only' USING ERRCODE = 'insufficient_privilege';
END
$$
""",
    """
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA sama_private FROM PUBLIC, anon, authenticated
""",
    """
GRANT USAGE ON SCHEMA sama_private TO authenticated
""",
    """
GRANT EXECUTE ON FUNCTION
    sama_private.my_user_ids(),
    sama_private.is_org_member(uuid),
    sama_private.is_org_admin(uuid),
    sama_private.can_see_workspace(uuid, uuid),
    sama_private.session_visible(uuid, uuid, text, uuid),
    sama_private.can_read_session(uuid, uuid)
TO authenticated
""",
    """
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON public.audit_log
FOR EACH ROW EXECUTE FUNCTION sama_private.audit_log_append_only()
""",
    # TRUNCATE does not fire row triggers, so it gets its own trigger. The purge does not
    # allow it.
    """
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON public.audit_log
FOR EACH STATEMENT EXECUTE FUNCTION sama_private.audit_log_append_only()
""",
)

# One SELECT policy per table for `authenticated`. No policy exists for `anon`, and no
# policy allows writes: the control plane writes as the table owner.
POLICIES = {
    "orgs": "sama_private.is_org_member(id)",
    "users": "sama_private.is_org_member(org_id)",
    "workspaces": "sama_private.can_see_workspace(org_id, id)",
    "memberships": "sama_private.is_org_member(org_id)",
    "agent_profiles": (
        "sama_private.is_org_member(org_id) AND (workspace_id IS NULL"
        " OR sama_private.can_see_workspace(org_id, workspace_id))"
    ),
    "sessions": "sama_private.session_visible(org_id, workspace_id, audience, owner_user_id)",
    "runs": "sama_private.can_read_session(org_id, session_id)",
    "messages": "sama_private.can_read_session(org_id, session_id)",
    "run_events": "sama_private.can_read_session(org_id, session_id)",
    "tool_calls": "sama_private.can_read_session(org_id, session_id)",
    "session_bindings": "sama_private.can_read_session(org_id, session_id)",
    "audit_log": "sama_private.is_org_admin(org_id)",
}


# Supabase gives `anon` and `authenticated` all rights on new objects in `public`. Sama takes
# these defaults away for the role that runs the migrations, so a table, view, sequence, or
# function that a later migration adds is closed until that migration grants rights.
DEFAULT_KINDS = ("TABLES", "SEQUENCES", "FUNCTIONS")


def _close_defaults() -> None:
    for kind in DEFAULT_KINDS:
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON {kind}"
            " FROM anon, authenticated"
        )


def _secure_tables() -> None:
    _close_defaults()
    op.execute("CREATE SCHEMA IF NOT EXISTS sama_private")
    op.execute("REVOKE ALL ON SCHEMA sama_private FROM PUBLIC")
    for statement in FUNCTIONS:
        op.execute(statement)
    for table in TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO authenticated")
        op.execute(
            f"CREATE POLICY {table}_select ON public.{table} FOR SELECT TO authenticated"
            f" USING ({POLICIES[table]})"
        )
    # Identity sequences get the Supabase default rights too.
    op.execute(
        "REVOKE ALL ON SEQUENCE public.messages_seq_seq, public.run_events_id_seq,"
        " public.audit_log_id_seq FROM PUBLIC, anon, authenticated"
    )


def upgrade() -> None:
    _create_tables()
    _secure_tables()


def downgrade() -> None:
    # Gives back the Supabase defaults that _close_defaults took away.
    for kind in DEFAULT_KINDS:
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON {kind} TO anon, authenticated"
        )
    _drop_tables()
    op.execute(
        "DROP FUNCTION sama_private.audit_log_append_only(),"
        " sama_private.can_read_session(uuid, uuid),"
        " sama_private.session_visible(uuid, uuid, text, uuid),"
        " sama_private.can_see_workspace(uuid, uuid), sama_private.is_org_admin(uuid),"
        " sama_private.is_org_member(uuid), sama_private.my_user_ids()"
    )


def _create_tables() -> None:
    op.create_table(
        "orgs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("plan", sa.Text(), server_default="team", nullable=False),
        sa.Column("region", sa.Text(), server_default="ap-south-1", nullable=False),
        sa.Column(
            "limits",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orgs")),
        sa.UniqueConstraint("slug", name=op.f("uq_orgs_slug")),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("actor_kind", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "actor_kind IN ('user', 'agent', 'system')", name=op.f("ck_audit_log_actor_kind")
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_audit_log_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index(
        op.f("ix_audit_log_org_id_created_at"), "audit_log", ["org_id", "created_at"], unique=False
    )
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("auth_user_id", sa.UUID(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_users_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("org_id", "auth_user_id", name=op.f("uq_users_org_id_auth_user_id")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_users_org_id_id")),
    )
    op.create_index(op.f("ix_users_auth_user_id"), "users", ["auth_user_id"], unique=False)
    op.create_table(
        "workspaces",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_workspaces_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_workspaces_org_id_id")),
        sa.UniqueConstraint("org_id", "slug", name=op.f("uq_workspaces_org_id_slug")),
    )
    op.create_table(
        "agent_profiles",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("soul", sa.Text(), server_default="", nullable=False),
        sa.Column("runtime", sa.Text(), server_default="claude_agent_sdk", nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column(
            "toolsets",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "skills",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "budget",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
            "runtime IN ('claude_agent_sdk', 'openai_agents_sdk', 'fake')",
            name=op.f("ck_agent_profiles_runtime"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_agent_profiles_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_agent_profiles_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_profiles")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_agent_profiles_org_id_id")),
    )
    op.create_table(
        "memberships",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'member')", name=op.f("ck_memberships_role")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_memberships_org_id_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_memberships_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_memberships_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memberships")),
        sa.UniqueConstraint(
            "user_id",
            "workspace_id",
            name=op.f("uq_memberships_user_id_workspace_id"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        op.f("ix_memberships_org_id_user_id"), "memberships", ["org_id", "user_id"], unique=False
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("agent_profile_id", sa.UUID(), nullable=False),
        sa.Column("audience", sa.Text(), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=True),
        sa.Column("parent_session_id", sa.UUID(), nullable=True),
        sa.Column("lineage", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
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
            "audience <> 'private' OR owner_user_id IS NOT NULL",
            name=op.f("ck_sessions_private_has_owner"),
        ),
        sa.CheckConstraint("audience IN ('private', 'shared')", name=op.f("ck_sessions_audience")),
        sa.CheckConstraint(
            "lineage IS NULL OR lineage IN ('compaction', 'branch', 'retry', 'delegation')",
            name=op.f("ck_sessions_lineage"),
        ),
        sa.CheckConstraint("status IN ('active', 'archived')", name=op.f("ck_sessions_status")),
        sa.CheckConstraint(
            "(parent_session_id IS NULL) = (lineage IS NULL)",
            name=op.f("ck_sessions_lineage_has_parent"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            name=op.f("fk_sessions_org_id_agent_profile_id_agent_profiles"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "owner_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_sessions_org_id_owner_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "parent_session_id"],
            ["sessions.org_id", "sessions.id"],
            name=op.f("fk_sessions_org_id_parent_session_id_sessions"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_sessions_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_sessions_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_sessions_org_id_id")),
        sa.UniqueConstraint(
            "org_id", "workspace_id", "id", name=op.f("uq_sessions_org_id_workspace_id_id")
        ),
    )
    op.create_index(
        op.f("ix_sessions_org_id_workspace_id"),
        "sessions",
        ["org_id", "workspace_id"],
        unique=False,
    )
    op.create_index(op.f("ix_sessions_owner_user_id"), "sessions", ["owner_user_id"], unique=False)
    op.create_table(
        "runs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("agent_profile_id", sa.UUID(), nullable=False),
        sa.Column("triggered_by_user_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("runtime", sa.Text(), nullable=False),
        sa.Column("config_fingerprint", sa.Text(), nullable=True),
        sa.Column(
            "budget",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "usage",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("runtime_state", sa.Text(), nullable=True),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "runtime IN ('claude_agent_sdk', 'openai_agents_sdk', 'fake')",
            name=op.f("ck_runs_runtime"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'completed', 'failed', 'cancelled')",
            name=op.f("ck_runs_status"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            name=op.f("fk_runs_org_id_agent_profile_id_agent_profiles"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id"],
            ["sessions.org_id", "sessions.id"],
            name=op.f("fk_runs_org_id_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "triggered_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_runs_org_id_triggered_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_runs_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_runs_org_id_id")),
        sa.UniqueConstraint(
            "org_id", "session_id", "id", name=op.f("uq_runs_org_id_session_id_id")
        ),
    )
    op.create_index(
        op.f("ix_runs_org_id_session_id"), "runs", ["org_id", "session_id"], unique=False
    )
    op.create_table(
        "session_bindings",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("external_key", sa.Text(), nullable=False),
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
            "channel IN ('web', 'slack', 'telegram', 'extension')",
            name=op.f("ck_session_bindings_channel"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "created_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_session_bindings_org_id_created_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id", "session_id"],
            ["sessions.org_id", "sessions.workspace_id", "sessions.id"],
            name=op.f("fk_session_bindings_org_id_workspace_id_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_session_bindings_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_bindings")),
        sa.UniqueConstraint(
            "channel", "external_key", name=op.f("uq_session_bindings_channel_external_key")
        ),
    )
    op.create_index(
        op.f("ix_session_bindings_org_id_session_id"),
        "session_bindings",
        ["org_id", "session_id"],
        unique=False,
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("author_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "content",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'system', 'tool')", name=op.f("ck_messages_role")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "author_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_messages_org_id_author_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            name=op.f("fk_messages_org_id_session_id_run_id_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id"],
            ["sessions.org_id", "sessions.id"],
            name=op.f("fk_messages_org_id_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_messages_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
    )
    op.create_index(
        op.f("ix_messages_session_id_seq"), "messages", ["session_id", "seq"], unique=False
    )
    op.create_table(
        "run_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            name=op.f("fk_run_events_org_id_session_id_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id"],
            ["sessions.org_id", "sessions.id"],
            name=op.f("fk_run_events_org_id_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_run_events_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_events")),
        sa.UniqueConstraint("run_id", "seq", name=op.f("uq_run_events_run_id_seq")),
    )
    op.create_index(
        op.f("ix_run_events_org_id_session_id"),
        "run_events",
        ["org_id", "session_id"],
        unique=False,
    )
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("call_id", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column(
            "arguments",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="started", nullable=False),
        sa.Column("result_preview", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('allow', 'ask', 'deny')",
            name=op.f("ck_tool_calls_decision"),
        ),
        sa.CheckConstraint(
            "status IN ('started', 'finished', 'failed', 'denied')",
            name=op.f("ck_tool_calls_status"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id", "run_id"],
            ["runs.org_id", "runs.session_id", "runs.id"],
            name=op.f("fk_tool_calls_org_id_session_id_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id"],
            ["sessions.org_id", "sessions.id"],
            name=op.f("fk_tool_calls_org_id_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_tool_calls_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_calls")),
        sa.UniqueConstraint("run_id", "call_id", name=op.f("uq_tool_calls_run_id_call_id")),
    )
    op.create_index(
        op.f("ix_tool_calls_org_id_session_id"),
        "tool_calls",
        ["org_id", "session_id"],
        unique=False,
    )


def _drop_tables() -> None:
    op.drop_index(op.f("ix_tool_calls_org_id_session_id"), table_name="tool_calls")
    op.drop_table("tool_calls")
    op.drop_index(op.f("ix_run_events_org_id_session_id"), table_name="run_events")
    op.drop_table("run_events")
    op.drop_index(op.f("ix_messages_session_id_seq"), table_name="messages")
    op.drop_table("messages")
    op.drop_index(op.f("ix_session_bindings_org_id_session_id"), table_name="session_bindings")
    op.drop_table("session_bindings")
    op.drop_index(op.f("ix_runs_org_id_session_id"), table_name="runs")
    op.drop_table("runs")
    op.drop_index(op.f("ix_sessions_owner_user_id"), table_name="sessions")
    op.drop_index(op.f("ix_sessions_org_id_workspace_id"), table_name="sessions")
    op.drop_table("sessions")
    op.drop_index(op.f("ix_memberships_org_id_user_id"), table_name="memberships")
    op.drop_table("memberships")
    op.drop_table("agent_profiles")
    op.drop_table("workspaces")
    op.drop_index(op.f("ix_users_auth_user_id"), table_name="users")
    op.drop_table("users")
    op.drop_index(op.f("ix_audit_log_org_id_created_at"), table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("orgs")
