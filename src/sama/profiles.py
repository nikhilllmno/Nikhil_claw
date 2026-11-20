"""Edits to an agent profile from the web app (docs/architecture.md, Section 11.4).

``update_profile`` changes the name, the soul, the model, the toolsets, and the sub-agents of a
profile in one transaction. It writes one audit entry with the names of the changed fields and
the names of the sub-agents, never the text of the soul or of a prompt.

A session keeps the config that it froze, so a change applies from the next session.

This is the M3.8 interim. A change to a profile will go through ``change_propose`` (M6.1) when
that tool exists.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import func, insert, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AgentProfile, AuditEntry
from sama.gate.decide import TOOLSETS
from sama.runtime.contract import (
    SUBAGENT_DESCRIPTION_MAX_CHARS,
    SUBAGENT_MAX_TURNS,
    SUBAGENT_NAME_MAX_CHARS,
    SUBAGENT_NAME_PATTERN,
    SUBAGENT_PROMPT_MAX_CHARS,
    RuntimeKind,
)
from sama.subagents import (
    MAX_SUBAGENTS,
    known_models,
    lock_profile,
    parse_subagents,
    subagents_value,
    supports_subagents,
    validate_subagents,
)
from sama.vault.redact import redact

NAME_MAX_CHARS: Final = 80
SOUL_MAX_CHARS: Final = 20_000


class ProfileError(ValueError):
    """The change is not valid. The text is safe to show to the caller."""


@dataclass(frozen=True, slots=True)
class ProfileChanges:
    """The fields to change. ``None`` means: keep the value."""

    name: str | None = None
    soul: str | None = None
    model: str | None = None
    toolsets: Sequence[str] | None = None
    subagents: Sequence[Any] | None = None
    """The raw JSON of the specs. ``parse_subagents`` checks it."""


def profile_options(runtime: RuntimeKind) -> dict[str, Any]:
    """What an editor of a profile with this runtime can choose, and the limits."""
    return {
        "toolsets": [
            {"name": name, "tools": sorted(tools)} for name, tools in sorted(TOOLSETS.items())
        ],
        "models": known_models(runtime),
        "subagents": supports_subagents(runtime),
        "limits": {
            "name_max_chars": NAME_MAX_CHARS,
            "soul_max_chars": SOUL_MAX_CHARS,
            "max_subagents": MAX_SUBAGENTS,
            "subagent_name_max_chars": SUBAGENT_NAME_MAX_CHARS,
            "subagent_name_pattern": SUBAGENT_NAME_PATTERN,
            "subagent_description_max_chars": SUBAGENT_DESCRIPTION_MAX_CHARS,
            "subagent_prompt_max_chars": SUBAGENT_PROMPT_MAX_CHARS,
            "subagent_max_turns": SUBAGENT_MAX_TURNS,
        },
    }


def _check_toolsets(toolsets: Sequence[str]) -> list[str]:
    if len(set(toolsets)) != len(toolsets):
        raise ProfileError("a toolset is in the list two times")
    unknown = [name for name in toolsets if name not in TOOLSETS]
    if unknown:
        raise ProfileError("the toolset is not known: " + ", ".join(sorted(set(unknown))[:5]))
    return list(toolsets)


async def update_profile(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    changes: ProfileChanges,
    *,
    actor_user_id: uuid.UUID,
) -> list[str]:
    """Applies the changes and writes the audit entry. Returns the names of the changed fields.

    A field with the value that the profile has now is not a change. Without a change, the
    function writes nothing. Raises ``ProfileError`` when a value is not valid, and
    ``SubagentError`` when the profile does not exist or the sub-agents do not fit the profile
    after the change (for example, a toolset that a sub-agent uses is not in the new toolsets).
    """
    soul = changes.soul
    if soul is not None:
        if len(soul) > SOUL_MAX_CHARS:
            raise ProfileError(f"the soul has at most {SOUL_MAX_CHARS} characters")
        if redact(soul) != soul:
            raise ProfileError("the soul contains a secret")
    toolsets = None if changes.toolsets is None else _check_toolsets(changes.toolsets)
    specs = None if changes.subagents is None else parse_subagents(list(changes.subagents))

    async with engine.begin() as connection:
        profile = await lock_profile(connection, org_id, profile_id)
        values: dict[str, Any] = {}
        if changes.name is not None and changes.name != profile.name:
            values["name"] = changes.name
        if soul is not None and soul != profile.soul:
            values["soul"] = soul
        if changes.model is not None and changes.model != profile.model:
            # The stored model can be a model that the table no longer lists. Only a new
            # value must be in the list.
            if changes.model not in known_models(profile.runtime):
                raise ProfileError("the model is not known for the runtime of the profile")
            values["model"] = changes.model
        if toolsets is not None and toolsets != profile.toolsets:
            values["toolsets"] = toolsets
        if specs is None:
            # The toolsets can change, so the stored sub-agents must still fit.
            if "toolsets" in values:
                validate_subagents(
                    parse_subagents(profile.subagents),
                    toolsets=values["toolsets"],
                    runtime=profile.runtime,
                )
        else:
            validate_subagents(
                specs, toolsets=values.get("toolsets", profile.toolsets), runtime=profile.runtime
            )
            if subagents_value(specs) != profile.subagents:
                values["subagents"] = subagents_value(specs)
        if not values:
            return []
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.org_id == org_id, AgentProfile.id == profile_id)
            .values(**values, updated_at=func.now())
        )
        fields = sorted(values)
        await connection.execute(
            insert(AuditEntry).values(
                org_id=org_id,
                workspace_id=profile.workspace_id,
                actor_kind="user",
                actor_user_id=actor_user_id,
                action="profile.updated",
                target_type="agent_profile",
                target_id=str(profile_id),
                details={
                    "fields": fields,
                    "subagents": [spec.name for spec in specs or ()]
                    if "subagents" in values
                    else [],
                },
            )
        )
    return fields
