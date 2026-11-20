"""Skills as slash commands: ``/<skill-name> <request>`` (docs/architecture.md, Section 9.5).

The gateways and the web API turn a skill command into a normal user message. The new text
tells the agent to load the skill. The channels themselves keep the reserved command names.
"""

from __future__ import annotations

import re
from typing import Final

RESERVED: Final = frozenset(
    {
        "new",
        "stop",
        "model",
        "start",
        "pair",
        "help",
        "skills",
        "skill",
        "approve",
        "deny",
        "resume",
        "memory",
        "cron",
        "compact",
        "learn",
        "connect",
        "reset",
        "status",
        "settings",
        "cancel",
        "sama",
    }
)
"""Command names of the channels. A skill cannot have one of these names."""

_COMMAND: Final = re.compile(
    r"/([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?\Z", re.S
)


def parse(text: str) -> tuple[str, str] | None:
    """The skill name and the request of a message that starts with ``/name``.

    The name is in lowercase, and ``_`` is ``-``, because Telegram does not allow a hyphen in
    a command. A reserved name is not a skill command. The result does not mean that the skill
    exists.
    """
    match = _COMMAND.match(text.strip())
    if match is None:
        return None
    name = match.group(1).lower().replace("_", "-")
    if name in RESERVED or name.startswith("-") or name.endswith("-") or "--" in name:
        return None
    return name, (match.group(2) or "").strip()


def expansion(name: str, request: str) -> str:
    """The message text for the agent: the request, then the instruction to use the skill."""
    if request:
        return (
            f'{request}\n\n(Skill command: load the skill "{name}" with skill_view and follow '
            "it for this request.)"
        )
    return (
        f'(Skill command: load the skill "{name}" with skill_view and follow it. The user '
        "gave no request with the command, so ask what the user wants.)"
    )
