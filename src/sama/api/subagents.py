"""The sub-agent library routes of the web API (docs/architecture.md, Sections 11.4 and 13.3).

A member lists the library agents that the member can see, and writes them with the rules of
``sama.subagent_library``: a personal agent is the own agent of the member, a workspace agent
needs a workspace admin or an org owner or admin, and an org agent needs an org owner or admin. Each
write is a version that can be reverted. A test run starts a private session of the member that
runs as the agent.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Annotated, Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from sama.runtime.contract import (
    SUBAGENT_DESCRIPTION_MAX_CHARS,
    SUBAGENT_MAX_TURNS,
    SUBAGENT_NAME_MAX_CHARS,
    SUBAGENT_PROMPT_MAX_CHARS,
)
from sama.subagent_library import (
    MAX_COST_USD,
    Agent,
    LibraryConflictError,
    LibraryError,
    LibraryForbiddenError,
    LibraryNotFoundError,
    Scope,
    SubagentLibrary,
    Version,
    can_write,
    library_options,
    parse_spec,
    writable_scopes,
)
from sama.tenancy.access import Grants

MAX_TEST_MESSAGE_CHARS = 4_000


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


_Name = Annotated[str, Field(min_length=1, max_length=SUBAGENT_NAME_MAX_CHARS)]
_Description = Annotated[str, Field(min_length=1, max_length=SUBAGENT_DESCRIPTION_MAX_CHARS)]
_Prompt = Annotated[str, Field(min_length=1, max_length=SUBAGENT_PROMPT_MAX_CHARS)]
_Toolsets = Annotated[list[Annotated[str, Field(max_length=40)]], Field(max_length=40)]
_Model = Annotated[str, Field(min_length=1, max_length=100)]
_Turns = Annotated[int, Field(ge=1, le=SUBAGENT_MAX_TURNS)]
_Cost = Annotated[Decimal, Field(gt=0, le=MAX_COST_USD, max_digits=8, decimal_places=4)]


class LibraryAgentIn(_Body):
    scope: Scope
    workspace_id: uuid.UUID | None = None
    """The workspace of a workspace agent."""
    name: _Name
    description: _Description
    prompt: _Prompt
    toolsets: _Toolsets = Field(default_factory=list)
    model: _Model | None = None
    max_turns: _Turns | None = None
    max_cost_usd: _Cost | None = None


class LibraryAgentChange(_Body):
    """A field that is not in the body does not change. ``null`` clears ``model``,
    ``max_turns``, and ``max_cost_usd``."""

    name: _Name | None = None
    description: _Description | None = None
    prompt: _Prompt | None = None
    toolsets: _Toolsets | None = None
    model: _Model | None = None
    max_turns: _Turns | None = None
    max_cost_usd: _Cost | None = None


class LibraryTestIn(_Body):
    workspace_id: uuid.UUID
    profile_id: uuid.UUID
    message: str = Field(min_length=1, max_length=MAX_TEST_MESSAGE_CHARS)


def agent_view(agent: Agent, grants: Grants, used_by: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "id": str(agent.id),
        "scope": agent.scope,
        "workspace_id": None if agent.workspace_id is None else str(agent.workspace_id),
        "user_id": None if agent.user_id is None else str(agent.user_id),
        **agent.spec().value(),
        "version": agent.version,
        "updated_at": agent.updated_at.isoformat(),
        "can_edit": can_write(agent.scope, agent.workspace_id, agent.user_id, grants),
        "used_by": used_by,
    }


def version_view(version: Version) -> dict[str, Any]:
    return {
        "version": version.version,
        "source": version.source,
        "reverts_version": version.reverts_version,
        "author_user_id": None if version.author_user_id is None else str(version.author_user_id),
        "created_at": version.created_at.isoformat(),
        "spec": version.spec,
    }


def library_http_error(error: Exception) -> HTTPException:
    """The HTTP error of a library error. The texts are fixed or have only names."""
    if isinstance(error, LibraryNotFoundError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, LibraryForbiddenError):
        return HTTPException(status_code=403, detail=str(error))
    if isinstance(error, LibraryConflictError):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=422, detail=str(error))


LIBRARY_ERRORS = (LibraryError, LibraryNotFoundError, LibraryForbiddenError, LibraryConflictError)


ManageCheck = Callable[[uuid.UUID, Grants], Awaitable[bool]]
"""True when an org owner or admin has the permission ``agents.manage`` (M7.10)."""
NO_MANAGE = "Your role in this org cannot do this: Create and change agents and their tool policy."


class LibraryRoutes:
    """The handlers of the library routes. The caller checks the membership first and gives
    the grants. A write of a workspace or org agent by an org owner or admin also needs the
    permission ``agents.manage`` of the role (M7.10). A personal agent needs no permission."""

    def __init__(self, library: SubagentLibrary, manage: ManageCheck | None = None) -> None:
        self.library = library
        self._manage = manage

    async def _may_manage(self, org_id: uuid.UUID, grants: Grants, scope: str) -> bool:
        if scope == "user" or self._manage is None or not grants.org_admin:
            return True
        return await self._manage(org_id, grants)

    async def _require_manage(self, org_id: uuid.UUID, grants: Grants, scope: str) -> None:
        if not await self._may_manage(org_id, grants, scope):
            raise HTTPException(status_code=403, detail=NO_MANAGE)

    async def _scope_of(self, org_id: uuid.UUID, grants: Grants, agent_id: uuid.UUID) -> str:
        try:
            return (await self.library.get(org_id, grants, agent_id)).scope
        except LIBRARY_ERRORS as error:
            raise library_http_error(error) from None

    async def view(self, org_id: uuid.UUID, grants: Grants, agent: Agent) -> dict[str, Any]:
        used = await self.library.used_by(org_id, [agent.id])
        return agent_view(agent, grants, used[agent.id])

    async def catalog(
        self, org_id: uuid.UUID, grants: Grants, workspace_id: uuid.UUID | None
    ) -> dict[str, Any]:
        if workspace_id is not None and not grants.sees_workspace(workspace_id):
            raise HTTPException(status_code=404, detail="the workspace does not exist")
        agents = await self.library.list_agents(org_id, grants, workspace_id=workspace_id)
        used = await self.library.used_by(org_id, [agent.id for agent in agents])
        scopes = [
            scope
            for scope in writable_scopes(grants, workspace_id)
            if await self._may_manage(org_id, grants, scope)
        ]
        return {
            "agents": [agent_view(agent, grants, used[agent.id]) for agent in agents],
            "writable_scopes": scopes,
            "options": library_options(),
        }

    async def get(self, org_id: uuid.UUID, grants: Grants, agent_id: uuid.UUID) -> dict[str, Any]:
        try:
            agent = await self.library.get(org_id, grants, agent_id)
        except LIBRARY_ERRORS as error:
            raise library_http_error(error) from None
        return await self.view(org_id, grants, agent)

    async def create(
        self, org_id: uuid.UUID, grants: Grants, body: LibraryAgentIn
    ) -> dict[str, Any]:
        if body.scope == "workspace" and (
            body.workspace_id is None or not grants.sees_workspace(body.workspace_id)
        ):
            raise HTTPException(status_code=422, detail="choose a workspace that you can see")
        await self._require_manage(org_id, grants, body.scope)
        data = body.model_dump(exclude={"scope", "workspace_id"})
        try:
            spec = parse_spec(data)
            agent_id = await self.library.create(
                org_id, grants, scope=body.scope, workspace_id=body.workspace_id, spec=spec
            )
            agent = await self.library.get(org_id, grants, agent_id)
        except LIBRARY_ERRORS as error:
            raise library_http_error(error) from None
        return await self.view(org_id, grants, agent)

    async def update(
        self, org_id: uuid.UUID, grants: Grants, agent_id: uuid.UUID, body: LibraryAgentChange
    ) -> dict[str, Any]:
        changes = body.model_dump(exclude_unset=True)
        for field in ("name", "description", "prompt", "toolsets"):
            if field in changes and changes[field] is None:
                raise HTTPException(status_code=422, detail=f"{field} cannot be null")
        if changes.get("max_cost_usd") is not None:
            changes["max_cost_usd"] = str(changes["max_cost_usd"])
        await self._require_manage(org_id, grants, await self._scope_of(org_id, grants, agent_id))
        try:
            agent = await self.library.update(org_id, grants, agent_id, changes)
        except LIBRARY_ERRORS as error:
            raise library_http_error(error) from None
        return await self.view(org_id, grants, agent)

    async def delete(self, org_id: uuid.UUID, grants: Grants, agent_id: uuid.UUID) -> None:
        await self._require_manage(org_id, grants, await self._scope_of(org_id, grants, agent_id))
        try:
            await self.library.delete(org_id, grants, agent_id)
        except LIBRARY_ERRORS as error:
            raise library_http_error(error) from None

    async def versions(
        self, org_id: uuid.UUID, grants: Grants, agent_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        try:
            found = await self.library.versions(org_id, grants, agent_id)
        except LIBRARY_ERRORS as error:
            raise library_http_error(error) from None
        return [version_view(version) for version in found]

    async def revert(
        self, org_id: uuid.UUID, grants: Grants, agent_id: uuid.UUID, version: int
    ) -> dict[str, Any]:
        await self._require_manage(org_id, grants, await self._scope_of(org_id, grants, agent_id))
        try:
            agent = await self.library.revert(org_id, grants, agent_id, version)
        except LIBRARY_ERRORS as error:
            raise library_http_error(error) from None
        return await self.view(org_id, grants, agent)
