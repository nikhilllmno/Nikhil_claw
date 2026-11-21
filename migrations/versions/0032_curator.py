"""The curator (docs/architecture.md, Section 9.7).

``skills`` gets a pin, usage counters, ``last_used_at``, and the status ``stale``. The curator
marks an unused skill ``stale``, then ``archived``. It never deletes a row. ``last_used_at``
of an existing skill starts at ``updated_at``, so an old skill does not become stale on the
first run only because the counters are new.

``skill_versions`` gets the change ``restore``: a person brings back an archived skill.

``jobs`` gets the mode ``curator`` and the creator ``system``. Each workspace has at most one
curator job. A removed curator job keeps its row, so sama does not make it again.

No table is new. The RLS policies of ``skills`` and ``jobs`` do not change.

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_CHANGES = "('create', 'patch', 'edit', 'write_file', 'remove_file', 'archive')"
NEW_CHANGES = "('create', 'patch', 'edit', 'write_file', 'remove_file', 'archive', 'restore')"


def upgrade() -> None:
    op.add_column(
        "skills", sa.Column("pinned", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    for column in ("view_count", "use_count", "patch_count"):
        op.add_column("skills", sa.Column(column, sa.Integer(), server_default="0", nullable=False))
    op.add_column("skills", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("skills", sa.Column("stale_since", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE public.skills SET last_used_at = updated_at")

    op.drop_constraint(op.f("ck_skills_status"), "skills", type_="check")
    op.create_check_constraint(
        op.f("ck_skills_status"), "skills", "status IN ('active', 'stale', 'archived')"
    )
    op.create_check_constraint(
        op.f("ck_skills_stale_since"),
        "skills",
        "(status = 'stale') = (stale_since IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_skills_counts"),
        "skills",
        "view_count >= 0 AND use_count >= 0 AND patch_count >= 0",
    )

    op.drop_constraint(op.f("ck_skill_versions_change"), "skill_versions", type_="check")
    op.create_check_constraint(
        op.f("ck_skill_versions_change"), "skill_versions", f"change IN {NEW_CHANGES}"
    )

    op.drop_constraint(op.f("ck_jobs_mode"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_mode"), "jobs", "mode IN ('agent', 'script', 'curator')"
    )
    op.drop_constraint(op.f("ck_jobs_created_by_kind"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_created_by_kind"), "jobs", "created_by_kind IN ('user', 'agent', 'system')"
    )
    op.create_index(
        "uq_jobs_curator",
        "jobs",
        ["org_id", "workspace_id"],
        unique=True,
        postgresql_where=sa.text("mode = 'curator'"),
    )


def downgrade() -> None:
    # The curator rows have no place in the old schema. Their history stays in the audit log.
    op.execute(
        "DELETE FROM public.job_fires f USING public.jobs j"
        " WHERE f.job_id = j.id AND j.mode = 'curator'"
    )
    op.execute("DELETE FROM public.jobs WHERE mode = 'curator'")
    op.drop_index("uq_jobs_curator", table_name="jobs")
    op.drop_constraint(op.f("ck_jobs_created_by_kind"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_created_by_kind"), "jobs", "created_by_kind IN ('user', 'agent')"
    )
    op.drop_constraint(op.f("ck_jobs_mode"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_mode"), "jobs", "mode IN ('agent', 'script')")

    op.execute("UPDATE public.skill_versions SET change = 'create' WHERE change = 'restore'")
    op.drop_constraint(op.f("ck_skill_versions_change"), "skill_versions", type_="check")
    op.create_check_constraint(
        op.f("ck_skill_versions_change"), "skill_versions", f"change IN {OLD_CHANGES}"
    )

    op.execute(
        "UPDATE public.skills SET status = 'active', stale_since = NULL WHERE status = 'stale'"
    )
    op.drop_constraint(op.f("ck_skills_counts"), "skills", type_="check")
    op.drop_constraint(op.f("ck_skills_stale_since"), "skills", type_="check")
    op.drop_constraint(op.f("ck_skills_status"), "skills", type_="check")
    op.create_check_constraint(
        op.f("ck_skills_status"), "skills", "status IN ('active', 'archived')"
    )
    for column in (
        "stale_since",
        "last_used_at",
        "patch_count",
        "use_count",
        "view_count",
        "pinned",
    ):
        op.drop_column("skills", column)
