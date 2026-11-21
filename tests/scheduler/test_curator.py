"""The curator job of a workspace (docs/architecture.md, Section 9.7, and M6.3)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.curator import curate as curate_module
from sama.curator.ensure import ensure_curator_jobs
from sama.curator.policy import SkillState, decide
from sama.db.models import (
    AuditEntry,
    Delivery,
    Job,
    JobFire,
    Membership,
    Memory,
    Message,
    Run,
    Skill,
    SkillVersion,
    Workspace,
)
from sama.orchestrator.queue import RunQueue
from sama.scheduler.jobs import JobService
from sama.scheduler.service import Scheduler
from sama.scheduler.spec import JobError, JobInput
from tests.scheduler.conftest import Tenant


async def _own_workspace(engine: AsyncEngine, tenant: Tenant) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership)
            .where(Membership.org_id == tenant.org, Membership.workspace_id == tenant.ids["ws"])
            .values(role="owner")
        )


async def _curator_jobs(engine: AsyncEngine, tenant: Tenant) -> list[Any]:
    async with engine.connect() as connection:
        return list(
            (
                await connection.execute(
                    select(Job).where(Job.org_id == tenant.org, Job.mode == "curator")
                )
            ).all()
        )


async def _ensure(engine: AsyncEngine) -> int:
    async with engine.begin() as connection:
        return await ensure_curator_jobs(connection, limit=500)


async def _add_skill(
    engine: AsyncEngine,
    tenant: Tenant,
    name: str,
    *,
    idle_days: int,
    scope: str = "workspace",
    author: str = "agent",
    status: str = "active",
    stale_days: int | None = None,
    pinned: bool = False,
) -> uuid.UUID:
    now = datetime.now(UTC)
    when = now - timedelta(days=idle_days)
    skill_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(Skill).values(
                id=skill_id,
                org_id=tenant.org,
                scope=scope,
                workspace_id=tenant.ids["ws"] if scope == "workspace" else None,
                user_id=tenant.ids["ana"] if scope == "user" else None,
                name=name,
                current_version=1,
                status=status,
                pinned=pinned,
                stale_since=None if stale_days is None else now - timedelta(days=stale_days),
                created_at=now - timedelta(days=200),
                last_used_at=when,
            )
        )
        await connection.execute(
            insert(SkillVersion).values(
                org_id=tenant.org,
                skill_id=skill_id,
                version_no=1,
                name=name,
                description=f"About {name}",
                skill_md=f"---\nname: {name}\ndescription: About {name}\n---\n\nDo it.\n",
                change="create",
                author_kind=author,
            )
        )
    return skill_id


async def _skill(engine: AsyncEngine, skill_id: uuid.UUID) -> Any:
    async with engine.connect() as connection:
        return (await connection.execute(select(Skill).where(Skill.id == skill_id))).one()


async def test_each_owned_workspace_gets_one_curator_job(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await _ensure(engine)
    assert await _curator_jobs(engine, tenant) == [], "a workspace with no owner has no job"

    await _own_workspace(engine, tenant)
    await _ensure(engine)
    [job] = await _curator_jobs(engine, tenant)
    assert (job.name, job.mode, job.status, job.deliver) == (
        "Curator",
        "curator",
        "active",
        "origin",
    )
    assert (job.created_by_kind, job.owner_user_id) == ("system", tenant.ids["ana"])
    assert job.agent_profile_id == tenant.ids["profile"]
    assert job.schedule["expr"].endswith(" * * 1")
    assert job.next_fire_at is not None
    assert job.next_fire_at.weekday() == 0
    assert (job.prompt, job.skills, job.script) == ("", [], None)

    await _ensure(engine)
    assert len(await _curator_jobs(engine, tenant)) == 1
    async with engine.connect() as connection:
        audit = (
            await connection.execute(
                select(AuditEntry.actor_kind).where(
                    AuditEntry.org_id == tenant.org, AuditEntry.action == "job.created"
                )
            )
        ).all()
    assert [row.actor_kind for row in audit] == ["system"]

    jobs = JobService(engine, RunQueue(engine))
    await jobs.remove(tenant.user(), job.id)
    await _ensure(engine)
    [removed] = await _curator_jobs(engine, tenant)
    assert removed.status == "removed", "a removed curator job does not come back"


async def test_the_scheduler_tick_adds_the_job_when_the_flag_is_on(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await _own_workspace(engine, tenant)
    await Scheduler(engine, RunQueue(engine), None).tick()
    assert await _curator_jobs(engine, tenant) == []
    await Scheduler(engine, RunQueue(engine), None, curator=True).tick()
    assert len(await _curator_jobs(engine, tenant)) == 1


async def test_the_curator_fire_moves_skills_and_reports_to_the_owner(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await _own_workspace(engine, tenant)
    await _ensure(engine)
    [job] = await _curator_jobs(engine, tenant)
    ids = {
        "old": await _add_skill(engine, tenant, "old-tips", idle_days=40),
        "gone": await _add_skill(
            engine, tenant, "gone", idle_days=100, status="stale", stale_days=8
        ),
        "waiting": await _add_skill(
            engine, tenant, "waiting", idle_days=100, status="stale", stale_days=2
        ),
        "back": await _add_skill(engine, tenant, "back", idle_days=1, status="stale", stale_days=9),
        "fresh": await _add_skill(engine, tenant, "fresh", idle_days=3),
        "pinned": await _add_skill(engine, tenant, "pinned-one", idle_days=100, pinned=True),
        "named": await _add_skill(engine, tenant, "job-skill", idle_days=100),
        "person": await _add_skill(engine, tenant, "by-person", idle_days=100, author="user"),
        "mine": await _add_skill(engine, tenant, "my-own", idle_days=40, scope="user"),
    }
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        await connection.execute(
            insert(Job).values(
                org_id=tenant.org,
                workspace_id=tenant.ids["ws"],
                owner_user_id=tenant.ids["ana"],
                agent_profile_id=tenant.ids["profile"],
                name="uses a skill",
                schedule={"kind": "interval", "seconds": 3600},
                prompt="Go",
                skills=["job-skill"],
                status="paused",
            )
        )
        await connection.execute(
            insert(Memory).values(
                org_id=tenant.org,
                scope="workspace",
                workspace_id=tenant.ids["ws"],
                entries=["The office is in Pune.", "Deploys run on Friday."],
                created_at=now - timedelta(days=120),
            )
        )
        await connection.execute(
            update(Job).where(Job.id == job.id).values(next_fire_at=now - timedelta(minutes=1))
        )

    scheduler = Scheduler(engine, RunQueue(engine), None, curator=True)
    await scheduler.tick()

    status = {key: (await _skill(engine, value)).status for key, value in ids.items()}
    assert status == {
        "old": "stale",
        "gone": "archived",
        "waiting": "stale",
        "back": "active",
        "fresh": "active",
        "pinned": "active",
        "named": "active",
        "person": "active",
        "mine": "stale",
    }
    gone = await _skill(engine, ids["gone"])
    assert (gone.current_version, gone.stale_since) == (2, None)
    assert (await _skill(engine, ids["old"])).stale_since is not None
    async with engine.connect() as connection:
        version = (
            await connection.execute(
                select(SkillVersion).where(
                    SkillVersion.skill_id == ids["gone"], SkillVersion.version_no == 2
                )
            )
        ).one()
        assert (version.change, version.author_kind, version.author_user_id) == (
            "archive",
            "system",
            None,
        )
        assert "Do it." in version.skill_md
        assert (
            await connection.execute(
                select(func.count()).select_from(Skill).where(Skill.org_id == tenant.org)
            )
        ).scalar_one() == len(ids), "the curator deletes nothing"
        fire = (await connection.execute(select(JobFire).where(JobFire.job_id == job.id))).one()
        assert (fire.status, fire.run_id) == ("completed", None)
        assert fire.started_at is not None
        assert fire.finished_at is not None
        assert (
            await connection.execute(select(Run.id).where(Run.session_id == fire.session_id))
        ).all() == []
        [message] = (
            await connection.execute(
                select(Message.role, Message.content).where(Message.session_id == fire.session_id)
            )
        ).all()
        [delivery] = (
            await connection.execute(select(Delivery).where(Delivery.fire_id == fire.id))
        ).all()
        actions = sorted(
            row.action
            for row in await connection.execute(
                select(AuditEntry.action).where(
                    AuditEntry.org_id == tenant.org, AuditEntry.actor_kind == "system"
                )
            )
        )
    report = message.content["text"]
    assert message.role == "assistant"
    assert report == fire.output
    assert 'Curator report for the workspace "ws"' in report
    assert "Now stale (no use for 30 days): old-tips." in report
    assert "Now archived (no use for 90 days): gone." in report
    assert "Active again: back." in report
    assert "Kept: 1 pinned, 1 named by a job or a workflow." in report
    assert "User skills: 1 now stale, 0 now archived." in report
    assert "my-own" not in report
    assert "by-person" not in report
    assert "Workspace notes: 2 entries" in report
    assert '"The office is in Pune."' in report
    assert "User profiles: 0 users have notes." in report
    assert "The curator never deletes anything." in report
    assert (delivery.kind, delivery.target) == ("result", "origin")
    assert delivery.text.startswith("Curator\n\n")
    assert actions == [
        "job.created",
        "job.fired",
        "skill.active",
        "skill.archive",
        "skill.stale",
        "skill.stale",
    ]
    await scheduler.start_scripts()
    async with engine.connect() as connection:
        fires = (await connection.execute(select(JobFire).where(JobFire.job_id == job.id))).all()
    assert [f.status for f in fires] == ["completed"], "the scripts step never claims the fire"


async def test_only_the_first_workspace_covers_org_and_user_skills(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await _own_workspace(engine, tenant)
    second = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(Workspace).values(id=second, org_id=tenant.org, name="two", slug="two")
        )
        await connection.execute(
            insert(Membership).values(
                org_id=tenant.org, user_id=tenant.ids["ana"], workspace_id=second, role="owner"
            )
        )
    org_skill = await _add_skill(engine, tenant, "org-tips", idle_days=40, scope="org")
    await _ensure(engine)
    jobs = {job.workspace_id: job for job in await _curator_jobs(engine, tenant)}
    assert set(jobs) == {tenant.ids["ws"], second}
    service = JobService(engine, RunQueue(engine))
    await service.run_now(tenant.user(workspace_id=second), jobs[second].id)
    assert (await _skill(engine, org_skill)).status == "active"
    await service.run_now(tenant.user(), jobs[tenant.ids["ws"]].id)
    assert (await _skill(engine, org_skill)).status == "stale"


async def test_kept_skills_do_not_fill_the_batch(
    engine: AsyncEngine, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The batch reads only skills that change, so old pinned skills never hide the others."""
    monkeypatch.setattr(curate_module, "MAX_SKILLS", 2)
    await _own_workspace(engine, tenant)
    for n in range(3):
        await _add_skill(engine, tenant, f"kept-{n}", idle_days=400, pinned=True)
    late = await _add_skill(engine, tenant, "late", idle_days=40)
    await _ensure(engine)
    [job] = await _curator_jobs(engine, tenant)
    await JobService(engine, RunQueue(engine)).run_now(tenant.user(), job.id)
    assert (await _skill(engine, late)).status == "stale"


async def test_a_pin_or_a_use_during_the_run_wins(engine: AsyncEngine, tenant: Tenant) -> None:
    """The curator decides from a snapshot. A pin or a use that commits after the snapshot
    keeps the skill: the update checks the row again."""
    pinned = await _add_skill(engine, tenant, "pinned-late", idle_days=40)
    used = await _add_skill(
        engine, tenant, "used-late", idle_days=100, status="stale", stale_days=10
    )
    now = datetime.now(UTC)
    job = SimpleNamespace(org_id=tenant.org, workspace_id=tenant.ids["ws"], id=uuid.uuid4())

    def snapshot(skill_id: uuid.UUID, name: str, status: str, idle: int) -> SkillState:
        return SkillState(
            skill_id=skill_id,
            scope="workspace",
            name=name,
            status=status,
            pinned=False,
            exempt=False,
            last_active=now - timedelta(days=idle),
            stale_since=now - timedelta(days=10) if status == "stale" else None,
        )

    before_pin = snapshot(pinned, "pinned-late", "active", 40)
    before_use = snapshot(used, "used-late", "stale", 100)
    assert decide(before_pin, now) == "stale"
    assert decide(before_use, now) == "archive"
    async with engine.begin() as connection:
        await connection.execute(update(Skill).where(Skill.id == pinned).values(pinned=True))
        await connection.execute(
            update(Skill).where(Skill.id == used).values(last_used_at=func.now())
        )
    async with engine.begin() as connection:
        assert not await curate_module._mark_stale(connection, job, before_pin, now)
        assert not await curate_module._archive(connection, job, before_use, now)
    assert (await _skill(engine, pinned)).status == "active"
    after = await _skill(engine, used)
    assert (after.status, after.current_version) == ("stale", 1)


async def test_a_person_changes_only_the_name_schedule_and_targets(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await _own_workspace(engine, tenant)
    await _ensure(engine)
    [job] = await _curator_jobs(engine, tenant)
    service = JobService(engine, RunQueue(engine))
    view = await service.update(
        tenant.user(), job.id, JobInput(name="Weekly tidy", schedule="0 9 * * 5", tz="UTC")
    )
    assert (view.name, view.mode, view.prompt, view.status) == (
        "Weekly tidy",
        "curator",
        "",
        "active",
    )
    with pytest.raises(JobError, match="fixed prompt"):
        await service.update(tenant.user(), job.id, JobInput(prompt="Do something else"))
    with pytest.raises(JobError, match="fixed mode"):
        await service.update(tenant.user(), job.id, JobInput(mode="agent"))
    same = await service.update(tenant.user(), job.id, JobInput(prompt="", skills=[]))
    assert same.mode == "curator"
    await service.pause(tenant.user(), job.id)
    assert (await service.resume(tenant.user(), job.id)).status == "active"
