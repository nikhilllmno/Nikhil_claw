"""The routes of the org API keys (docs/architecture.md, Sections 10.7 and 11.4, M7.6).

An org owner or admin lists, makes, and revokes the keys of the org. The key goes to the
admin one time, in the answer of ``POST``. The other answers never have it.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sama.api.auth import Principal
from sama.apikeys.store import (
    MAX_EXPIRY_DAYS,
    NAME_MAX_CHARS,
    ApiKeyError,
    ApiKeyStore,
    Scope,
)
from sama.tenancy.access import Grants


def _both() -> list[Scope]:
    return ["chat", "mcp"]


class NewApiKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX_CHARS)
    ]
    workspace_id: uuid.UUID
    agent_profile_id: uuid.UUID | None = None
    scopes: list[Scope] = Field(default_factory=_both, min_length=1, max_length=2)
    approvals: Literal["deny", "pending"] = "deny"
    expires_in_days: Annotated[int, Field(ge=1, le=MAX_EXPIRY_DAYS)] | None = None


async def _call[T](call: Awaitable[T]) -> T:
    try:
        return await call
    except ApiKeyError as error:
        raise HTTPException(status_code=error.status, detail=str(error)) from None


def router(
    store: ApiKeyStore,
    principal: Callable[[Request], Awaitable[Principal]],
    org_admin: Callable[[Principal, uuid.UUID], Awaitable[Grants]],
) -> APIRouter:
    routes = APIRouter()
    # This module has no ``from __future__ import annotations``: FastAPI reads the local
    # alias ``Me`` when it builds the routes.
    Me = Annotated[Principal, Depends(principal)]

    @routes.get("/orgs/{org_id}/api-keys")
    async def list_api_keys(me: Me, org_id: uuid.UUID) -> list[dict[str, Any]]:
        await org_admin(me, org_id)
        return await store.list(org_id)

    @routes.post("/orgs/{org_id}/api-keys", status_code=201)
    async def create_api_key(me: Me, org_id: uuid.UUID, body: NewApiKey) -> JSONResponse:
        grants = await org_admin(me, org_id)
        view, key = await _call(
            store.create(
                org_id,
                actor_user_id=grants.user_id,
                name=body.name,
                workspace_id=body.workspace_id,
                agent_profile_id=body.agent_profile_id,
                scopes=body.scopes,
                approvals=body.approvals,
                expires_in_days=body.expires_in_days,
            )
        )
        return JSONResponse(
            status_code=201,
            content=jsonable_encoder({**view, "key": key}),
            headers={"Cache-Control": "no-store"},
        )

    @routes.delete("/orgs/{org_id}/api-keys/{key_id}", status_code=204)
    async def revoke_api_key(me: Me, org_id: uuid.UUID, key_id: uuid.UUID) -> Response:
        grants = await org_admin(me, org_id)
        await _call(store.revoke(org_id, key_id, actor_user_id=grants.user_id))
        return Response(status_code=204)

    return routes
