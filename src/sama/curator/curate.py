"""One run of the curator for one workspace (docs/architecture.md, Section 9.7).

``curate`` runs in the transaction of the job fire, as the table owner. It changes the status
of agent-written skills and returns the report. It never deletes a row. An archive is a new
version of the skill with the change ``archive`` and the author ``system``, so the history
shows it and a person can restore the skill.

The curator job of a workspace covers the workspace skills and the agent and workspace memory
of that workspace. The curator job of the first workspace of the org (the oldest) also covers
the org skills, the user skills, and the user memory. The report names no user skill and shows
no user memory: they are private to their users.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from sqlalchemy import ColumnElement, and_, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.orm import aliased

from sama.curator.policy import (
    ARCHIVE_AFTER,
    MAX_MEMORY_REVISIONS,
    MEMORY_FULL_RATIO,
    MEMORY_OLD_AFTER,
    STALE_AFTER,
    SkillState,
    decide,
    entry_ages,
    names_text,
    percent,
    skill_names,
)
from sama.db.models import (
    AgentProfile,
    AuditEntry,
    Job,
    Memory,
    MemoryRevision,
    Skill,
    SkillVersion,
    Workflow,
    WorkflowVersion,
    Workspace,
)
from sama.memory.core import CAPS, Target, used
from sama.vault.redact import redact

MAX_SKILLS: Final = 2000
"""The most skills that one run reads. The rest wait for the next run."""
TOP_SKILLS: Final = 5
PREVIEW_CHARS: Final = 80
MAX_PREVIEWS: Final = 5


@dataclass(slots=True)
class SkillOutcome:
    stale: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    revived: list[str] = field(default_factory=list)
    user_stale: int = 0
    user_archived: int = 0
    checked: int = 0
    active: int = 0
    stale_total: int = 0
    archived_total: int = 0
    pinned: int = 0
    exempt: int = 0
    top: list[tuple[str, str, int, int, int]] = field(default_factory=list)
    """``(name, scope, uses, views, patches)`` of the most used shared skills."""


@dataclass(slots=True)
class MemoryOutcome:
    lines: list[str] = field(default_factory=list)


async def _now(connection: AsyncConnection) -> datetime:
    value: datetime = (await connection.execute(select(func.now()))).scalar_one()
    return value


async def is_first_workspace(
    connection: AsyncConnection, org_id: uuid.UUID, workspace_id: uuid.UUID
) -> bool:
    """True for the oldest workspace of the org. Its curator job also covers the org skills,
    the user skills, and the user memory."""
    first = (
        await connection.execute(
            select(Workspace.id)
            .where(Workspace.org_id == org_id)
            .order_by(Workspace.created_at, Workspace.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    return first == workspace_id


def _coverage(org_id: uuid.UUID, workspace_id: uuid.UUID, first: bool) -> ColumnElement[bool]:
    options = [and_(Skill.scope == "workspace", Skill.workspace_id == workspace_id)]
    if first:
        options += [Skill.scope == "org", Skill.scope == "user"]
    return and_(Skill.org_id == org_id, or_(*options))


async def exempt_names(connection: AsyncConnection, org_id: uuid.UUID) -> set[str]:
    """The skill names that a job or a workflow of the org names. The curator keeps them."""
    names: set[str] = set()
    for (skills,) in await connection.execute(
        select(Job.skills).where(Job.org_id == org_id, Job.status != "removed")
    ):
        names |= skill_names({"skills": skills})
    for (definition,) in await connection.execute(
        select(WorkflowVersion.definition)
        .join(
            Workflow,
            and_(
                Workflow.org_id == WorkflowVersion.org_id,
                Workflow.id == WorkflowVersion.workflow_id,
                Workflow.version == WorkflowVersion.version,
            ),
        )
        .where(WorkflowVersion.org_id == org_id, Workflow.status != "removed")
    ):
        names |= skill_names(definition)
    return names


async def _audit(
    connection: AsyncConnection, job: Any, skill: SkillState, action: str, **details: Any
) -> None:
    await connection.execute(
        insert(AuditEntry).values(
            org_id=job.org_id,
            workspace_id=job.workspace_id,
            actor_kind="system",
            actor_user_id=None,
            action=action,
            target_type="skill",
            target_id=str(skill.skill_id),
            details={"name": skill.name, "scope": skill.scope, "job_id": str(job.id), **details},
        )
    )


async def _mark_stale(
    connection: AsyncConnection, job: Any, skill: SkillState, now: datetime
) -> bool:
    done = (
        await connection.execute(
            update(Skill)
            .where(Skill.org_id == job.org_id, Skill.id == skill.skill_id, Skill.status == "active")
            .values(status="stale", stale_since=now)
            .returning(Skill.id)
        )
    ).one_or_none()
    if done is not None:
        await _audit(connection, job, skill, "skill.stale")
    return done is not None


async def _revive(connection: AsyncConnection, job: Any, skill: SkillState) -> bool:
    done = (
        await connection.execute(
            update(Skill)
            .where(Skill.org_id == job.org_id, Skill.id == skill.skill_id, Skill.status == "stale")
            .values(status="active", stale_since=None)
            .returning(Skill.id)
        )
    ).one_or_none()
    if done is not None:
        await _audit(connection, job, skill, "skill.active")
    return done is not None


async def _archive(connection: AsyncConnection, job: Any, skill: SkillState, now: datetime) -> bool:
    """A new version with the same text, the change ``archive``, and the author ``system``.
    The row lock waits for a write of the skill that runs now, and the status check skips a
    skill that the write made active again."""
    row = (
        await connection.execute(
            select(Skill.current_version)
            .where(Skill.org_id == job.org_id, Skill.id == skill.skill_id, Skill.status == "stale")
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        return False
    current = (
        await connection.execute(
            select(SkillVersion).where(
                SkillVersion.org_id == job.org_id,
                SkillVersion.skill_id == skill.skill_id,
                SkillVersion.version_no == row.current_version,
            )
        )
    ).one()
    version = row.current_version + 1
    days = (now - skill.last_active).days
    await connection.execute(
        insert(SkillVersion).values(
            org_id=job.org_id,
            skill_id=skill.skill_id,
            version_no=version,
            name=current.name,
            description=current.description,
            skill_md=current.skill_md,
            files=dict(current.files),
            tags=list(current.tags),
            requires_toolsets=list(current.requires_toolsets),
            change="archive",
            summary=f"The curator archived the skill: no view, use, or change for {days} days.",
            author_kind="system",
        )
    )
    await connection.execute(
        update(Skill)
        .where(Skill.org_id == job.org_id, Skill.id == skill.skill_id)
        .values(current_version=version, status="archived", stale_since=None, updated_at=now)
    )
    await _audit(connection, job, skill, "skill.archive", version=version)
    return True


async def curate_skills(
    connection: AsyncConnection, job: Any, *, first: bool, now: datetime
) -> SkillOutcome:
    outcome = SkillOutcome()
    exempt = await exempt_names(connection, job.org_id)
    first_version = aliased(SkillVersion)
    coverage = _coverage(job.org_id, job.workspace_id, first)
    rows = (
        await connection.execute(
            select(
                Skill.id,
                Skill.scope,
                Skill.name,
                Skill.status,
                Skill.pinned,
                Skill.last_used_at,
                Skill.created_at,
                Skill.stale_since,
            )
            .join(
                first_version,
                and_(
                    first_version.org_id == Skill.org_id,
                    first_version.skill_id == Skill.id,
                    first_version.version_no == 1,
                ),
            )
            .where(coverage, Skill.status.in_(("active", "stale")))
            .where(first_version.author_kind == "agent")
            .order_by(Skill.last_used_at.asc().nulls_first(), Skill.id)
            .limit(MAX_SKILLS)
        )
    ).all()
    for row in rows:
        state = SkillState(
            skill_id=row.id,
            scope=row.scope,
            name=row.name,
            status=row.status,
            pinned=row.pinned,
            exempt=row.name in exempt,
            last_active=max(row.last_used_at or row.created_at, row.created_at),
            stale_since=row.stale_since,
        )
        outcome.checked += 1
        outcome.pinned += state.pinned
        outcome.exempt += state.exempt and not state.pinned
        decision = decide(state, now)
        private = state.scope == "user"
        status = state.status
        if decision == "stale" and await _mark_stale(connection, job, state, now):
            status = "stale"
            if private:
                outcome.user_stale += 1
            else:
                outcome.stale.append(state.name)
        elif decision == "archive" and await _archive(connection, job, state, now):
            status = "archived"
            if private:
                outcome.user_archived += 1
            else:
                outcome.archived.append(state.name)
        elif decision == "revive" and await _revive(connection, job, state):
            status = "active"
            if not private:
                outcome.revived.append(state.name)
        if status == "active":
            outcome.active += 1
        elif status == "stale":
            outcome.stale_total += 1
    archived_before = (
        await connection.execute(
            select(func.count())
            .select_from(Skill)
            .join(
                first_version,
                and_(
                    first_version.org_id == Skill.org_id,
                    first_version.skill_id == Skill.id,
                    first_version.version_no == 1,
                ),
            )
            .where(coverage, Skill.status == "archived", first_version.author_kind == "agent")
        )
    ).scalar_one()
    outcome.archived_total = int(archived_before)
    top = await connection.execute(
        select(Skill.name, Skill.scope, Skill.use_count, Skill.view_count, Skill.patch_count)
        .where(coverage, Skill.scope != "user", Skill.status != "archived")
        .where(Skill.use_count + Skill.view_count > 0)
        .order_by((Skill.use_count + Skill.view_count).desc(), Skill.name)
        .limit(TOP_SKILLS)
    )
    outcome.top = [(r.name, r.scope, r.use_count, r.view_count, r.patch_count) for r in top]
    return outcome


def _preview(entry: str) -> str:
    text = " ".join(redact(entry).split())
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1] + "…"


async def curate_memory(
    connection: AsyncConnection, job: Any, *, first: bool, now: datetime
) -> MemoryOutcome:
    """The report lines about memory. Nothing changes."""
    outcome = MemoryOutcome()
    rows = (
        await connection.execute(
            select(Memory.id, Memory.scope, Memory.entries, Memory.created_at, AgentProfile.name)
            .outerjoin(
                AgentProfile,
                and_(
                    AgentProfile.org_id == Memory.org_id,
                    AgentProfile.id == Memory.agent_profile_id,
                ),
            )
            .where(
                Memory.org_id == job.org_id,
                Memory.workspace_id == job.workspace_id,
                Memory.scope.in_(("workspace", "agent")),
            )
            .order_by(Memory.scope.desc(), AgentProfile.name)
        )
    ).all()
    for row in rows:
        entries = [str(entry) for entry in row.entries]
        cap = CAPS[Target(row.scope)]
        size = used(entries)
        title = "Workspace notes" if row.scope == "workspace" else f'Agent notes of "{row.name}"'
        line = f"- {title}: {len(entries)} entries, {size}/{cap} characters ({percent(size, cap)}%)"
        if size >= MEMORY_FULL_RATIO * cap:
            line += ". It is nearly full: remove or merge old entries."
        outcome.lines.append(line)
        if not entries:
            continue
        revisions = [
            (r.created_at, [str(e) for e in r.entries])
            for r in await connection.execute(
                select(MemoryRevision.created_at, MemoryRevision.entries)
                .where(MemoryRevision.org_id == job.org_id, MemoryRevision.memory_id == row.id)
                .order_by(MemoryRevision.seq.desc())
                .limit(MAX_MEMORY_REVISIONS)
            )
        ]
        old = [
            entry
            for entry, since in zip(entries, entry_ages(entries, revisions), strict=True)
            if now - (since or row.created_at) >= MEMORY_OLD_AFTER
        ]
        if old:
            outcome.lines.append(
                f"  {len(old)} entries did not change for {MEMORY_OLD_AFTER.days} days or more."
                " Check that they are still true:"
            )
            outcome.lines.extend(f'  - "{_preview(entry)}"' for entry in old[:MAX_PREVIEWS])
    if first:
        cap = CAPS[Target.USER]
        sizes = [
            used([str(entry) for entry in entries])
            for (entries,) in await connection.execute(
                select(Memory.entries).where(Memory.org_id == job.org_id, Memory.scope == "user")
            )
        ]
        full = sum(1 for size in sizes if size >= MEMORY_FULL_RATIO * cap)
        line = f"- User profiles: {len(sizes)} users have notes"
        line += f"; {full} of them are nearly full." if full else "."
        outcome.lines.append(line)
    if not outcome.lines:
        outcome.lines.append("- This workspace has no memory yet.")
    return outcome


def render_report(
    workspace: str, now: datetime, first: bool, skills: SkillOutcome, memory: MemoryOutcome
) -> str:
    scope_text = "workspace skills" + (", org skills, and user skills" if first else "")
    lines = [
        f'Curator report for the workspace "{workspace}", {now.date().isoformat()}.',
        "",
        f"Skills ({scope_text} that an agent wrote): {skills.checked} checked.",
        f"- Active: {skills.active}. Stale: {skills.stale_total}."
        f" Archived (all time): {skills.archived_total}.",
    ]
    if skills.pinned or skills.exempt:
        lines.append(
            f"- Kept: {skills.pinned} pinned, {skills.exempt} named by a job or a workflow."
        )
    if skills.stale:
        lines.append(
            f"- Now stale (no use for {STALE_AFTER.days} days): {names_text(skills.stale)}."
        )
    if skills.archived:
        lines.append(
            f"- Now archived (no use for {ARCHIVE_AFTER.days} days): {names_text(skills.archived)}."
        )
    if skills.revived:
        lines.append(f"- Active again: {names_text(skills.revived)}.")
    if skills.user_stale or skills.user_archived:
        lines.append(
            f"- User skills: {skills.user_stale} now stale, {skills.user_archived} now archived."
            " Each user sees their own skills on the skills page."
        )
    if not (skills.stale or skills.archived or skills.revived):
        lines.append("- No change this week.")
    if skills.top:
        lines += ["", "Most used skills:"]
        lines += [
            f"- {name} ({scope}): {uses} uses, {views} views, {patches} changes"
            for name, scope, uses, views, patches in skills.top
        ]
    lines += ["", "Memory:", *memory.lines, ""]
    lines.append(
        "The curator never deletes anything. Pin a skill on the skills page to keep it active."
        " Restore an archived skill on the skills page."
    )
    return "\n".join(lines)


async def curate(connection: AsyncConnection, job: Any) -> str:
    """Runs the curator for the workspace of ``job`` and returns the report."""
    now = await _now(connection)
    workspace = (
        await connection.execute(
            select(Workspace.name).where(
                Workspace.org_id == job.org_id, Workspace.id == job.workspace_id
            )
        )
    ).scalar_one()
    first = await is_first_workspace(connection, job.org_id, job.workspace_id)
    skills = await curate_skills(connection, job, first=first, now=now)
    memory = await curate_memory(connection, job, first=first, now=now)
    return render_report(workspace, now, first, skills, memory)
