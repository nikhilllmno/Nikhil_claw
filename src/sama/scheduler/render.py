"""Plain text forms of jobs, for the tool ``cronjob_manage`` and the ``/cron`` commands."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sama.scheduler.jobs import JobView


def _when(value: datetime | str | None) -> str:
    if value is None:
        return "none"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.strftime("%Y-%m-%d %H:%M UTC")


def render_preview(preview: dict[str, Any]) -> str:
    fires = ", ".join(_when(when) for when in preview["next_fires"]) or "none"
    return f"Schedule: {preview['text']}\nNext 3 fires: {fires}"


def render_job(view: JobView, *, full: bool = False) -> str:
    lines = [
        f"{view.name} (id {view.id}, version {view.version})",
        f"Status: {view.status}" + (f" ({view.paused_reason})" if view.paused_reason else ""),
        f"Schedule: {view.schedule_text}",
        "Next fires: " + (", ".join(_when(when) for when in view.next_fires) or "none"),
        f"Deliver: {view.deliver}"
        + (f"; on failure: {view.failure_deliver}" if view.failure_deliver else ""),
    ]
    if view.mode == "script":
        lines.append("Mode: script")
    if view.model:
        lines.append(f"Model: {view.model}")
    if view.preapproved_tools:
        lines.append(
            "Pre-approved tools: " + ", ".join(item["tool"] for item in view.preapproved_tools)
        )
    if view.needs_approval:
        lines.append(
            "Needs the approval of the owner: "
            + "; ".join(view.approval_reasons)
            + f". The owner approves version {view.version} on the jobs page, or with "
            f"the command: cron approve {view.id} {view.version}"
        )
    if full:
        if view.mode == "agent":
            lines.append(f"Prompt: {view.prompt}")
        if view.skills:
            lines.append("Skills: " + ", ".join(view.skills))
        for fire in view.fires[:5]:
            lines.append(
                f"- fire {_when(fire['scheduled_for'])}: {fire['status']}"
                + (f" ({fire['error']})" if fire.get("error") else "")
            )
    return "\n".join(lines)


def render_jobs(views: Sequence[JobView]) -> str:
    if not views:
        return "You have no jobs."
    return "\n\n".join(render_job(view) for view in views)
