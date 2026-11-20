"""The tool ``cronjob_manage`` (docs/architecture.md, Section 12.1).

The agent uses it to make and change the scheduled jobs of the run user. The gate gives it
the policy ``ask`` in a shared session. A job run does not have it (the gate removes the
toolset ``cron``), and ``JobService`` refuses each change from a job run as a second check.
"""

from __future__ import annotations

import uuid
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from sama.scheduler.jobs import JobInput, JobService, preview
from sama.scheduler.render import render_job, render_jobs, render_preview
from sama.scheduler.spec import JobError
from sama.tenancy.run_token import RunTokenClaims

CRONJOB_MANAGE: Final = "cronjob_manage"

CronAction = Literal[
    "create", "list", "get", "update", "pause", "resume", "remove", "run", "cancel", "preview"
]

DESCRIPTION: Final = (
    "Make and manage scheduled jobs. A job runs a prompt (or a sandbox script) on a schedule, "
    "with no person in the run, and sends the result. Actions: preview (schedule, tz: show the "
    "checked schedule and its next 3 fire times), create (name, schedule, prompt, and the "
    "optional fields), list, get (job_id), update (job_id and the fields to change; give an "
    "empty text to clear model or failure_deliver), pause, resume, remove, run (fire now), "
    "cancel (stop the open fire). schedule is plain English ('every 2 hours', 'daily at 9am', "
    "'every monday and thursday at 18:00', 'monthly on the 1st at 8am', 'tomorrow at 7pm') or "
    "a 5-field cron expression. tz is an IANA time zone (default UTC). deliver and "
    "failure_deliver are origin (this chat), none, slack:<channel>, telegram:<chat id>, or "
    "email:<address>. preapproved_tools lists the tools that the job may use with no person "
    "there: {tool, limits: {argument: [allowed values]}}. A job that pre-approves tools, runs "
    "a script, or sends to another place waits until its owner approves it with /cron approve "
    "or on the jobs page. Tell the user that. Always preview a schedule and show the next fire "
    "times to the user before you create the job."
)


class CronRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: CronAction
    job_id: uuid.UUID | None = None
    fields: JobInput


async def manage(jobs: JobService, claims: RunTokenClaims, request: CronRequest) -> str:
    """Runs one action for the user of the run, and returns the text for the model."""
    given = request.fields
    if request.action == "preview":
        if given.schedule is None:
            raise JobError("preview needs a schedule")
        return render_preview(preview(given.schedule, given.tz))
    actor = await jobs.agent_actor(
        org_id=uuid.UUID(claims.org_id),
        workspace_id=uuid.UUID(claims.workspace_id),
        session_id=uuid.UUID(claims.session_id),
        run_id=uuid.UUID(claims.run_id),
        user_id=uuid.UUID(claims.user_id) if claims.user_id else None,
    )
    if request.action == "list":
        return render_jobs(await jobs.list(actor))
    if request.action == "create":
        view = await jobs.create(actor, given)
        return "Created the job.\n" + render_job(view)
    if request.job_id is None:
        raise JobError(f"{request.action} needs job_id")
    job_id = request.job_id
    if request.action == "get":
        return render_job(await jobs.get(actor, job_id), full=True)
    if request.action == "update":
        return "Changed the job.\n" + render_job(await jobs.update(actor, job_id, given))
    if request.action == "pause":
        return "Paused the job.\n" + render_job(await jobs.pause(actor, job_id))
    if request.action == "resume":
        return "Resumed the job.\n" + render_job(await jobs.resume(actor, job_id))
    if request.action == "remove":
        await jobs.remove(actor, job_id)
        return "Removed the job."
    if request.action == "run":
        fire_id = await jobs.run_now(actor, job_id)
        return f"The job fires now (fire {fire_id}). The result goes to its deliver target."
    count = await jobs.cancel(actor, job_id)
    return f"Cancelled {count} open run(s) of the job."


def job_input(**values: Any) -> JobInput:
    """A ``JobInput`` with only the given fields set, so that an update changes only them."""
    given = {name: value for name, value in values.items() if value is not None}
    try:
        return JobInput.model_validate(given)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()[:5]
        )
        raise JobError(f"the job fields are not valid: {problems}") from None
