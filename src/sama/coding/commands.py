"""The channel commands ``/claude``, ``/codex``, and ``/sama`` (docs/architecture.md,
Section 15.4).

- ``/claude <task>`` and ``/codex <task>`` send one message to the coding agent. The channel
  queues the message with ``RunQueue.enqueue(coding_agent=...)``.
- ``/claude`` and ``/codex`` with no task start the direct mode of the session: each later
  message goes to that coding agent (``sessions.coding_agent``).
- ``/sama`` ends the direct mode.

The commands work in Telegram, in Slack (``/sama claude ...``, ``/sama codex ...``,
``/sama sama``, or a mention with ``/claude ...``), and in the web composer.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.coding.agents import CodingAgent, label
from sama.db.models import Session

COMMANDS: Final = {"/claude": CodingAgent.CLAUDE_CODE, "/codex": CodingAgent.CODEX}
SAMA_COMMAND: Final = "/sama"
_COMMAND = re.compile(r"^(/claude|/codex|/sama)(?:@\w+)?(?:\s+([\s\S]*))?$", re.IGNORECASE)

DIRECT_ON: Final = (
    "Direct mode: each message of this session now goes to {name}, in the sandbox of the "
    "session. Start a message with owner/name to work in a GitHub repository, and end it with "
    "--pr to open a pull request. Send /sama to talk to sama again."
)
DIRECT_OFF: Final = "sama answers the messages of this session again."
NOT_DIRECT: Final = "This session is not in a direct mode. sama answers its messages."
HELP_LINES: Final = (
    "/claude <task> - send a coding task to Claude Code. /claude alone sends each message to "
    "Claude Code until /sama",
    "/codex <task> - send a coding task to Codex. /codex alone sends each message to Codex "
    "until /sama",
)


@dataclass(frozen=True, slots=True)
class CodingCommand:
    """One parsed command. ``agent`` is ``None`` for ``/sama``. ``task`` is empty for a switch
    of the direct mode."""

    agent: CodingAgent | None
    task: str

    @property
    def switch(self) -> bool:
        return not self.task


def parse_command(text: str) -> CodingCommand | None:
    """The coding command of a message, or ``None``. ``/sama`` with text after it is not a
    command here: the Slack app uses ``/sama <name>`` for its own commands."""
    match = _COMMAND.fullmatch(text.strip())
    if match is None:
        return None
    name, task = match.group(1).lower(), (match.group(2) or "").strip()
    if name == SAMA_COMMAND:
        return None if task else CodingCommand(None, "")
    return CodingCommand(COMMANDS[name], task)


async def set_direct(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    session_id: uuid.UUID,
    agent: CodingAgent | None,
) -> str:
    """Starts or ends the direct mode of a session and returns the answer for the user. The
    caller checked that the user can use the session."""
    async with engine.begin() as connection:
        was = (
            await connection.execute(
                select(Session.coding_agent)
                .where(Session.org_id == org_id, Session.id == session_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        await connection.execute(
            update(Session)
            .where(Session.org_id == org_id, Session.id == session_id)
            .values(coding_agent=None if agent is None else agent.value)
        )
    return direct_answer(agent, was)


def direct_answer(agent: CodingAgent | None, was: str | None) -> str:
    """The answer to a switch, when the caller already knows the old mode."""
    if agent is None:
        return DIRECT_OFF if was else NOT_DIRECT
    return DIRECT_ON.format(name=label(agent))
