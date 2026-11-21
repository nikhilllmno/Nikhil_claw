"""The channel commands ``/claude``, ``/codex``, and ``/sama`` (docs/architecture.md,
Section 15.4).

- ``/claude <task>`` and ``/codex <task>`` send one message to the coding agent. The channel
  queues the message with ``RunQueue.enqueue(coding_agent=...)``.
- ``/claude`` and ``/codex`` with no task start the direct mode of the session: each later
  message goes to that coding agent (``sessions.coding_agent``).
- ``/sama`` ends the direct mode.

A switch changes the session for each person in it, so only the owner of the session, or an
admin of the workspace or the org in a shared session, may switch (``may_switch``, the rule of
the settings console). A task (``/claude <task>``) is open to each member who may send
messages; the tool gate asks for it as usual.

The commands work in Telegram, in Slack (``/sama claude ...``, ``/sama codex ...``,
``/sama sama``, or a mention with ``/claude ...``), and in the web composer.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.coding.agents import CodingAgent, label
from sama.db.models import AuditEntry, Session
from sama.tenancy.access import Grants, load_grants

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
NOT_ALLOWED: Final = (
    "Only the owner or an admin can switch this shared session to a coding agent and back. "
    "You can still send one task: write /claude or /codex, then the task."
)
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


class SwitchNotAllowedError(Exception):
    """The user may not switch the direct mode of the session. The message is for the user."""


def may_switch(
    grants: Grants, *, audience: str, owner_user_id: uuid.UUID | None, workspace_id: uuid.UUID
) -> bool:
    """The rule of the settings console (``PATCH /sessions/{id}``, M7.10): the owner of the
    session may switch it. In a shared session an admin of the workspace or of the org may too.
    A switch changes the session for each person in it."""
    return bool(
        owner_user_id == grants.user_id
        or (audience == "shared" and (grants.org_admin or grants.admins_workspace(workspace_id)))
    )


async def set_direct(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    session_id: uuid.UUID,
    agent: CodingAgent | None,
    *,
    user_id: uuid.UUID,
    audit: bool = False,
) -> str:
    """Starts or ends the direct mode of a session for ``user_id`` and returns the answer for
    the user. Raises ``SwitchNotAllowedError`` when the user may not switch the session
    (``may_switch``) or cannot see it. With ``audit`` (the web app and the settings console,
    M7.10), a change writes the audit entry ``session.coding_agent_changed``."""
    async with engine.begin() as connection:
        row = (
            await connection.execute(
                select(
                    Session.coding_agent,
                    Session.audience,
                    Session.owner_user_id,
                    Session.workspace_id,
                )
                .where(Session.org_id == org_id, Session.id == session_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise SwitchNotAllowedError("The session does not exist.")
        grants = await load_grants(connection, org_id, user_id)
        if not grants.sees_session(row.workspace_id, row.audience, row.owner_user_id):
            raise SwitchNotAllowedError("The session does not exist.")
        if not may_switch(
            grants,
            audience=row.audience,
            owner_user_id=row.owner_user_id,
            workspace_id=row.workspace_id,
        ):
            raise SwitchNotAllowedError(NOT_ALLOWED)
        await connection.execute(
            update(Session)
            .where(Session.org_id == org_id, Session.id == session_id)
            .values(coding_agent=None if agent is None else agent.value)
        )
        now = None if agent is None else agent.value
        if audit and now != row.coding_agent:
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=org_id,
                    actor_kind="user",
                    actor_user_id=user_id,
                    action="session.coding_agent_changed",
                    target_type="session",
                    target_id=str(session_id),
                    details={"from": row.coding_agent, "to": now},
                )
            )
    return direct_answer(agent, row.coding_agent)


def direct_answer(agent: CodingAgent | None, was: str | None) -> str:
    """The answer to a switch, when the caller already knows the old mode."""
    if agent is None:
        return DIRECT_OFF if was else NOT_DIRECT
    return DIRECT_ON.format(name=label(agent))
