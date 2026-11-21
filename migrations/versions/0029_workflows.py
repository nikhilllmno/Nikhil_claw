"""Durable workflows (docs/architecture.md, Section 12.3).

``workflows`` has one row for each workflow. ``workflow_versions`` has the YAML text, the
checked definition, its hash, and the owner approval of each version. ``workflow_instances``
has one row for each run of a workflow: the unique key ``(workflow_id, scheduled_for)`` makes a
schedule fire start once. ``workflow_steps`` has one row for each step of an instance, unique on
``(instance_id, step_id)``. ``workflow_receipts`` has one row for each external call of a tool
step: sama writes ``intent`` before the call and the result after it. The default profile gets
the toolset ``workflows``. A trigger can have the target ``workflow`` (``triggers.workflow_id``).
The event of such a trigger points to a workflow instance, not to a session and a run.

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The default list after the code and media migration (0027) and the same list with
# ``workflows``. An exact match keeps the profiles that a person changed.
OLD_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search", "subagents", "connections", "integrations", "delegation", "tasks", "cron",'
    ' "code_exec"]'
)
NEW_TOOLSETS = (
    '["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions", "media",'
    ' "tool_search", "subagents", "connections", "integrations", "delegation", "tasks", "cron",'
    ' "code_exec", "workflows"]'
)


def _now() -> sa.TextClause:
    return sa.text("now()")


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def _id() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _org(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["org_id"], ["orgs.id"], name=op.f(f"fk_{table}_org_id_orgs"), ondelete="CASCADE"
    )


def upgrade() -> None:
    op.create_table(
        "workflows",
        _id(),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("agent_profile_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("origin_session_id", sa.UUID(), nullable=True),
        sa.Column("created_by_kind", sa.Text(), server_default="user", nullable=False),
        sa.Column("created_by_run_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'pending_approval', 'removed')",
            name=op.f("ck_workflows_status"),
        ),
        sa.CheckConstraint(
            "created_by_kind IN ('user', 'agent')", name=op.f("ck_workflows_created_by_kind")
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 100", name=op.f("ck_workflows_name_length")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_workflows_version")),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_workflows_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "owner_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_workflows_org_id_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            name=op.f("fk_workflows_org_id_agent_profile_id_agent_profiles"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "origin_session_id"],
            ["sessions.org_id", "sessions.id"],
            name=op.f("fk_workflows_org_id_origin_session_id_sessions"),
        ),
        _org("workflows"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflows")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_workflows_org_id_id")),
    )
    op.create_index(
        op.f("ix_workflows_org_id_owner_user_id"), "workflows", ["org_id", "owner_user_id"]
    )
    op.create_index(
        "ix_workflows_owner_name",
        "workflows",
        ["org_id", "owner_user_id", "name"],
        unique=True,
        postgresql_where=sa.text("status <> 'removed'"),
    )
    op.create_index(
        "ix_workflows_due",
        "workflows",
        ["next_fire_at"],
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "workflow_versions",
        _id(),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workflow_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("definition", _jsonb(), nullable=False),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.Column("needs_approval", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("approved_hash", sa.Text(), nullable=True),
        sa.Column("approved_by_user_id", sa.UUID(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_kind", sa.Text(), server_default="user", nullable=False),
        sa.Column("created_by_run_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint("version >= 1", name=op.f("ck_workflow_versions_version")),
        sa.CheckConstraint(
            "jsonb_typeof(definition) = 'object'",
            name=op.f("ck_workflow_versions_definition_is_object"),
        ),
        sa.CheckConstraint(
            "created_by_kind IN ('user', 'agent')",
            name=op.f("ck_workflow_versions_created_by_kind"),
        ),
        sa.CheckConstraint(
            "(approved_hash IS NULL) = (approved_at IS NULL)",
            name=op.f("ck_workflow_versions_approved_has_time"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workflow_id"],
            ["workflows.org_id", "workflows.id"],
            name=op.f("fk_workflow_versions_org_id_workflow_id_workflows"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "approved_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_workflow_versions_org_id_approved_by_user_id_users"),
        ),
        _org("workflow_versions"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_versions")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_workflow_versions_org_id_id")),
        sa.UniqueConstraint(
            "org_id",
            "workflow_id",
            "version",
            name=op.f("uq_workflow_versions_org_id_workflow_id_version"),
        ),
    )

    op.create_table(
        "workflow_instances",
        _id(),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workflow_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("owner_user_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("agent_profile_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("inputs", _jsonb(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("output", _jsonb(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("parent_instance_id", sa.UUID(), nullable=True),
        sa.Column("parent_step_id", sa.Text(), nullable=True),
        sa.Column("depth", sa.Integer(), server_default="0", nullable=False),
        sa.Column("trigger", sa.Text(), server_default="manual", nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_by_kind", sa.Text(), server_default="user", nullable=False),
        sa.Column("started_by_run_id", sa.UUID(), nullable=True),
        sa.Column("toolsets", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'waiting', 'completed', 'failed', 'cancelled')",
            name=op.f("ck_workflow_instances_status"),
        ),
        sa.CheckConstraint(
            "trigger IN ('manual', 'schedule', 'trigger', 'workflow')",
            name=op.f("ck_workflow_instances_trigger"),
        ),
        sa.CheckConstraint(
            "started_by_kind IN ('user', 'agent', 'system')",
            name=op.f("ck_workflow_instances_started_by_kind"),
        ),
        sa.CheckConstraint("depth BETWEEN 0 AND 3", name=op.f("ck_workflow_instances_depth")),
        sa.CheckConstraint(
            "jsonb_typeof(inputs) = 'object'", name=op.f("ck_workflow_instances_inputs_is_object")
        ),
        sa.CheckConstraint(
            "toolsets IS NULL OR jsonb_typeof(toolsets) = 'array'",
            name=op.f("ck_workflow_instances_toolsets_array"),
        ),
        sa.CheckConstraint(
            "(parent_instance_id IS NULL) = (parent_step_id IS NULL)",
            name=op.f("ck_workflow_instances_parent_has_step"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workflow_id"],
            ["workflows.org_id", "workflows.id"],
            name=op.f("fk_workflow_instances_org_id_workflow_id_workflows"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workflow_id", "version"],
            [
                "workflow_versions.org_id",
                "workflow_versions.workflow_id",
                "workflow_versions.version",
            ],
            name=op.f("fk_workflow_instances_version"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "owner_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_workflow_instances_org_id_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name=op.f("fk_workflow_instances_org_id_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "agent_profile_id"],
            ["agent_profiles.org_id", "agent_profiles.id"],
            name=op.f("fk_workflow_instances_org_id_agent_profile_id_agent_profiles"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "parent_instance_id"],
            ["workflow_instances.org_id", "workflow_instances.id"],
            name=op.f("fk_workflow_instances_parent"),
            ondelete="CASCADE",
        ),
        _org("workflow_instances"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_instances")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_workflow_instances_org_id_id")),
        sa.UniqueConstraint(
            "workflow_id",
            "scheduled_for",
            name=op.f("uq_workflow_instances_workflow_id_scheduled_for"),
        ),
    )
    op.create_index(
        op.f("ix_workflow_instances_org_id_workflow_id_created_at"),
        "workflow_instances",
        ["org_id", "workflow_id", "created_at"],
    )
    op.create_index(
        op.f("ix_workflow_instances_org_id_owner_user_id_created_at"),
        "workflow_instances",
        ["org_id", "owner_user_id", "created_at"],
    )
    op.create_index(
        op.f("ix_workflow_instances_parent_instance_id"),
        "workflow_instances",
        ["parent_instance_id"],
    )
    op.create_index(
        "ix_workflow_instances_open",
        "workflow_instances",
        ["checked_at"],
        postgresql_where=sa.text("status IN ('running', 'waiting')"),
    )
    # The message rate counts the instances that triggers start (Section 12.2).
    op.create_index(
        "ix_workflow_instances_trigger_starts",
        "workflow_instances",
        ["org_id", "created_at"],
        postgresql_where=sa.text("trigger = 'trigger'"),
    )

    op.create_table(
        "workflow_steps",
        _id(),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("instance_id", sa.UUID(), nullable=False),
        sa.Column("step_id", sa.Text(), nullable=False),
        sa.Column("parent_step_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("input", _jsonb(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("operation_id", sa.Text(), nullable=True),
        sa.Column("output", _jsonb(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("child_instance_id", sa.UUID(), nullable=True),
        sa.Column("wake_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event", sa.Text(), nullable=True),
        sa.Column("resolved_by_user_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('tool', 'agent', 'approval', 'branch', 'parallel', 'wait', 'workflow')",
            name=op.f("ck_workflow_steps_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'waiting', 'needs_reconcile', 'waiting_human',"
            " 'completed', 'failed', 'skipped', 'cancelled')",
            name=op.f("ck_workflow_steps_status"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_workflow_steps_attempts")),
        sa.CheckConstraint(
            "char_length(step_id) BETWEEN 1 AND 64", name=op.f("ck_workflow_steps_step_id_length")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "instance_id"],
            ["workflow_instances.org_id", "workflow_instances.id"],
            name=op.f("fk_workflow_steps_org_id_instance_id_workflow_instances"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id"],
            ["sessions.org_id", "sessions.id"],
            name=op.f("fk_workflow_steps_org_id_session_id_sessions"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "run_id"],
            ["runs.org_id", "runs.id"],
            name=op.f("fk_workflow_steps_org_id_run_id_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "child_instance_id"],
            ["workflow_instances.org_id", "workflow_instances.id"],
            name=op.f("fk_workflow_steps_org_id_child_instance_id_workflow_instances"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "resolved_by_user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_workflow_steps_org_id_resolved_by_user_id_users"),
        ),
        _org("workflow_steps"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_steps")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_workflow_steps_org_id_id")),
        sa.UniqueConstraint(
            "instance_id", "step_id", name=op.f("uq_workflow_steps_instance_id_step_id")
        ),
    )
    op.create_index(op.f("ix_workflow_steps_run_id"), "workflow_steps", ["run_id"])
    op.create_index(
        op.f("ix_workflow_steps_child_instance_id"), "workflow_steps", ["child_instance_id"]
    )
    op.create_index(
        "ix_workflow_steps_due",
        "workflow_steps",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending' AND kind = 'tool'"),
    )
    op.create_index(
        "ix_workflow_steps_leased",
        "workflow_steps",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'running' AND kind = 'tool'"),
    )
    op.create_index(
        "ix_workflow_steps_reconcile",
        "workflow_steps",
        ["created_at"],
        postgresql_where=sa.text("status = 'needs_reconcile'"),
    )

    op.create_table(
        "workflow_receipts",
        _id(),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("instance_id", sa.UUID(), nullable=False),
        sa.Column("step_row_id", sa.UUID(), nullable=False),
        sa.Column("step_id", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="intent", nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('intent', 'done', 'failed', 'unknown')",
            name=op.f("ck_workflow_receipts_status"),
        ),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_workflow_receipts_attempt")),
        sa.ForeignKeyConstraint(
            ["org_id", "instance_id"],
            ["workflow_instances.org_id", "workflow_instances.id"],
            name=op.f("fk_workflow_receipts_org_id_instance_id_workflow_instances"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "step_row_id"],
            ["workflow_steps.org_id", "workflow_steps.id"],
            name=op.f("fk_workflow_receipts_org_id_step_row_id_workflow_steps"),
            ondelete="CASCADE",
        ),
        _org("workflow_receipts"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_receipts")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_workflow_receipts_org_id_id")),
        sa.UniqueConstraint(
            "step_row_id", "attempt", name=op.f("uq_workflow_receipts_step_row_id_attempt")
        ),
    )
    op.create_index(
        op.f("ix_workflow_receipts_org_id_instance_id_created_at"),
        "workflow_receipts",
        ["org_id", "instance_id", "created_at"],
    )

    # The owner of a workflow reads the workflow, its versions, its instances, their steps,
    # and their receipts, while the owner is a member of the org. No policy allows a write:
    # the control plane writes as the table owner.
    owner = (
        "sama_private.is_org_member(org_id)"
        " AND owner_user_id IN (SELECT sama_private.my_user_ids())"
    )
    policies = {
        "workflows": owner,
        "workflow_versions": (
            "EXISTS (SELECT 1 FROM public.workflows w"
            " WHERE w.org_id = workflow_versions.org_id AND w.id = workflow_versions.workflow_id)"
        ),
        "workflow_instances": owner,
        "workflow_steps": (
            "EXISTS (SELECT 1 FROM public.workflow_instances i"
            " WHERE i.org_id = workflow_steps.org_id AND i.id = workflow_steps.instance_id)"
        ),
        "workflow_receipts": (
            "EXISTS (SELECT 1 FROM public.workflow_instances i"
            " WHERE i.org_id = workflow_receipts.org_id AND i.id = workflow_receipts.instance_id)"
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

    _triggers_upgrade()
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{NEW_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{OLD_TOOLSETS}'::jsonb"
    )


OLD_EVENT_POLICY = "sama_private.can_read_session(org_id, session_id)"
NEW_EVENT_POLICY = (
    "(session_id IS NOT NULL AND sama_private.can_read_session(org_id, session_id))"
    " OR (workflow_instance_id IS NOT NULL AND EXISTS (SELECT 1 FROM public.workflow_instances i"
    " WHERE i.org_id = trigger_events.org_id AND i.id = trigger_events.workflow_instance_id))"
)


def _event_policy(rule: str) -> None:
    op.execute("DROP POLICY trigger_events_select ON public.trigger_events")
    op.execute(
        "CREATE POLICY trigger_events_select ON public.trigger_events FOR SELECT TO authenticated"
        f" USING ({rule})"
    )


def _triggers_upgrade() -> None:
    """A trigger can start a workflow (Section 12.2). Its events then point to the instance,
    not to a session and a run."""
    op.add_column("triggers", sa.Column("workflow_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_triggers_workflow",
        "triggers",
        "workflows",
        ["org_id", "workflow_id"],
        ["org_id", "id"],
        ondelete="CASCADE",
    )
    op.create_index(op.f("ix_triggers_workflow_id"), "triggers", ["workflow_id"])
    op.drop_constraint(op.f("ck_triggers_target"), "triggers", type_="check")
    op.create_check_constraint(
        op.f("ck_triggers_target"), "triggers", "target IN ('run', 'workflow')"
    )
    op.create_check_constraint(
        op.f("ck_triggers_workflow_target"),
        "triggers",
        "(target = 'workflow') = (workflow_id IS NOT NULL)",
    )
    op.add_column("trigger_events", sa.Column("workflow_instance_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_trigger_events_workflow_instance",
        "trigger_events",
        "workflow_instances",
        ["org_id", "workflow_instance_id"],
        ["org_id", "id"],
        ondelete="CASCADE",
    )
    op.create_index(
        op.f("ix_trigger_events_workflow_instance_id"), "trigger_events", ["workflow_instance_id"]
    )
    op.alter_column("trigger_events", "session_id", nullable=True)
    op.alter_column("trigger_events", "run_id", nullable=True)
    op.create_check_constraint(
        op.f("ck_trigger_events_one_target"),
        "trigger_events",
        "(run_id IS NULL) = (session_id IS NULL)"
        " AND (run_id IS NULL) <> (workflow_instance_id IS NULL)",
    )
    _event_policy(NEW_EVENT_POLICY)


def _triggers_downgrade() -> None:
    """Deletes the workflow triggers and their events, which the old schema cannot hold."""
    op.execute("DELETE FROM public.trigger_events WHERE workflow_instance_id IS NOT NULL")
    op.execute("DELETE FROM public.triggers WHERE target = 'workflow'")
    _event_policy(OLD_EVENT_POLICY)
    op.drop_constraint(op.f("ck_trigger_events_one_target"), "trigger_events", type_="check")
    op.alter_column("trigger_events", "run_id", nullable=False)
    op.alter_column("trigger_events", "session_id", nullable=False)
    op.drop_index(op.f("ix_trigger_events_workflow_instance_id"), table_name="trigger_events")
    op.drop_constraint("fk_trigger_events_workflow_instance", "trigger_events", type_="foreignkey")
    op.drop_column("trigger_events", "workflow_instance_id")
    op.drop_constraint(op.f("ck_triggers_workflow_target"), "triggers", type_="check")
    op.drop_constraint(op.f("ck_triggers_target"), "triggers", type_="check")
    op.create_check_constraint(op.f("ck_triggers_target"), "triggers", "target IN ('run')")
    op.drop_index(op.f("ix_triggers_workflow_id"), table_name="triggers")
    op.drop_constraint("fk_triggers_workflow", "triggers", type_="foreignkey")
    op.drop_column("triggers", "workflow_id")


def downgrade() -> None:
    op.execute(
        f"UPDATE public.agent_profiles SET toolsets = '{OLD_TOOLSETS}'::jsonb"  # noqa: S608
        f" WHERE toolsets = '{NEW_TOOLSETS}'::jsonb"
    )
    _triggers_downgrade()
    op.drop_table("workflow_receipts")
    op.drop_table("workflow_steps")
    op.drop_table("workflow_instances")
    op.drop_table("workflow_versions")
    op.drop_table("workflows")
