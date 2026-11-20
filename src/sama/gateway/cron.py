"""The ``/cron`` command of Telegram and Slack (docs/architecture.md, Section 12.1).

The person who sends the command is the actor. A job that a person makes in a chat is approved
at once, and its target ``origin`` is that chat. The command works only in a direct chat, so
that the jobs of one person do not show in a group.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Final

from sama.gateway.sessions import Target
from sama.scheduler.jobs import Actor, JobInput, JobService, JobView, preview
from sama.scheduler.render import render_job, render_jobs, render_preview
from sama.scheduler.spec import JobError

CRON_HELP: Final = (
    "{cron} - list your jobs\n"
    "{cron} add <schedule> | <prompt> - make a job, for example "
    "{cron} add every weekday at 9am | Summarize the news about AI\n"
    "{cron} preview <schedule> - show the next 3 fire times\n"
    "{cron} show|pause|resume|run|cancel|remove <job id>\n"
    "{cron} approve <job id> <version> - approve a job that the agent made"
)
MIN_PREFIX: Final = 4

SessionOf = Callable[[], Awaitable[uuid.UUID | None]]


async def _find(jobs: JobService, actor: Actor, text: str) -> JobView:
    """The job whose id starts with ``text``. A short prefix is enough when it is unique."""
    key = text.strip().lower()
    if len(key) < MIN_PREFIX:
        raise JobError("give the job id (the first characters are enough)")
    found = [view for view in await jobs.list(actor) if str(view.id).startswith(key)]
    if len(found) != 1:
        raise JobError("no job has that id" if not found else "more jobs have that id start")
    return found[0]


async def cron_reply(
    jobs: JobService | None,
    target: Target,
    rest: str,
    session_of: SessionOf,
    *,
    command: str = "/cron",
) -> str:
    """The answer to ``/cron <rest>``. ``session_of`` gives the session of the chat, and makes
    one when the chat has none. ``command`` is the command text for the help."""
    if jobs is None:
        return "Scheduled jobs are not on in this deployment."
    if target.audience != "private":
        return f"Use {command} in a direct chat with sama."
    usage = CRON_HELP.format(cron=command)
    verb, _, args = rest.strip().partition(" ")
    verb, args = verb.lower(), args.strip()
    actor = Actor(
        org_id=target.org_id,
        user_id=target.user_id,
        kind="user",
        workspace_id=target.workspace_id,
        agent_profile_id=target.agent_profile_id,
    )
    try:
        if verb in ("", "list"):
            return render_jobs(await jobs.list(actor))
        if verb == "help":
            return usage
        if verb == "preview":
            return render_preview(preview(args))
        if verb == "add":
            schedule, bar, prompt = args.partition("|")
            if not bar or not schedule.strip() or not prompt.strip():
                return f"Write {command} add <schedule> | <prompt>."
            actor = replace(actor, session_id=await session_of())
            name = " ".join(prompt.split())[:60]
            view = await jobs.create(
                actor, JobInput(name=name, schedule=schedule.strip(), prompt=prompt.strip())
            )
            return "Made the job.\n" + render_job(view)
        if verb == "approve":
            ident, _, version = args.partition(" ")
            if not version.strip().isdecimal():
                return f"Write {command} approve <job id> <version>. {command} show gives it."
            view = await _find(jobs, actor, ident)
            approved = await jobs.approve(actor, view.id, int(version.strip()))
            return "Approved.\n" + render_job(approved)
        if verb in ("show", "pause", "resume", "run", "cancel", "remove"):
            view = await _find(jobs, actor, args)
            if verb == "show":
                return render_job(await jobs.get(actor, view.id), full=True)
            if verb == "pause":
                return "Paused.\n" + render_job(await jobs.pause(actor, view.id))
            if verb == "resume":
                return "Resumed.\n" + render_job(await jobs.resume(actor, view.id))
            if verb == "run":
                await jobs.run_now(actor, view.id)
                return "The job fires now."
            if verb == "cancel":
                count = await jobs.cancel(actor, view.id)
                return f"Cancelled {count} open run(s)."
            await jobs.remove(actor, view.id)
            return "Removed."
    except JobError as error:
        return str(error)
    return usage
