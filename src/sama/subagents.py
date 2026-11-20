"""The native sub-agents of an agent profile (docs/architecture.md, Section 13.1).

A profile has a list of sub-agent specs (``agent_profiles.subagents``). A spec has toolsets, as
the profile has. The assembler turns the specs into ``SubagentDefinition`` objects when it
builds the config of a session (``resolve``). The definitions have sama tool names and are
frozen with the config. The rules:

- A sub-agent never has more tools than the run. Its tools are the tools of its toolsets that
  the run also has. A write checks that the toolsets are toolsets of the profile.
- A sub-agent cannot start a sub-agent, and it cannot ask a person (``clarify``).
- Only a runtime with the capability ``native_subagents`` gets sub-agents. A review run gets
  none.
- A spec cannot have a secret or an instruction to the model that the injection scan finds.

``set_profile_subagents`` is the only function that writes the column.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_core import ErrorDetails
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AgentProfile, AuditEntry
from sama.gate.decide import TOOLSETS, tools_of
from sama.modelproxy.prices import PriceTable, Provider
from sama.runtime.contract import (
    SUBAGENT_DESCRIPTION_MAX_CHARS,
    SUBAGENT_MAX_TURNS,
    SUBAGENT_NAME_MAX_CHARS,
    SUBAGENT_NAME_PATTERN,
    SUBAGENT_PROMPT_MAX_CHARS,
    RuntimeKind,
    SubagentDefinition,
)
from sama.vault.redact import redact

log: Final = logging.getLogger("sama.subagents")

TOOLSET: Final = "subagents"
"""The toolset that lets a run start a sub-agent. A profile needs it for its sub-agents."""

NOT_FOR_SUBAGENTS: Final = frozenset({TOOLSET, "clarify"})
"""Toolsets that a sub-agent never has. A sub-agent cannot start a sub-agent, and it has no
direct line to the user."""

MAX_SUBAGENTS: Final = 8
"""A profile has at most this number of sub-agents. Each one is in the prompt of the model."""

INHERIT: Final = "inherit"
"""The value of ``model`` for a sub-agent that uses the model of the run."""


class SubagentError(ValueError):
    """The specs of a profile are not valid. The text is safe to show to the caller."""


class SubagentSpec(BaseModel):
    """One sub-agent in the profile. It names toolsets, not tools."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(max_length=SUBAGENT_NAME_MAX_CHARS, pattern=SUBAGENT_NAME_PATTERN)
    description: str = Field(min_length=1, max_length=SUBAGENT_DESCRIPTION_MAX_CHARS)
    prompt: str = Field(min_length=1, max_length=SUBAGENT_PROMPT_MAX_CHARS)
    toolsets: tuple[str, ...] = ()
    model: str | None = Field(default=None, min_length=1, max_length=100)
    """``None`` or ``inherit``: the model of the run. Else a model id with a known price."""
    max_turns: int | None = Field(default=None, gt=0, le=SUBAGENT_MAX_TURNS)

    @model_validator(mode="after")
    def _toolsets_are_unique(self) -> Self:
        if len(set(self.toolsets)) != len(self.toolsets):
            raise ValueError("a toolset is in the list two times")
        return self


_FIELDS: Final = frozenset(SubagentSpec.model_fields)

_MESSAGES: Final = {
    "missing": "is required",
    "extra_forbidden": "is an unknown field",
    "string_too_short": "is too short",
    "string_too_long": "is too long",
    "string_pattern_mismatch": "does not match the pattern",
    "too_long": "has too many items",
    "greater_than": "is too small",
    "less_than_equal": "is too large",
    "string_type": "must be a string",
    "int_type": "must be an integer",
    "int_parsing": "must be an integer",
    "model_type": "must be an object",
    "dict_type": "must be an object",
    "tuple_type": "must be a list",
    "value_error": "has a value that is not valid",
}


def _describe(error: ErrorDetails) -> str:
    """One problem as text for the caller. A name is in the text only when it is a field of the
    schema, and the message is fixed: the key of an extra field is not in it."""
    loc = error["loc"]
    field = loc[0] if loc else None
    if error["type"] == "extra_forbidden" or (field is not None and field not in _FIELDS):
        return "unknown field"
    name = str(field) if field is not None else "value"
    return f"{name}: {_MESSAGES.get(error['type'], 'is not valid')}"


def parse_subagents(raw: Any) -> tuple[SubagentSpec, ...]:
    """The specs from the JSON value of the column or of a file.

    Raises ``SubagentError`` when the value is not a list of valid specs, or when two specs
    have the same name.
    """
    if not isinstance(raw, list):
        raise SubagentError("the sub-agents must be a list")
    if len(raw) > MAX_SUBAGENTS:
        raise SubagentError(f"a profile has at most {MAX_SUBAGENTS} sub-agents")
    specs: list[SubagentSpec] = []
    for index, item in enumerate(raw, start=1):
        try:
            specs.append(SubagentSpec.model_validate(item))
        except ValidationError as error:
            # Only the names of the schema fields and fixed messages. The key of an extra field
            # and the input can be secret: pydantic puts both in ``loc`` and in ``msg``.
            problems = "; ".join(_describe(e) for e in error.errors(include_input=False))
            raise SubagentError(f"sub-agent {index} is not valid ({problems})") from None
    names = [spec.name for spec in specs]
    if len(set(names)) != len(names):
        raise SubagentError("two sub-agents have the same name")
    return tuple(specs)


def supports_subagents(runtime: RuntimeKind) -> bool:
    """True if the runtime has the capability ``native_subagents``. A runtime that the
    sandboxed runtime does not know has no sub-agents."""
    # Here, not at the top: the orchestrator imports the prompt assembler, which imports this.
    from sama.orchestrator.sandboxed import CAPABILITIES

    capabilities = CAPABILITIES.get(runtime)
    return capabilities is not None and capabilities.native_subagents


def _allowed_toolsets(profile_toolsets: Iterable[str]) -> frozenset[str]:
    return frozenset(name for name in profile_toolsets if name in TOOLSETS) - NOT_FOR_SUBAGENTS


def problems(
    specs: Sequence[SubagentSpec], *, profile_toolsets: Iterable[str], runtime: RuntimeKind
) -> list[str]:
    """Why the specs cannot be stored for a profile. An empty list means that they can."""
    if not specs:
        return []
    # Here, not at the top: the prompt package imports this module.
    from sama.prompt.scan import scan

    found: list[str] = []
    profile = frozenset(profile_toolsets)
    if not supports_subagents(runtime):
        found.append(f"the runtime {runtime.value} has no native sub-agents")
    if TOOLSET not in profile:
        found.append(f"the profile needs the toolset {TOOLSET}")
    allowed = _allowed_toolsets(profile)
    prices = PriceTable.load()
    for spec in specs:
        label = f"sub-agent {spec.name}"
        for name in spec.toolsets:
            if name in NOT_FOR_SUBAGENTS:
                found.append(f"{label}: a sub-agent cannot have the toolset {name}")
            elif name not in profile:
                found.append(f"{label}: the profile does not have the toolset {name}")
            elif name not in allowed:
                found.append(f"{label}: the toolset {name} has no tools of sama")
        if (
            spec.model is not None
            and spec.model != INHERIT
            and prices.lookup(Provider.ANTHROPIC, spec.model) is None
        ):
            found.append(f"{label}: the model has no price in the price table")
        for field, text in (("description", spec.description), ("prompt", spec.prompt)):
            findings = list(scan(text))
            if redact(text) != text:
                findings.append("secret")
            if findings:
                found.append(f"{label}: the {field} has {', '.join(findings)}")
    return found


def resolve(
    specs: Sequence[SubagentSpec], *, toolsets: frozenset[str], runtime: RuntimeKind
) -> tuple[SubagentDefinition, ...]:
    """The definitions for the config of a run.

    ``toolsets`` are the effective toolsets of the run. The result is empty when the run does
    not have the toolset ``subagents`` or when the runtime has no native sub-agents. The tools
    of a sub-agent are the tools of its toolsets that the run also has, so a sub-agent never
    widens the tools of the run (the gate checks each call, too).
    """
    if not specs or TOOLSET not in toolsets:
        return ()
    if not supports_subagents(runtime):
        log.warning(
            "the profile has sub-agents, but the runtime has none runtime=%s count=%d",
            runtime.value,
            len(specs),
        )
        return ()
    allowed = _allowed_toolsets(toolsets)
    return tuple(
        SubagentDefinition(
            name=spec.name,
            description=spec.description,
            prompt=spec.prompt,
            tools=tuple(sorted(tools_of(allowed.intersection(spec.toolsets)))),
            model=None if spec.model in (None, INHERIT) else spec.model,
            max_turns=spec.max_turns,
        )
        for spec in specs
    )


async def set_profile_subagents(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    specs: Sequence[SubagentSpec],
    *,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    """Replaces the sub-agents of a profile and writes an audit entry (names only).

    A session keeps the config that it froze, so the change applies from the next session.
    Raises ``SubagentError`` when the profile does not exist or a spec is not valid for it.
    """
    names = [spec.name for spec in specs]
    if len(set(names)) != len(names):
        raise SubagentError("two sub-agents have the same name")
    if len(specs) > MAX_SUBAGENTS:
        raise SubagentError(f"a profile has at most {MAX_SUBAGENTS} sub-agents")
    async with engine.begin() as connection:
        row = (
            await connection.execute(
                select(AgentProfile.toolsets, AgentProfile.runtime, AgentProfile.workspace_id)
                .where(AgentProfile.org_id == org_id, AgentProfile.id == profile_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise SubagentError("the profile does not exist")
        found = problems(
            specs,
            profile_toolsets=row.toolsets if isinstance(row.toolsets, list) else [],
            runtime=RuntimeKind(row.runtime),
        )
        if found:
            raise SubagentError("; ".join(found))
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.org_id == org_id, AgentProfile.id == profile_id)
            .values(
                subagents=[spec.model_dump(mode="json") for spec in specs], updated_at=func.now()
            )
        )
        await connection.execute(
            insert(AuditEntry).values(
                org_id=org_id,
                workspace_id=row.workspace_id,
                actor_kind="user" if actor_user_id else "system",
                actor_user_id=actor_user_id,
                action="profile.subagents_set",
                target_type="agent_profile",
                target_id=str(profile_id),
                details={"names": names},
            )
        )


async def profile_subagents(
    engine: AsyncEngine, org_id: uuid.UUID, profile_id: uuid.UUID
) -> tuple[SubagentSpec, ...]:
    """The specs of a profile. Raises ``SubagentError`` when the profile does not exist."""
    async with engine.connect() as connection:
        raw = (
            await connection.execute(
                select(AgentProfile.subagents).where(
                    AgentProfile.org_id == org_id, AgentProfile.id == profile_id
                )
            )
        ).scalar_one_or_none()
    if raw is None:
        raise SubagentError("the profile does not exist")
    return parse_subagents(raw)
