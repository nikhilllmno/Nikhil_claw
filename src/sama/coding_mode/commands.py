"""The ``/plan`` command of the chat channels (docs/architecture.md, Section 15.5).

- ``/plan TASK``: plan mode on, then TASK goes to the agent as a message.
- ``/plan``: plan mode and the newest plan.
- ``/plan approve``: approve the newest proposed plan. ``/plan edit TEXT``: approve it with
  TEXT in its place. ``/plan reject [FEEDBACK]``: reject it.
- ``/plan on`` and ``/plan off``: plan mode on or off.

The web composer, Telegram, and Slack use the same rules. ``prefix`` is how a channel writes a
command: ``/`` in Telegram, ``/sama `` in Slack.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sama.coding_mode.service import CodingError, CodingService, describe_plan
from sama.orchestrator.queue import QueueError

PLAN_COMMAND: Final = "plan"
NO_PLAN: Final = "This session has no open plan. Send {prefix}plan TASK to start one."


@dataclass(frozen=True, slots=True)
class PlanReply:
    text: str
    run_id: uuid.UUID | None = None
    """The run that answers, when the command sent a message to the agent."""


def plan_action(rest: str) -> tuple[str, str]:
    """The action of ``/plan REST`` and its text: ``show``, ``on``, ``off``, ``approve``,
    ``edit``, ``reject``, or ``task``."""
    text = rest.strip()
    if not text:
        return "show", ""
    word, _, tail = text.partition(" ")
    word = word.lower()
    tail = tail.strip()
    if word in ("on", "off", "approve") and not tail:
        return word, ""
    if word == "edit" and tail:
        return "edit", tail
    if word == "reject":
        return "reject", tail
    return "task", text


async def plan_reply(
    service: CodingService | None,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None,
    session_id: uuid.UUID | None,
    rest: str,
    prefix: str = "/",
) -> PlanReply:
    if service is None:
        return PlanReply("Coding mode is not available here.")
    if session_id is None:
        return PlanReply(NO_PLAN.format(prefix=prefix))
    action, text = plan_action(rest)
    try:
        match action:
            case "show":
                state = await service.state(org_id=org_id, session_id=session_id)
                return PlanReply(describe_plan(state.plan, state.plan_mode, prefix))
            case "on" | "off":
                await service.set_plan_mode(
                    org_id=org_id, session_id=session_id, user_id=user_id, on=action == "on"
                )
                if action == "on":
                    return PlanReply(
                        "Plan mode is on. sama proposes a plan before it changes files."
                    )
                return PlanReply("Plan mode is off.")
            case "task":
                started = await service.start_plan(
                    org_id=org_id, session_id=session_id, user_id=user_id, task=text
                )
                return PlanReply("Plan mode is on. sama makes a plan first.", started)
        plan = await service.latest_proposed(org_id=org_id, session_id=session_id)
        if plan is None:
            return PlanReply(NO_PLAN.format(prefix=prefix))
        try:
            _decided, run_id = await service.decide(
                org_id=org_id,
                session_id=session_id,
                user_id=user_id,
                plan_id=plan.id,
                approve=action != "reject",
                text=(text or None) if action != "approve" else None,
            )
        except QueueError as error:
            return PlanReply(
                f"sama could not send the decision to the agent, so the plan is open: {error}"
            )
        if action == "reject":
            answer = f"Plan version {plan.version} is rejected. Plan mode stays on."
            if run_id is None:
                answer += f" Send {prefix}plan TASK or a message with feedback."
            return PlanReply(answer, run_id)
        return PlanReply(
            f"Plan version {plan.version} is approved. Plan mode is off, and sama starts the work.",
            run_id,
        )
    except CodingError as error:
        return PlanReply(str(error))
    except QueueError as error:
        return PlanReply(str(error))
