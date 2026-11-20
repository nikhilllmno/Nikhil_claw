"""The ``/cron`` chat command and the ``cronjob_manage`` tool (M4.2)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Job
from sama.gateway.cron import cron_reply
from sama.gateway.sessions import Target
from sama.scheduler.jobs import JobService
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.cron import CronRequest, job_input, manage
from tests.scheduler.conftest import Tenant


def _target(tenant: Tenant, audience: str = "private") -> Target:
    return Target(
        org_id=tenant.org,
        user_id=tenant.ids["ana"],
        workspace_id=tenant.ids["ws"],
        agent_profile_id=tenant.ids["profile"],
        audience=audience,
    )


async def test_cron_makes_and_lists_jobs(
    engine: AsyncEngine, jobs: JobService, tenant: Tenant
) -> None:
    async def session_of() -> uuid.UUID | None:
        return tenant.ids["chat"]

    target = _target(tenant)
    added = await cron_reply(jobs, target, "add every day at 9am | Send me the news", session_of)
    assert "news" in added.lower()
    async with engine.connect() as connection:
        job = (await connection.execute(select(Job).where(Job.org_id == tenant.org))).one()
    assert (job.origin_session_id, job.created_by_kind, job.status) == (
        tenant.ids["chat"],
        "user",
        "active",
    )
    listed = await cron_reply(jobs, target, "list", session_of)
    assert str(job.id)[:8] in listed
    preview = await cron_reply(jobs, target, "preview every 2 hours", session_of)
    assert preview.startswith("Schedule: every 2 hours\nNext 3 fires: ")
    assert preview.count(" UTC") == 3
    paused = await cron_reply(jobs, target, f"pause {str(job.id)[:8]}", session_of)
    assert "paused" in paused.lower()
    short = await cron_reply(jobs, target, f"pause {str(job.id)[:3]}", session_of)
    assert short == "give the job id (the first characters are enough)"
    await cron_reply(jobs, target, f"remove {str(job.id)[:8]}", session_of)
    async with engine.connect() as connection:
        status = (await connection.execute(select(Job.status).where(Job.id == job.id))).scalar_one()
    assert status == "removed"


async def test_cron_works_only_in_a_direct_chat(jobs: JobService, tenant: Tenant) -> None:
    async def session_of() -> uuid.UUID | None:
        return None

    reply = await cron_reply(jobs, _target(tenant, "shared"), "list", session_of)
    assert reply == "Use /cron in a direct chat with sama."
    off = await cron_reply(None, _target(tenant), "list", session_of)
    assert off == "Scheduled jobs are not on in this deployment."


async def test_the_tool_makes_a_job_of_the_agent(
    engine: AsyncEngine, jobs: JobService, tenant: Tenant
) -> None:
    claims = RunTokenClaims(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(tenant.ids["chat"]),
        run_id=str(tenant.ids["chat-run"]),
        token_id="t",
        expires_at=2_000_000_000,
    )
    fields = job_input(
        name="report",
        schedule="every monday at 8am",
        tz="Asia/Kolkata",
        prompt="Write the weekly report",
        deliver="email:boss@example.test",
    )
    text = await manage(jobs, claims, CronRequest(action="create", fields=fields))
    assert text.startswith("Created the job.")
    async with engine.connect() as connection:
        job = (await connection.execute(select(Job).where(Job.org_id == tenant.org))).one()
    assert (job.created_by_kind, job.created_by_run_id, job.owner_user_id) == (
        "agent",
        tenant.ids["chat-run"],
        tenant.ids["ana"],
    )
    assert job.status == "pending_approval", "an outside email target needs the owner"
    assert "approve" in text.lower()
