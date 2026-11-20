"""Scheduler, cron jobs, and delivery (docs/architecture.md, Section 12.1).

``jobs`` has the scheduled jobs. ``job_fires`` has one row for each fire: the unique key
``(job_id, scheduled_for)`` makes a fire run once, also when two schedulers tick at the same
time. ``deliveries`` has one row for each message that a fire sends, with its receipt.
``runs.model`` is the model pin of a job run. The default profile gets the toolset ``cron``.

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The default list after the delegation and task board migration (0023) and the same list with
# ``cron``. An exact match keeps the profiles that a person changed.
OLD_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search", "subagents", "connections", "integrations", "delegation", "tasks"]'
)
NEW_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search", "subagents", "connections", "integrations", "delegation", "tasks", "cron"]'
)


def _now() -> sa.TextClause:
    return sa.text("now()")


def _array() -> sa.TextClause:
    return sa.text("'[]'::jsonb")


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column("runs", sa.Column("model", sa.Text(), nullable=True))

    op.create_table(
        "jobs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("agent_profile_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("schedule", _jsonb(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("prompt", sa.Text(), server_default="", nullable=False),
        sa.Column("skills", _jsonb(), server_default=_array(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("mode", sa.Text(), server_default="agent", nullable=False),
        sa.Column("script", sa.Text(), nullable=True),
        sa.Column("deliver", sa.Text(), server_default="origin", nullable=False),
        sa.Column("failure_deliver", sa.Text(), nullable=True),
        sa.Column("preapproved_tools", _jsonb(), server_default=_array(), nullable=False),
        sa.Column("attach_to_session", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("origin_session_id", sa.UUID(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("approved_version", sa.Integer(), nullable=True),
        sa.Column("approved_hash", sa.Text(), nullable=True),
        sa.Column("approved_by_user_id", sa.UUID(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("paused_reason", sa.Text(), nullable=True),
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_kind", sa.Text(), server_default="user", nullable=False),
        sa.Column("created_by_run_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'pending_approval', 'removed')",
            name=op.f("ck_jobs_status"),
        ),
        sa.CheckConstraint("mode IN ('agent', 'script')", name=op.f("ck_jobs_mode")),
        sa.CheckConstraint(
            "created_by_kind IN ('user', 'agent')", name=op.f("ck_jobs_created_by_kind")
        ),
        sa.CheckConstraint("(mode = 'script') = (script IS NOT NULL)", name=op.f("ck_jobs_script")),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 200", name=op.f("ck_jobs_name_length")),
        sa.CheckConstraint(
            "jsonb_typeof(schedule) = 'object'", name=op.f("ck_jobs_schedule_is_object")
        ),
        sa.CheckConstraint("jsonb_typeof(skills) = 'array'", name=op.f("ck_jobs_skills_is_array")),
        sa.CheckConstraint(
            "jsonb_typeof(preapproved_tools) = 'array'",
            name=op.f("ck_jobs_preapproved_tools_is_array"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_jobs_version")),
        sa.CheckConstraint(
            "approved_version IS NULL OR approved_version BETWEEN 1 AND version",
            name=op.f("ck_jobs_approved_version"),
        ),
        sa.CheckConstraint(
            "(approved_version IS NULL) = (approved_hash IS NULL)",
            name=op.f("ck_jobs_approved_has_hash"),
        ),
        sa.CheckConstraint("consecutive_failures >= 0", name=op.f("ck_jobs_consecutive_failures")),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_jobs_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "owner_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_jobs_org_id_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            name=op.f("fk_jobs_org_id_agent_profile_id_agent_profiles"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id"],
            ["sessions.org_id", "sessions.id"],
            name=op.f("fk_jobs_org_id_session_id_sessions"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "origin_session_id"],
            ["sessions.org_id", "sessions.id"],
            name=op.f("fk_jobs_org_id_origin_session_id_sessions"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "approved_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_jobs_org_id_approved_by_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_jobs_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_jobs_org_id_id")),
    )
    op.create_index(op.f("ix_jobs_org_id_owner_user_id"), "jobs", ["org_id", "owner_user_id"])
    op.create_index(
        "ix_jobs_due", "jobs", ["next_fire_at"], postgresql_where=sa.text("status = 'active'")
    )

    op.create_table(
        "job_fires",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("job_version", sa.Integer(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("manual", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("deliver", sa.Text(), server_default="none", nullable=False),
        sa.Column("failure_deliver", sa.Text(), nullable=True),
        sa.Column("script", sa.Text(), nullable=True),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'skipped')",
            name=op.f("ck_job_fires_status"),
        ),
        sa.CheckConstraint("job_version >= 1", name=op.f("ck_job_fires_job_version")),
        sa.ForeignKeyConstraint(
            ["org_id", "job_id"],
            ["jobs.org_id", "jobs.id"],
            name=op.f("fk_job_fires_org_id_job_id_jobs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id"],
            ["sessions.org_id", "sessions.id"],
            name=op.f("fk_job_fires_org_id_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "run_id"],
            ["runs.org_id", "runs.id"],
            name=op.f("fk_job_fires_org_id_run_id_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_job_fires_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_fires")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_job_fires_org_id_id")),
        sa.UniqueConstraint(
            "job_id", "scheduled_for", name=op.f("uq_job_fires_job_id_scheduled_for")
        ),
    )
    op.create_index(op.f("ix_job_fires_run_id"), "job_fires", ["run_id"])
    op.create_index(
        op.f("ix_job_fires_org_id_job_id_created_at"),
        "job_fires",
        ["org_id", "job_id", "created_at"],
    )
    op.create_index(
        "ix_job_fires_running",
        "job_fires",
        ["created_at"],
        postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "deliveries",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("fire_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('result', 'failure', 'alert')", name=op.f("ck_deliveries_kind")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed', 'unknown')",
            name=op.f("ck_deliveries_status"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_deliveries_attempts")),
        sa.CheckConstraint(
            "(status = 'sending') = (lease_expires_at IS NOT NULL)",
            name=op.f("ck_deliveries_lease"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "job_id"],
            ["jobs.org_id", "jobs.id"],
            name=op.f("fk_deliveries_org_id_job_id_jobs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "fire_id"],
            ["job_fires.org_id", "job_fires.id"],
            name=op.f("fk_deliveries_org_id_fire_id_job_fires"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_deliveries_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deliveries")),
        sa.UniqueConstraint(
            "org_id", "fire_id", "kind", name=op.f("uq_deliveries_org_id_fire_id_kind")
        ),
    )
    op.create_index(
        op.f("ix_deliveries_org_id_job_id_created_at"),
        "deliveries",
        ["org_id", "job_id", "created_at"],
    )
    op.create_index(
        "ix_deliveries_due",
        "deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_deliveries_sending",
        "deliveries",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'sending'"),
    )

    # The owner of a job reads the job, its fires, and its deliveries, while the owner is a
    # member of the org. A job is private to its owner, as a private session is. No policy
    # allows a write: the control plane writes as the table owner.
    policies = {
        "jobs": (
            "sama_private.is_org_member(org_id)"
            " AND owner_user_id IN (SELECT sama_private.my_user_ids())"
        ),
        "job_fires": (
            "EXISTS (SELECT 1 FROM public.jobs j"
            " WHERE j.org_id = job_fires.org_id AND j.id = job_fires.job_id)"
        ),
        "deliveries": (
            "EXISTS (SELECT 1 FROM public.jobs j"
            " WHERE j.org_id = deliveries.org_id AND j.id = deliveries.job_id)"
        ),
    }
    for table, rule in policies.items():
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO authenticated")
        op.execute(
            f"CREATE POLICY {table}_select ON public.{table} FOR SELECT TO authenticated"
            f" USING ({rule})"
        )

    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{NEW_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{OLD_TOOLSETS}'::jsonb"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{OLD_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{NEW_TOOLSETS}'::jsonb"
    )
    op.drop_table("deliveries")
    op.drop_table("job_fires")
    op.drop_table("jobs")
    op.drop_column("runs", "model")
