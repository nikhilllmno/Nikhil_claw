"""The routes of the custom tools (docs/architecture.md, Section 10.8; roadmap M7.3).

The routes need a sign-in. ``sama.customtools.CustomToolService`` checks the scope rules; these
routes find the org and the grants of the user, and map the errors to HTTP answers. No answer
holds a credential.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from sama.api.auth import Principal
from sama.customtools.service import CustomToolError, CustomToolService
from sama.customtools.spec import (
    MAX_DESCRIPTION_CHARS,
    MAX_SUMMARY_CHARS,
    Definition,
    HttpConfig,
    Kind,
    Policy,
    ScriptConfig,
)
from sama.tenancy.access import Grants

MAX_SECRET_CHARS = 4_000

WorkspaceGrants = Callable[[Principal, uuid.UUID], Awaitable[tuple[uuid.UUID, Grants]]]
ToolGrants = Callable[[Principal, uuid.UUID], Awaitable[tuple[uuid.UUID, Grants]]]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CredentialIn(_Body):
    """The secret of an HTTP tool. ``value`` for ``bearer`` and ``api_key``, ``username`` and
    ``password`` for ``basic``."""

    value: SecretStr | None = Field(default=None, max_length=MAX_SECRET_CHARS)
    username: SecretStr | None = Field(default=None, max_length=MAX_SECRET_CHARS)
    password: SecretStr | None = Field(default=None, max_length=MAX_SECRET_CHARS)

    def values(self) -> dict[str, str]:
        return {
            key: secret.get_secret_value()
            for key, secret in (
                ("value", self.value),
                ("username", self.username),
                ("password", self.password),
            )
            if secret is not None
        }


class DefinitionIn(_Body):
    kind: Kind
    description: Annotated[str, Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)]
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    policy: Policy = "ask"
    http: HttpConfig | None = None
    script: ScriptConfig | None = None
    summary: Annotated[str, Field(max_length=MAX_SUMMARY_CHARS)] = ""

    def definition(self) -> Definition:
        try:
            return Definition.model_validate(
                self.model_dump(
                    include={"kind", "description", "input_schema", "policy", "http", "script"}
                )
            )
        except ValidationError as error:
            reason = "; ".join(str(e.get("msg", "")) for e in error.errors())
            raise HTTPException(status_code=422, detail=reason[:500]) from None


class ToolIn(DefinitionIn):
    scope: Literal["user", "workspace", "org"] = "user"
    name: Annotated[str, Field(min_length=2, max_length=40)]
    credential: CredentialIn | None = None


class RevertIn(_Body):
    version: int = Field(ge=1)


class TestIn(_Body):
    arguments: dict[str, Any] = Field(default_factory=dict)
    version: int | None = Field(default=None, ge=1)


async def _call[T](call: Awaitable[T]) -> T:
    try:
        return await call
    except CustomToolError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from None


def router(
    service: Callable[[], CustomToolService],
    principal: Callable[[Request], Awaitable[Principal]],
    workspace_grants: WorkspaceGrants,
    tool_grants: ToolGrants,
) -> APIRouter:
    """``service`` gives the service or raises ``409`` when custom tools are off."""
    routes = APIRouter()
    # This module has no ``from __future__ import annotations``: FastAPI reads the local
    # alias ``Me`` when it builds the routes.
    Me = Annotated[Principal, Depends(principal)]

    @routes.get("/workspaces/{workspace_id}/custom-tools")
    async def list_tools(me: Me, workspace_id: uuid.UUID) -> list[dict[str, Any]]:
        tools = service()
        org_id, grants = await workspace_grants(me, workspace_id)
        return [view.public() for view in await tools.listing(org_id, grants, workspace_id)]

    @routes.get("/workspaces/{workspace_id}/custom-tools/scopes")
    async def writable_scopes(me: Me, workspace_id: uuid.UUID) -> dict[str, list[str]]:
        """The scopes in which the user can add a tool: ``user`` for every member, and the
        shared scopes for an admin of them (Section 10.8)."""
        tools = service()
        _org_id, grants = await workspace_grants(me, workspace_id)
        return {"scopes": list(tools.writable_scopes(grants, workspace_id))}

    @routes.post("/workspaces/{workspace_id}/custom-tools", status_code=201)
    async def create_tool(me: Me, workspace_id: uuid.UUID, body: ToolIn) -> dict[str, Any]:
        tools = service()
        org_id, grants = await workspace_grants(me, workspace_id)
        view = await _call(
            tools.create(
                org_id,
                grants,
                scope=body.scope,
                workspace_id=workspace_id,
                name=body.name,
                definition=body.definition(),
                summary=body.summary,
                credential=None if body.credential is None else body.credential.values(),
            )
        )
        return view.public()

    @routes.get("/custom-tools/{tool_id}")
    async def get_tool(me: Me, tool_id: uuid.UUID) -> dict[str, Any]:
        tools = service()
        org_id, grants = await tool_grants(me, tool_id)
        return (await _call(tools.get(org_id, tool_id, grants))).public()

    @routes.get("/custom-tools/{tool_id}/versions")
    async def tool_versions(me: Me, tool_id: uuid.UUID) -> list[dict[str, Any]]:
        tools = service()
        org_id, grants = await tool_grants(me, tool_id)
        return [v.public() for v in await _call(tools.versions(org_id, tool_id, grants))]

    @routes.put("/custom-tools/{tool_id}")
    async def edit_tool(me: Me, tool_id: uuid.UUID, body: DefinitionIn) -> dict[str, Any]:
        tools = service()
        org_id, grants = await tool_grants(me, tool_id)
        view = await _call(tools.edit(org_id, tool_id, grants, body.definition(), body.summary))
        return view.public()

    @routes.post("/custom-tools/{tool_id}/revert")
    async def revert_tool(me: Me, tool_id: uuid.UUID, body: RevertIn) -> dict[str, Any]:
        tools = service()
        org_id, grants = await tool_grants(me, tool_id)
        return (await _call(tools.revert(org_id, tool_id, grants, body.version))).public()

    @routes.put("/custom-tools/{tool_id}/credential")
    async def set_credential(me: Me, tool_id: uuid.UUID, body: CredentialIn) -> dict[str, Any]:
        tools = service()
        org_id, grants = await tool_grants(me, tool_id)
        view = await _call(tools.set_credential(org_id, tool_id, grants, body.values()))
        return view.public()

    @routes.post("/custom-tools/{tool_id}/test")
    async def test_tool(me: Me, tool_id: uuid.UUID, body: TestIn) -> dict[str, Any]:
        tools = service()
        org_id, grants = await tool_grants(me, tool_id)
        result = await _call(tools.test(org_id, tool_id, grants, body.arguments, body.version))
        return result.public()

    @routes.delete("/custom-tools/{tool_id}", status_code=204)
    async def delete_tool(me: Me, tool_id: uuid.UUID) -> Response:
        tools = service()
        org_id, grants = await tool_grants(me, tool_id)
        await _call(tools.delete(org_id, tool_id, grants))
        return Response(status_code=204)

    return routes
