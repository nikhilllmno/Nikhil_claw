"""Edits to an agent profile from the web app (docs/architecture.md, Section 11.4).

``update_profile`` changes the name, the soul, the runtime, the model, the toolsets, and the
sub-agents of a profile in one transaction. It writes one audit entry with the names of the
changed fields and the names of the sub-agents, never the text of the soul or of a prompt.

A session keeps the soul, the runtime, the model, and the sub-agents that it froze, so those
changes apply from the next session. The gate reads the toolsets on every tool call, so a change
to the toolsets applies at the next tool call, also in a session that is running.

This is the M3.8 interim. A change to a profile will go through ``change_propose`` (M6.1) when
that tool exists.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AgentProfile, AuditEntry, Membership
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
    model_is_known,
    parse_subagents,
    subagents_value,
    supports_subagents,
    validate_subagents,
)
from sama.tenancy.access import ADMIN_ROLES
from sama.vault.redact import redact

EDITABLE_RUNTIMES: Final = (RuntimeKind.CLAUDE_AGENT_SDK, RuntimeKind.OPENAI_AGENTS_SDK)
"""The runtimes that an editor can choose. ``fake`` is for tests only."""
NAME_MAX_CHARS: Final = 80
SOUL_MAX_CHARS: Final = 20_000


class ProfileError(ValueError):
    """The change is not valid. The text is safe to show to the caller."""


class NotAdminError(Exception):
    """The actor is not an org owner or admin any more when the write runs."""


@dataclass(frozen=True, slots=True)
class ProfileChanges:
    """The fields to change. ``None`` means: keep the value."""

    name: str | None = None
    soul: str | None = None
    runtime: RuntimeKind | None = None
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
        "runtimes": [
            {
                "name": kind.value,
                "models": known_models(kind),
                "subagents": supports_subagents(kind),
            }
            for kind in EDITABLE_RUNTIMES
        ],
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
) -> None:
    """Applies the changes and writes the audit entry.

    A field with the value that the profile has now is not a change. Without a change, the
    function writes nothing. Raises ``ProfileError`` when a value is not valid, and
    ``SubagentError`` when the profile does not exist or the sub-agents do not fit the profile
    after the change (for example, a toolset that a sub-agent uses is not in the new toolsets).
    Raises ``NotAdminError`` when the actor is not an org owner or admin at the time of the
    write: the caller checked the role earlier, and the role can change while the write waits
    for the lock of the profile.

    A change of the runtime needs a model of the new runtime: the new model of the change, or
    the model that the profile has now. A review model that the new runtime does not know goes
    back to the default of the runtime. The stored sub-agents must fit the new runtime.
    """
    if changes.runtime is not None and changes.runtime not in EDITABLE_RUNTIMES:
        raise ProfileError(f"the runtime {changes.runtime.value} cannot be chosen")
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
        # Checked after the lock of the profile, and the share lock keeps the membership until
        # the end of the transaction, so a demotion that is waiting cannot slip in before the
        # write.
        admin = await connection.execute(
            select(Membership.id)
            .where(
                Membership.org_id == org_id,
                Membership.user_id == actor_user_id,
                Membership.workspace_id.is_(None),
                Membership.role.in_(ADMIN_ROLES),
            )
            .with_for_update(read=True)
        )
        if admin.first() is None:
            raise NotAdminError
        values: dict[str, Any] = {}
        subagent_names: list[str] = []
        if changes.name is not None and changes.name != profile.name:
            values["name"] = changes.name
        if soul is not None and soul != profile.soul:
            values["soul"] = soul
        runtime = profile.runtime
        if changes.runtime is not None and changes.runtime != profile.runtime:
            runtime = changes.runtime
            values["runtime"] = runtime.value
        if "runtime" in values:
            model = profile.model if changes.model is None else changes.model
            if model not in known_models(runtime):
                raise ProfileError("choose a model of the new runtime")
            if model != profile.model:
                values["model"] = model
            if profile.review_model is not None and not model_is_known(
                runtime, profile.review_model
            ):
                values["review_model"] = None
        elif changes.model is not None and changes.model != profile.model:
            # The stored model can be a model that the table no longer lists. Only a new
            # value must be in the list.
            if changes.model not in known_models(runtime):
                raise ProfileError("the model is not known for the runtime of the profile")
            values["model"] = changes.model
        if toolsets is not None and toolsets != profile.toolsets:
            values["toolsets"] = toolsets
        if specs is None:
            # The toolsets or the runtime can change, so the stored sub-agents must still fit.
            if "toolsets" in values or "runtime" in values:
                validate_subagents(
                    parse_subagents(profile.subagents),
                    toolsets=values.get("toolsets", profile.toolsets),
                    runtime=runtime,
                )
        else:
            validate_subagents(
                specs, toolsets=values.get("toolsets", profile.toolsets), runtime=runtime
            )
            value = subagents_value(specs)
            if value != profile.subagents:
                values["subagents"] = value
                subagent_names = [spec.name for spec in specs]
        if not values:
            return
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.org_id == org_id, AgentProfile.id == profile_id)
            .values(**values, updated_at=func.now())
        )
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
                    "fields": sorted(values),
                    "subagents": subagent_names,
                },
            )
        )
