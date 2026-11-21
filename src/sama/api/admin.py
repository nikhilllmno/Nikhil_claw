"""The admin views of the web app (docs/architecture.md, Section 17.5, M6.9): the usage, the
audit log, and the curator reports of an org.

The API checks that the caller is an org owner or admin before it calls these methods. The
reads then run as the table owner and filter on ``org_id`` in each query, as the data controls
do (Section 17.4): an admin sees the usage and the audit rows of the whole org, also of the
private sessions of other members. No row has the text of a message. Every text that leaves
here goes through the redactor again.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from pydantic import BaseModel
from sqlalchemy import Date, and_, cast, func, literal, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import (
    AgentProfile,
    AuditEntry,
    Job,
    JobFire,
    ModelCall,
    Session,
    User,
    Workspace,
)
from sama.vault.redact import default_redactor

MAX_USAGE_DAYS: Final = 90
DEFAULT_USAGE_DAYS: Final = 30
USAGE_TOP: Final = 50
"""Each breakdown of the usage has at most this many rows, the most expensive first."""
MAX_AUDIT_LIMIT: Final = 200
DEFAULT_AUDIT_LIMIT: Final = 50
MAX_AUDIT_FILTER_CHARS: Final = 64
MAX_AUDIT_ID: Final = 2**63 - 1
"""The largest ``audit_log.id`` (``bigint``). A larger cursor is not valid (``422``)."""
MAX_CURATOR_REPORTS: Final = 50
DEFAULT_CURATOR_REPORTS: Final = 10
MAX_REPORT_CHARS: Final = 50_000
"""A report text is cut after this many characters. A curator report is far shorter."""


class UsageTotals(BaseModel):
    cost_usd: Decimal
    org_key_cost_usd: Decimal
    """The part of ``cost_usd`` on the own model keys of the org (Section 17.3)."""
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    model_calls: int
    runs: int
    sessions: int


class UsageDay(BaseModel):
    day: date
    cost_usd: Decimal
    input_tokens: int
    output_tokens: int
    model_calls: int


class UsageRow(BaseModel):
    """One row of a breakdown. ``key`` is the id (a user or a profile) or the name (a model or
    a key source). ``label`` is the name to show."""

    key: str | None
    label: str
    cost_usd: Decimal
    input_tokens: int
    output_tokens: int
    model_calls: int


class OrgUsage(BaseModel):
    days: int
    since: datetime
    until: datetime
    totals: UsageTotals
    by_day: list[UsageDay]
    """One entry for each UTC day of the window, also for a day without calls."""
    by_user: list[UsageRow]
    by_profile: list[UsageRow]
    by_model: list[UsageRow]
    by_key_source: list[UsageRow]


class AuditRow(BaseModel):
    id: int
    created_at: datetime
    action: str
    actor_kind: str
    actor_user_id: uuid.UUID | None
    actor_name: str | None
    workspace_id: uuid.UUID | None
    target_type: str | None
    target_id: str | None
    run_id: uuid.UUID | None
    details: dict[str, Any]


class AuditPage(BaseModel):
    entries: list[AuditRow]
    next_before: int | None
    """Give this as ``before`` for the next (older) page. ``None`` on the last page."""


class CuratorJob(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    workspace_name: str
    status: str
    next_fire_at: datetime | None
    last_fire_at: datetime | None


class CuratorReport(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    workspace_id: uuid.UUID
    workspace_name: str
    status: str
    manual: bool
    scheduled_for: datetime
    finished_at: datetime | None
    output: str | None
    error: str | None
    truncated: bool


class CuratorReports(BaseModel):
    jobs: list[CuratorJob]
    reports: list[CuratorReport]


def usage_window(days: int, now: datetime) -> tuple[datetime, datetime]:
    """The start of the first UTC day of the window and ``now``. The window has ``days`` UTC
    days, today included."""
    today = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=days - 1), now


def _cut(text: str | None) -> tuple[str | None, bool]:
    if text is None:
        return None, False
    clean = default_redactor().text(text)
    if len(clean) <= MAX_REPORT_CHARS:
        return clean, False
    # The cut comes after the redaction and ends at a space, so it never shows the start of a
    # secret that the redactor masks as a whole.
    head = clean[:MAX_REPORT_CHARS]
    space = head.rfind(" ")
    return (head[:space] if space > 0 else head), True


class OrgViews:
    """The reads of the admin pages. The caller checks the role first."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def usage(self, org_id: uuid.UUID, days: int, *, now: datetime | None = None) -> OrgUsage:
        since, until = usage_window(days, now or datetime.now(UTC))
        window = and_(
            ModelCall.org_id == org_id,
            ModelCall.created_at >= since,
            ModelCall.created_at <= until,
        )
        cost = func.coalesce(func.sum(ModelCall.cost_usd), 0)
        tokens_in = func.coalesce(func.sum(ModelCall.input_tokens), 0)
        tokens_out = func.coalesce(func.sum(ModelCall.output_tokens), 0)
        calls = func.count(ModelCall.id)
        sums = (
            cost.label("cost_usd"),
            tokens_in.label("input_tokens"),
            tokens_out.label("output_tokens"),
            calls.label("model_calls"),
        )
        async with self._engine.connect() as plain:
            # One snapshot for all the parts, so a call that commits in between does not make
            # the totals and the breakdowns disagree.
            connection = await plain.execution_options(isolation_level="REPEATABLE READ")
            await connection.begin()
            totals = (
                await connection.execute(
                    select(
                        *sums,
                        func.coalesce(
                            func.sum(ModelCall.cost_usd).filter(ModelCall.key_source == "org"), 0
                        ).label("org_key_cost_usd"),
                        func.coalesce(func.sum(ModelCall.cache_read_tokens), 0).label(
                            "cache_read_tokens"
                        ),
                        func.count(func.distinct(ModelCall.run_id)).label("runs"),
                        func.count(func.distinct(ModelCall.session_id)).label("sessions"),
                    ).where(window)
                )
            ).one()
            day = cast(func.timezone("UTC", ModelCall.created_at), Date).label("day")
            daily = {
                row.day: row
                for row in await connection.execute(select(day, *sums).where(window).group_by(day))
            }
            by_user = await connection.execute(
                select(ModelCall.user_id.label("key"), User.display_name.label("label"), *sums)
                .select_from(ModelCall)
                .outerjoin(
                    User, and_(User.org_id == ModelCall.org_id, User.id == ModelCall.user_id)
                )
                .where(window)
                .group_by(ModelCall.user_id, User.display_name)
                .order_by(cost.desc(), ModelCall.user_id)
                .limit(USAGE_TOP)
            )
            by_user_rows = [
                _row(r.key, r.label or ("Jobs and system" if r.key is None else "Removed user"), r)
                for r in by_user
            ]
            profile_id = Session.agent_profile_id
            by_profile = await connection.execute(
                select(profile_id.label("key"), AgentProfile.name.label("label"), *sums)
                .select_from(ModelCall)
                .join(
                    Session,
                    and_(Session.org_id == ModelCall.org_id, Session.id == ModelCall.session_id),
                )
                .outerjoin(
                    AgentProfile,
                    and_(AgentProfile.org_id == Session.org_id, AgentProfile.id == profile_id),
                )
                .where(window)
                .group_by(profile_id, AgentProfile.name)
                .order_by(cost.desc(), profile_id)
                .limit(USAGE_TOP)
            )
            by_profile_rows = [_row(r.key, r.label or "Removed agent", r) for r in by_profile]
            model = (ModelCall.provider + literal("/") + ModelCall.model).label("key")
            by_model = await connection.execute(
                select(model, *sums)
                .where(window)
                .group_by(ModelCall.provider, ModelCall.model)
                .order_by(cost.desc(), model)
                .limit(USAGE_TOP)
            )
            by_model_rows = [_row(r.key, r.key, r) for r in by_model]
            by_source = await connection.execute(
                select(ModelCall.key_source.label("key"), *sums)
                .where(window)
                .group_by(ModelCall.key_source)
                .order_by(ModelCall.key_source)
            )
            by_source_rows = [
                _row(r.key, "Org key" if r.key == "org" else "Sama key", r) for r in by_source
            ]
        first = since.date()
        by_day = []
        for offset in range(days):
            current = first + timedelta(days=offset)
            found = daily.get(current)
            by_day.append(
                UsageDay(
                    day=current,
                    cost_usd=Decimal(found.cost_usd) if found else Decimal(0),
                    input_tokens=int(found.input_tokens) if found else 0,
                    output_tokens=int(found.output_tokens) if found else 0,
                    model_calls=int(found.model_calls) if found else 0,
                )
            )
        return OrgUsage(
            days=days,
            since=since,
            until=until,
            totals=UsageTotals(
                cost_usd=Decimal(totals.cost_usd),
                org_key_cost_usd=Decimal(totals.org_key_cost_usd),
                input_tokens=int(totals.input_tokens),
                output_tokens=int(totals.output_tokens),
                cache_read_tokens=int(totals.cache_read_tokens),
                model_calls=int(totals.model_calls),
                runs=int(totals.runs),
                sessions=int(totals.sessions),
            ),
            by_day=by_day,
            by_user=by_user_rows,
            by_profile=by_profile_rows,
            by_model=by_model_rows,
            by_key_source=by_source_rows,
        )

    async def audit(
        self,
        org_id: uuid.UUID,
        *,
        limit: int,
        before: int | None = None,
        action: str | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> AuditPage:
        """The audit rows of the org, newest first. ``action`` keeps the rows whose action
        starts with the text (for example ``member.``)."""
        query = (
            select(
                AuditEntry.id,
                AuditEntry.created_at,
                AuditEntry.action,
                AuditEntry.actor_kind,
                AuditEntry.actor_user_id,
                User.display_name.label("actor_name"),
                AuditEntry.workspace_id,
                AuditEntry.target_type,
                AuditEntry.target_id,
                AuditEntry.run_id,
                AuditEntry.details,
            )
            .select_from(AuditEntry)
            .outerjoin(
                User, and_(User.org_id == AuditEntry.org_id, User.id == AuditEntry.actor_user_id)
            )
            .where(AuditEntry.org_id == org_id)
            .order_by(AuditEntry.id.desc())
            .limit(limit + 1)
        )
        if before is not None:
            query = query.where(AuditEntry.id < before)
        if action:
            # A prefix match with the wildcards of LIKE escaped.
            escaped = action.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            query = query.where(AuditEntry.action.like(f"{escaped}%", escape="\\"))
        if actor_user_id is not None:
            query = query.where(AuditEntry.actor_user_id == actor_user_id)
        async with self._engine.connect() as connection:
            rows = list(await connection.execute(query))
        more = len(rows) > limit
        rows = rows[:limit]
        redactor = default_redactor()
        entries = [
            AuditRow(
                id=row.id,
                created_at=row.created_at,
                action=row.action,
                actor_kind=row.actor_kind,
                actor_user_id=row.actor_user_id,
                actor_name=row.actor_name,
                workspace_id=row.workspace_id,
                target_type=row.target_type,
                target_id=row.target_id,
                run_id=row.run_id,
                details=redactor.json(dict(row.details or {})),
            )
            for row in rows
        ]
        return AuditPage(entries=entries, next_before=entries[-1].id if more else None)

    async def curator_reports(self, org_id: uuid.UUID, *, limit: int) -> CuratorReports:
        """The curator jobs of the org (Section 9.7) and the newest ended fires with their
        reports."""
        async with self._engine.connect() as connection:
            jobs = list(
                await connection.execute(
                    select(
                        Job.id,
                        Job.workspace_id,
                        Workspace.name.label("workspace_name"),
                        Job.status,
                        Job.next_fire_at,
                        Job.last_fire_at,
                    )
                    .join(
                        Workspace,
                        and_(Workspace.org_id == Job.org_id, Workspace.id == Job.workspace_id),
                    )
                    .where(Job.org_id == org_id, Job.mode == "curator")
                    .order_by(Workspace.name, Job.id)
                )
            )
            fires = list(
                await connection.execute(
                    select(
                        JobFire.id,
                        JobFire.job_id,
                        Job.workspace_id,
                        Workspace.name.label("workspace_name"),
                        JobFire.status,
                        JobFire.manual,
                        JobFire.scheduled_for,
                        JobFire.finished_at,
                        JobFire.output,
                        JobFire.error,
                    )
                    .join(Job, and_(Job.org_id == JobFire.org_id, Job.id == JobFire.job_id))
                    .join(
                        Workspace,
                        and_(Workspace.org_id == Job.org_id, Workspace.id == Job.workspace_id),
                    )
                    .where(
                        JobFire.org_id == org_id,
                        Job.mode == "curator",
                        JobFire.status.in_(("completed", "failed")),
                    )
                    .order_by(JobFire.created_at.desc(), JobFire.id)
                    .limit(limit)
                )
            )
        reports = []
        for fire in fires:
            output, cut_output = _cut(fire.output)
            error, cut_error = _cut(fire.error)
            reports.append(
                CuratorReport(
                    id=fire.id,
                    job_id=fire.job_id,
                    workspace_id=fire.workspace_id,
                    workspace_name=fire.workspace_name,
                    status=fire.status,
                    manual=fire.manual,
                    scheduled_for=fire.scheduled_for,
                    finished_at=fire.finished_at,
                    output=output,
                    error=error,
                    truncated=cut_output or cut_error,
                )
            )
        return CuratorReports(
            jobs=[CuratorJob.model_validate(job, from_attributes=True) for job in jobs],
            reports=reports,
        )


def _row(key: Any, label: str, row: Any) -> UsageRow:
    return UsageRow(
        key=None if key is None else str(key),
        label=label,
        cost_usd=Decimal(row.cost_usd),
        input_tokens=int(row.input_tokens),
        output_tokens=int(row.output_tokens),
        model_calls=int(row.model_calls),
    )
