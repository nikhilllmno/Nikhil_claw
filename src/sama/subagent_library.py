"""The sub-agent library (docs/architecture.md, Section 13.3, M7.4).

A library agent is a sub-agent with a name, a description, a prompt, toolsets, a model, and
limits (turns and cost). It belongs to the org, to one workspace, or to one user (personal).
Sama uses a library agent in two ways:

- An agent profile names library agents in ``agent_profiles.library_subagents``. The prompt
  assembler freezes them in the config of a session as native sub-agents (Section 13.1), with
  the inline sub-agents of the profile.
- ``delegate_task(agent=...)`` starts a library agent as a delegated child (Section 13.2). The
  child session keeps the frozen spec in ``sessions.library_agent``, and runs with the prompt,
  the toolsets, the model, and the limits of the agent. A test run of the web app uses the
  same path in a new private session of the person.

The rules of Section 13.1 apply: a library agent never has more tools than the run, it cannot
start a sub-agent or ask a person, and the description and the prompt cannot have a secret or a
finding of the injection scan.

Who can write: any member writes their own personal agents, with no approval. A workspace agent
needs a workspace admin or an org owner or admin, and an org agent needs an org owner or admin.
Each write makes a version in ``library_agent_versions`` and an audit entry with the name only.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import (
    AgentProfile,
    AuditEntry,
    LibraryAgent,
    LibraryAgentVersion,
)
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
    INHERIT,
    MAX_SUBAGENTS,
    NOT_FOR_SUBAGENTS,
    TOOLSET,
    SubagentSpec,
    known_models,
    model_is_known,
    supports_subagents,
)
from sama.tenancy.access import Grants
from sama.vault.redact import redact

log: Final = logging.getLogger("sama.subagent_library")

Scope = Literal["user", "workspace", "org"]
SCOPES: Final[tuple[Scope, ...]] = ("user", "workspace", "org")
"""From the narrowest to the widest. A name in a narrow scope hides the same name in a wider
scope for ``delegate_task(agent=...)``."""
MAX_COST_USD: Final = Decimal("10")
"""The highest cost limit of a library agent. It is the limit of a delegated child, too."""
LIBRARY_MODEL_RUNTIMES: Final = (RuntimeKind.CLAUDE_AGENT_SDK, RuntimeKind.OPENAI_AGENTS_SDK)
SPEC_FIELDS: Final = (
    "name",
    "description",
    "prompt",
    "toolsets",
    "model",
    "max_turns",
    "max_cost_usd",
)


class LibraryError(ValueError):
    """The request is not valid. The text is safe to show to the caller."""


class LibraryNotFoundError(LookupError):
    """The agent does not exist, or the caller cannot see it."""


class LibraryForbiddenError(PermissionError):
    """The caller cannot write the agent or the scope."""


class LibraryConflictError(ValueError):
    """The write conflicts with another row (a name that exists, an agent in use)."""


class LibrarySpec(BaseModel):
    """The fields of a library agent that a person edits."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(max_length=SUBAGENT_NAME_MAX_CHARS, pattern=SUBAGENT_NAME_PATTERN)
    description: str = Field(min_length=1, max_length=SUBAGENT_DESCRIPTION_MAX_CHARS)
    prompt: str = Field(min_length=1, max_length=SUBAGENT_PROMPT_MAX_CHARS)
    toolsets: tuple[str, ...] = ()
    model: str | None = Field(default=None, min_length=1, max_length=100)
    """``None`` or ``inherit``: the model of the run."""
    max_turns: int | None = Field(default=None, gt=0, le=SUBAGENT_MAX_TURNS)
    max_cost_usd: Decimal | None = Field(
        default=None, gt=0, le=MAX_COST_USD, max_digits=8, decimal_places=4
    )

    @model_validator(mode="after")
    def _toolsets_are_unique(self) -> Self:
        if len(set(self.toolsets)) != len(self.toolsets):
            raise ValueError("a toolset is in the list two times")
        return self

    def value(self) -> dict[str, Any]:
        """The JSON value of the spec, for a version row."""
        return {
            "name": self.name,
            "description": self.description,
            "prompt": self.prompt,
            "toolsets": list(self.toolsets),
            "model": self.model,
            "max_turns": self.max_turns,
            "max_cost_usd": None if self.max_cost_usd is None else _plain(self.max_cost_usd),
        }


def _plain(value: Decimal) -> str:
    """A decimal without trailing zeros and without an exponent, for example ``0.4``."""
    return format(value.normalize(), "f")


def parse_spec(raw: Mapping[str, Any]) -> LibrarySpec:
    """The spec from JSON. Raises ``LibraryError`` with the names of the fields only."""
    try:
        spec = LibrarySpec.model_validate(dict(raw))
    except ValidationError as error:
        fields = sorted(
            {
                str(e["loc"][0]) if e["loc"] and str(e["loc"][0]) in SPEC_FIELDS else "value"
                for e in error.errors(include_input=False)
            }
        )
        raise LibraryError("these fields are not valid: " + ", ".join(fields)) from None
    if spec.model == INHERIT:
        spec = spec.model_copy(update={"model": None})
    return spec


def library_toolsets() -> list[str]:
    """The toolsets that a library agent can have, sorted."""
    return sorted(name for name in TOOLSETS if name not in NOT_FOR_SUBAGENTS)


def spec_problems(spec: LibrarySpec) -> list[str]:
    """Why the spec cannot be stored. An empty list means that it can."""
    # Here, not at the top: the prompt package imports ``sama.subagents``.
    from sama.prompt.scan import scan

    found: list[str] = []
    allowed = set(library_toolsets())
    for name in spec.toolsets:
        if name in NOT_FOR_SUBAGENTS:
            found.append(f"a sub-agent cannot have the toolset {name}")
        elif name not in allowed:
            found.append(f"the toolset {name} is not known")
    if spec.model is not None and not any(
        model_is_known(runtime, spec.model) for runtime in LIBRARY_MODEL_RUNTIMES
    ):
        found.append("the model has no price in the price table")
    for field, text in (("description", spec.description), ("prompt", spec.prompt)):
        findings = list(scan(text))
        if redact(text) != text:
            findings.append("secret")
        if findings:
            found.append(f"the {field} has {', '.join(findings)}")
    return found


def check_spec(spec: LibrarySpec) -> None:
    found = spec_problems(spec)
    if found:
        raise LibraryError("; ".join(found))


def library_options() -> dict[str, Any]:
    """What an editor of a library agent can choose, and the limits."""
    return {
        "toolsets": [
            {"name": name, "tools": sorted(TOOLSETS[name])} for name in library_toolsets()
        ],
        "models": [
            {"runtime": runtime.value, "models": known_models(runtime)}
            for runtime in LIBRARY_MODEL_RUNTIMES
        ],
        "limits": {
            "name_max_chars": SUBAGENT_NAME_MAX_CHARS,
            "name_pattern": SUBAGENT_NAME_PATTERN,
            "description_max_chars": SUBAGENT_DESCRIPTION_MAX_CHARS,
            "prompt_max_chars": SUBAGENT_PROMPT_MAX_CHARS,
            "max_turns": SUBAGENT_MAX_TURNS,
            "max_cost_usd": str(MAX_COST_USD),
        },
    }


# ---------------------------------------------------------------------------------------------
# Rows, visibility, and rights
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Agent:
    """One library agent, as stored."""

    id: uuid.UUID
    org_id: uuid.UUID
    scope: str
    workspace_id: uuid.UUID | None
    user_id: uuid.UUID | None
    name: str
    description: str
    prompt: str
    toolsets: tuple[str, ...]
    model: str | None
    max_turns: int | None
    max_cost_usd: Decimal | None
    version: int
    updated_at: Any

    @classmethod
    def of(cls, row: Any) -> Agent:
        return cls(
            id=row.id,
            org_id=row.org_id,
            scope=row.scope,
            workspace_id=row.workspace_id,
            user_id=row.user_id,
            name=row.name,
            description=row.description,
            prompt=row.prompt,
            toolsets=tuple(str(t) for t in row.toolsets or []),
            model=row.model,
            max_turns=row.max_turns,
            max_cost_usd=row.max_cost_usd,
            version=row.version,
            updated_at=row.updated_at,
        )

    def spec(self) -> LibrarySpec:
        return LibrarySpec(
            name=self.name,
            description=self.description,
            prompt=self.prompt,
            toolsets=self.toolsets,
            model=self.model,
            max_turns=self.max_turns,
            max_cost_usd=self.max_cost_usd,
        )

    def native_spec(self, runtime: RuntimeKind) -> SubagentSpec:
        """The agent as a native sub-agent spec for a run of ``runtime``. A model that the
        runtime does not know becomes the model of the run."""
        model = self.model
        if model is not None and not model_is_known(runtime, model):
            log.warning(
                "a library agent has a model of another runtime agent=%s runtime=%s",
                self.id,
                runtime.value,
            )
            model = None
        return SubagentSpec(
            name=self.name,
            description=self.description,
            prompt=self.prompt,
            toolsets=self.toolsets,
            model=model,
            max_turns=self.max_turns,
        )

    def frozen(self) -> dict[str, Any]:
        """The value of ``sessions.library_agent``: the spec of this version, frozen."""
        return {
            "id": str(self.id),
            "scope": self.scope,
            "version": self.version,
            **self.spec().value(),
        }

    def applies(
        self, *, workspace_id: uuid.UUID, audience: str, owner_user_id: uuid.UUID | None
    ) -> bool:
        """True if a session of this workspace and audience can use the agent. A workspace
        agent is for its workspace. A personal agent is for a private session of its owner."""
        if self.scope == "org":
            return True
        if self.scope == "workspace":
            return self.workspace_id == workspace_id
        return audience == "private" and owner_user_id is not None and owner_user_id == self.user_id


_COLUMNS: Final = (
    LibraryAgent.id,
    LibraryAgent.org_id,
    LibraryAgent.scope,
    LibraryAgent.workspace_id,
    LibraryAgent.user_id,
    LibraryAgent.name,
    LibraryAgent.description,
    LibraryAgent.prompt,
    LibraryAgent.toolsets,
    LibraryAgent.model,
    LibraryAgent.max_turns,
    LibraryAgent.max_cost_usd,
    LibraryAgent.version,
    LibraryAgent.updated_at,
)


def can_read(agent: Agent, grants: Grants) -> bool:
    """The rule of the RLS policy ``library_agents_select``."""
    if not grants.org_member:
        return False
    if agent.scope == "org":
        return True
    if agent.scope == "workspace":
        return agent.workspace_id is not None and grants.sees_workspace(agent.workspace_id)
    return agent.user_id == grants.user_id


def can_write(
    scope: str, workspace_id: uuid.UUID | None, user_id: uuid.UUID | None, grants: Grants
) -> bool:
    """The owner writes a personal agent, a workspace admin or an org owner or admin writes a
    workspace agent, and an org owner or admin writes an org agent."""
    if not grants.org_member:
        return False
    if scope == "user":
        return user_id == grants.user_id
    if scope == "workspace":
        return workspace_id is not None and (
            grants.org_admin or grants.admins_workspace(workspace_id)
        )
    return scope == "org" and grants.org_admin


def writable_scopes(grants: Grants, workspace_id: uuid.UUID | None) -> list[Scope]:
    out: list[Scope] = []
    if can_write("user", None, grants.user_id, grants):
        out.append("user")
    if workspace_id is not None and can_write("workspace", workspace_id, None, grants):
        out.append("workspace")
    if can_write("org", None, None, grants):
        out.append("org")
    return out


async def load_agents(
    connection: AsyncConnection, org_id: uuid.UUID, ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, Agent]:
    wanted = list(dict.fromkeys(ids))
    if not wanted:
        return {}
    rows = await connection.execute(
        select(*_COLUMNS).where(LibraryAgent.org_id == org_id, LibraryAgent.id.in_(wanted))
    )
    return {row.id: Agent.of(row) for row in rows}


def agent_ids(raw: Any) -> list[uuid.UUID]:
    """The ids of the JSON value ``agent_profiles.library_subagents``. A bad item is left out."""
    if not isinstance(raw, list):
        return []
    out: list[uuid.UUID] = []
    for item in raw:
        try:
            value = uuid.UUID(str(item))
        except ValueError:
            continue
        if value not in out:
            out.append(value)
    return out


def parse_ids(raw: Any) -> list[str]:
    """The checked value of ``library_subagents`` in a change of a profile. Raises
    ``LibraryError``."""
    if not isinstance(raw, list):
        raise LibraryError("the library sub-agents must be a list")
    if len(raw) > MAX_SUBAGENTS:
        raise LibraryError(f"a profile has at most {MAX_SUBAGENTS} sub-agents")
    out: list[str] = []
    for item in raw:
        try:
            value = str(uuid.UUID(str(item)))
        except ValueError:
            raise LibraryError("a library sub-agent id is not valid") from None
        if value in out:
            raise LibraryError("a library sub-agent is in the list two times")
        out.append(value)
    return out


async def find_by_name(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    name: str,
    *,
    workspace_id: uuid.UUID,
    audience: str,
    owner_user_id: uuid.UUID | None,
) -> Agent | None:
    """The library agent with this name that a session can use: the personal agent of the
    owner of a private session first, then the workspace agent, then the org agent."""
    rows = await connection.execute(
        select(*_COLUMNS).where(LibraryAgent.org_id == org_id, LibraryAgent.name == name)
    )
    found = [
        agent
        for agent in (Agent.of(row) for row in rows)
        if agent.applies(workspace_id=workspace_id, audience=audience, owner_user_id=owner_user_id)
    ]
    found.sort(key=lambda agent: SCOPES.index(agent.scope))
    return found[0] if found else None


async def session_agents(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    raw: Any,
    *,
    workspace_id: uuid.UUID,
    audience: str,
    owner_user_id: uuid.UUID | None,
) -> list[Agent]:
    """The library agents of a profile that a session can use, in the order of the profile. A
    missing agent or an agent of another workspace or user is left out with a log line."""
    ids = agent_ids(raw)
    found = await load_agents(connection, org_id, ids)
    out: list[Agent] = []
    for agent_id in ids:
        agent = found.get(agent_id)
        if agent is None:
            log.warning("a profile names a library agent that does not exist agent=%s", agent_id)
        elif agent.applies(
            workspace_id=workspace_id, audience=audience, owner_user_id=owner_user_id
        ):
            out.append(agent)
    return out


async def usable_agents(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID,
    audience: str,
    owner_user_id: uuid.UUID | None,
) -> list[Agent]:
    """The library agents that a session can start with ``delegate_task(agent=...)``, one for
    each name (the narrowest scope wins), by name."""
    rows = await connection.execute(
        select(*_COLUMNS)
        .where(LibraryAgent.org_id == org_id)
        .order_by(LibraryAgent.name, LibraryAgent.id)
    )
    best: dict[str, Agent] = {}
    for agent in (Agent.of(row) for row in rows):
        if not agent.applies(
            workspace_id=workspace_id, audience=audience, owner_user_id=owner_user_id
        ):
            continue
        current = best.get(agent.name)
        if current is None or SCOPES.index(agent.scope) < SCOPES.index(current.scope):
            best[agent.name] = agent
    return [best[name] for name in sorted(best)]


async def personal_agents(
    connection: AsyncConnection, org_id: uuid.UUID, user_id: uuid.UUID
) -> list[Agent]:
    """The personal agents of a user, by name."""
    rows = await connection.execute(
        select(*_COLUMNS)
        .where(
            LibraryAgent.org_id == org_id,
            LibraryAgent.scope == "user",
            LibraryAgent.user_id == user_id,
        )
        .order_by(LibraryAgent.name, LibraryAgent.id)
    )
    return [Agent.of(row) for row in rows]


async def check_profile_library(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    *,
    profile_workspace_id: uuid.UUID | None,
    before: Sequence[str],
    after: Sequence[str],
    inline_names: Sequence[str],
    toolsets: Sequence[str],
    runtime: RuntimeKind,
    actor_user_id: uuid.UUID | None,
) -> None:
    """Checks the library agents of a profile after a change (Section 13.3).

    ``before`` and ``after`` are the ids of now and after the change. Each agent must exist in
    the org. A workspace agent must be of the workspace of the profile (any workspace for a
    profile of the org). A personal agent that the change adds must be of the actor. The inline
    and the library sub-agents have unique names, and together at most ``MAX_SUBAGENTS``. A
    profile with library agents needs the toolset ``subagents`` and a runtime with native
    sub-agents. The toolsets of an agent are not checked: the build uses only the toolsets that
    the run has. Raises ``LibraryError``.
    """
    if not after:
        return
    if not supports_subagents(runtime):
        raise LibraryError(f"the runtime {runtime.value} has no native sub-agents")
    if TOOLSET not in toolsets:
        raise LibraryError(f"the profile needs the toolset {TOOLSET}")
    if len(after) + len(inline_names) > MAX_SUBAGENTS:
        raise LibraryError(f"a profile has at most {MAX_SUBAGENTS} sub-agents")
    ids = [uuid.UUID(value) for value in after]
    # A share lock on each agent until the end of the transaction: a delete of the agent takes
    # an update lock first, so a delete and a new reference cannot both commit.
    rows = await connection.execute(
        select(*_COLUMNS)
        .where(LibraryAgent.org_id == org_id, LibraryAgent.id.in_(ids))
        .order_by(LibraryAgent.id)
        .with_for_update(read=True)
    )
    found = {row.id: Agent.of(row) for row in rows}
    added = set(after) - set(before)
    names = list(inline_names)
    for agent_id in ids:
        agent = found.get(agent_id)
        if agent is None:
            raise LibraryError("a library sub-agent does not exist")
        if (
            agent.scope == "workspace"
            and profile_workspace_id is not None
            and agent.workspace_id != profile_workspace_id
        ):
            raise LibraryError(f"the sub-agent {agent.name} is of another workspace")
        if agent.scope == "user" and str(agent_id) in added and agent.user_id != actor_user_id:
            raise LibraryError(f"the sub-agent {agent.name} is a personal agent of another user")
        names.append(agent.name)
    if len(set(names)) != len(names):
        raise LibraryError("two sub-agents have the same name")


# ---------------------------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Version:
    version: int
    source: str
    reverts_version: int | None
    author_user_id: uuid.UUID | None
    created_at: Any
    spec: dict[str, Any]


class SubagentLibrary:
    """Reads and writes the library. The caller gives the grants of the person. The reads
    check the same rules as the RLS policy. The writes lock the row and the memberships, so a
    change of a role cannot slip in before the write."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def list_agents(
        self, org_id: uuid.UUID, grants: Grants, *, workspace_id: uuid.UUID | None = None
    ) -> list[Agent]:
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                select(*_COLUMNS)
                .where(LibraryAgent.org_id == org_id)
                .order_by(LibraryAgent.name, LibraryAgent.id)
            )
            agents = [Agent.of(row) for row in rows]
        visible = [
            agent
            for agent in agents
            if can_read(agent, grants)
            and (
                workspace_id is None
                or agent.scope != "workspace"
                or agent.workspace_id == workspace_id
            )
        ]
        visible.sort(key=lambda agent: (SCOPES.index(agent.scope), agent.name))
        return visible

    async def get(self, org_id: uuid.UUID, grants: Grants, agent_id: uuid.UUID) -> Agent:
        async with self._engine.connect() as connection:
            found = (await load_agents(connection, org_id, [agent_id])).get(agent_id)
        if found is None or not can_read(found, grants):
            raise LibraryNotFoundError("the sub-agent does not exist")
        return found

    async def used_by(
        self, org_id: uuid.UUID, agent_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[dict[str, str]]]:
        """The agent profiles that name each agent."""
        out: dict[uuid.UUID, list[dict[str, str]]] = {agent_id: [] for agent_id in agent_ids}
        if not agent_ids:
            return out
        wanted = {str(agent_id) for agent_id in agent_ids}
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                select(AgentProfile.id, AgentProfile.name, AgentProfile.library_subagents)
                .where(
                    AgentProfile.org_id == org_id,
                    func.jsonb_array_length(AgentProfile.library_subagents) > 0,
                )
                .order_by(AgentProfile.name, AgentProfile.id)
            )
            for row in rows:
                for value in row.library_subagents or []:
                    if str(value) in wanted:
                        out[uuid.UUID(str(value))].append({"id": str(row.id), "name": row.name})
        return out

    async def create(
        self,
        org_id: uuid.UUID,
        grants: Grants,
        *,
        scope: Scope,
        workspace_id: uuid.UUID | None,
        spec: LibrarySpec,
    ) -> uuid.UUID:
        check_spec(spec)
        user_id = grants.user_id if scope == "user" else None
        workspace = workspace_id if scope == "workspace" else None
        if scope == "workspace" and workspace is None:
            raise LibraryError("a workspace agent needs a workspace")
        async with self._engine.begin() as connection:
            await self._lock_rights(connection, org_id, grants, scope, workspace, user_id)
            await self._check_free(connection, org_id, scope, workspace, user_id, spec.name, None)
            agent_id = (
                await connection.execute(
                    insert(LibraryAgent)
                    .values(
                        org_id=org_id,
                        scope=scope,
                        workspace_id=workspace,
                        user_id=user_id,
                        **_columns(spec),
                        version=1,
                        created_by_user_id=grants.user_id,
                        updated_by_user_id=grants.user_id,
                    )
                    .returning(LibraryAgent.id)
                )
            ).scalar_one()
            await self._version(connection, org_id, agent_id, 1, spec, "create", grants, None)
            await self._audit(
                connection, org_id, workspace, agent_id, "created", grants, spec.name, 1
            )
        return agent_id

    async def update(
        self,
        org_id: uuid.UUID,
        grants: Grants,
        agent_id: uuid.UUID,
        changes: Mapping[str, Any],
    ) -> Agent:
        """Changes the given fields. Without a change, it writes nothing."""
        unknown = set(changes) - set(SPEC_FIELDS)
        if unknown:
            raise LibraryError("unknown field")
        async with self._engine.begin() as connection:
            agent = await self._lock(connection, org_id, grants, agent_id)
            current = agent.spec().value()
            merged = {**current, **dict(changes)}
            spec = parse_spec(merged)
            if spec.value() == current:
                return agent
            check_spec(spec)
            if spec.name != agent.name:
                await self._check_free(
                    connection,
                    org_id,
                    agent.scope,
                    agent.workspace_id,
                    agent.user_id,
                    spec.name,
                    agent.id,
                )
            version = agent.version + 1
            await self._write(connection, org_id, agent, spec, version, grants)
            await self._version(connection, org_id, agent.id, version, spec, "edit", grants, None)
            await self._audit(
                connection,
                org_id,
                agent.workspace_id,
                agent.id,
                "updated",
                grants,
                spec.name,
                version,
            )
        return await self.get(org_id, grants, agent_id)

    async def revert(
        self, org_id: uuid.UUID, grants: Grants, agent_id: uuid.UUID, version: int
    ) -> Agent:
        """Makes a new version with the spec of ``version``."""
        async with self._engine.begin() as connection:
            agent = await self._lock(connection, org_id, grants, agent_id)
            raw = (
                await connection.execute(
                    select(LibraryAgentVersion.spec).where(
                        LibraryAgentVersion.org_id == org_id,
                        LibraryAgentVersion.agent_id == agent_id,
                        LibraryAgentVersion.version == version,
                    )
                )
            ).scalar_one_or_none()
            if raw is None:
                raise LibraryNotFoundError("the version does not exist")
            spec = parse_spec(raw)
            # The text is checked again: the injection scan and the known secrets can change.
            check_spec(spec)
            if spec.name != agent.name:
                await self._check_free(
                    connection,
                    org_id,
                    agent.scope,
                    agent.workspace_id,
                    agent.user_id,
                    spec.name,
                    agent.id,
                )
            new = agent.version + 1
            await self._write(connection, org_id, agent, spec, new, grants)
            await self._version(connection, org_id, agent.id, new, spec, "revert", grants, version)
            await self._audit(
                connection, org_id, agent.workspace_id, agent.id, "reverted", grants, spec.name, new
            )
        return await self.get(org_id, grants, agent_id)

    async def delete(self, org_id: uuid.UUID, grants: Grants, agent_id: uuid.UUID) -> None:
        """Deletes the agent and its versions. Raises ``LibraryConflictError`` when an agent
        profile uses it."""
        async with self._engine.begin() as connection:
            agent = await self._lock(connection, org_id, grants, agent_id)
            # The update lock of the agent row (``_lock``) waits for each profile write that
            # names the agent: that write holds a share lock on the row until it commits
            # (``check_profile_library``). So this read sees every committed reference, and a
            # later write that adds a reference finds no agent.
            users = (
                await connection.execute(
                    select(AgentProfile.name)
                    .where(
                        AgentProfile.org_id == org_id,
                        AgentProfile.library_subagents.contains([str(agent_id)]),
                    )
                    .order_by(AgentProfile.name)
                )
            ).scalars()
            names = list(users)
            if names:
                raise LibraryConflictError(
                    "remove the sub-agent from these agent profiles first: " + ", ".join(names[:10])
                )
            await connection.execute(
                delete(LibraryAgent).where(
                    LibraryAgent.org_id == org_id, LibraryAgent.id == agent_id
                )
            )
            await self._audit(
                connection,
                org_id,
                agent.workspace_id,
                agent.id,
                "deleted",
                grants,
                agent.name,
                None,
            )

    async def versions(
        self, org_id: uuid.UUID, grants: Grants, agent_id: uuid.UUID
    ) -> list[Version]:
        await self.get(org_id, grants, agent_id)
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                select(
                    LibraryAgentVersion.version,
                    LibraryAgentVersion.source,
                    LibraryAgentVersion.reverts_version,
                    LibraryAgentVersion.author_user_id,
                    LibraryAgentVersion.created_at,
                    LibraryAgentVersion.spec,
                )
                .where(
                    LibraryAgentVersion.org_id == org_id,
                    LibraryAgentVersion.agent_id == agent_id,
                )
                .order_by(LibraryAgentVersion.version.desc())
            )
            return [
                Version(
                    version=row.version,
                    source=row.source,
                    reverts_version=row.reverts_version,
                    author_user_id=row.author_user_id,
                    created_at=row.created_at,
                    spec=dict(row.spec),
                )
                for row in rows
            ]

    # -- helpers -----------------------------------------------------------------------------

    async def _lock_rights(
        self,
        connection: AsyncConnection,
        org_id: uuid.UUID,
        grants: Grants,
        scope: str,
        workspace_id: uuid.UUID | None,
        user_id: uuid.UUID | None,
    ) -> None:
        # Here, not at the top: ``sama.tenancy.access`` is imported by many modules.
        from sama.tenancy.access import lock_grants

        now = await lock_grants(connection, org_id, grants.user_id)
        if not can_write(scope, workspace_id, user_id, now):
            raise LibraryForbiddenError("you cannot write a sub-agent of this scope")

    async def _lock(
        self, connection: AsyncConnection, org_id: uuid.UUID, grants: Grants, agent_id: uuid.UUID
    ) -> Agent:
        row = (
            await connection.execute(
                select(*_COLUMNS)
                .where(LibraryAgent.org_id == org_id, LibraryAgent.id == agent_id)
                .with_for_update()
            )
        ).one_or_none()
        agent = None if row is None else Agent.of(row)
        if agent is None or not can_read(agent, grants):
            raise LibraryNotFoundError("the sub-agent does not exist")
        await self._lock_rights(
            connection, org_id, grants, agent.scope, agent.workspace_id, agent.user_id
        )
        return agent

    @staticmethod
    async def _check_free(
        connection: AsyncConnection,
        org_id: uuid.UUID,
        scope: str,
        workspace_id: uuid.UUID | None,
        user_id: uuid.UUID | None,
        name: str,
        agent_id: uuid.UUID | None,
    ) -> None:
        query = select(LibraryAgent.id).where(
            LibraryAgent.org_id == org_id,
            LibraryAgent.scope == scope,
            LibraryAgent.workspace_id.is_not_distinct_from(workspace_id),
            LibraryAgent.user_id.is_not_distinct_from(user_id),
            LibraryAgent.name == name,
        )
        if agent_id is not None:
            query = query.where(LibraryAgent.id != agent_id)
        if (await connection.execute(query)).first() is not None:
            raise LibraryConflictError(f"a sub-agent with the name {name} exists in this scope")

    @staticmethod
    async def _write(
        connection: AsyncConnection,
        org_id: uuid.UUID,
        agent: Agent,
        spec: LibrarySpec,
        version: int,
        grants: Grants,
    ) -> None:
        await connection.execute(
            update(LibraryAgent)
            .where(LibraryAgent.org_id == org_id, LibraryAgent.id == agent.id)
            .values(
                **_columns(spec),
                version=version,
                updated_by_user_id=grants.user_id,
                updated_at=func.now(),
            )
        )

    @staticmethod
    async def _version(
        connection: AsyncConnection,
        org_id: uuid.UUID,
        agent_id: uuid.UUID,
        version: int,
        spec: LibrarySpec,
        source: str,
        grants: Grants,
        reverts: int | None,
    ) -> None:
        await connection.execute(
            insert(LibraryAgentVersion).values(
                org_id=org_id,
                agent_id=agent_id,
                version=version,
                spec=spec.value(),
                source=source,
                reverts_version=reverts,
                author_user_id=grants.user_id,
            )
        )

    @staticmethod
    async def _audit(
        connection: AsyncConnection,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID | None,
        agent_id: uuid.UUID,
        action: str,
        grants: Grants,
        name: str,
        version: int | None,
    ) -> None:
        details: dict[str, Any] = {"name": name}
        if version is not None:
            details["version"] = version
        await connection.execute(
            insert(AuditEntry).values(
                org_id=org_id,
                workspace_id=workspace_id,
                actor_kind="user",
                actor_user_id=grants.user_id,
                action=f"subagent_library.{action}",
                target_type="library_agent",
                target_id=str(agent_id),
                details=details,
            )
        )


def _columns(spec: LibrarySpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "description": spec.description,
        "prompt": spec.prompt,
        "toolsets": list(spec.toolsets),
        "model": spec.model,
        "max_turns": spec.max_turns,
        "max_cost_usd": spec.max_cost_usd,
    }
