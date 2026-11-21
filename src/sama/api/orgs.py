"""The routes of sign-up, invites, and roles (docs/architecture.md, Sections 11.4 and 17.2).

Every route needs a sign-in. ``sama.orgs.OrgService`` checks the rules; these routes map its
errors to HTTP answers.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Self

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from sama.api.auth import Principal
from sama.orgs.emails import MAX_EMAIL_CHARS
from sama.orgs.service import (
    MAX_DISPLAY_NAME_CHARS,
    MAX_ORG_NAME_CHARS,
    MAX_WORKSPACES_PER_CHANGE,
    SLUG_PATTERN,
    InviteRole,
    OrgError,
    OrgService,
    Role,
)

MAX_TOKEN_CHARS = 200

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewOrg(_Body):
    name: Annotated[Name, StringConstraints(max_length=MAX_ORG_NAME_CHARS)]
    slug: Annotated[str, StringConstraints(pattern=SLUG_PATTERN)] | None = None
    display_name: Annotated[Name, StringConstraints(max_length=MAX_DISPLAY_NAME_CHARS)] | None = (
        None
    )
    """The name of the user in the new org. Default: the part of the email before ``@``."""


class NewInvite(_Body):
    email: Annotated[str, StringConstraints(min_length=3, max_length=MAX_EMAIL_CHARS)]
    role: InviteRole = "member"
    workspace_id: uuid.UUID | None = None


class AcceptInvite(_Body):
    token: Annotated[str, StringConstraints(min_length=16, max_length=MAX_TOKEN_CHARS)] | None = (
        None
    )
    invite_id: uuid.UUID | None = None
    display_name: Annotated[Name, StringConstraints(max_length=MAX_DISPLAY_NAME_CHARS)] | None = (
        None
    )

    @model_validator(mode="after")
    def _one_way(self) -> Self:
        if (self.token is None) == (self.invite_id is None):
            raise ValueError("give the token or the invite id")
        return self


class MemberChange(_Body):
    role: Role | None = None
    workspace_ids: list[uuid.UUID] | None = Field(
        default=None, max_length=MAX_WORKSPACES_PER_CHANGE
    )
    """The workspaces of the member. It replaces the set."""

    @model_validator(mode="after")
    def _something(self) -> Self:
        if self.role is None and self.workspace_ids is None:
            raise ValueError("give the role or the workspaces")
        return self


async def _call[T](call: Awaitable[T]) -> T:
    try:
        return await call
    except OrgError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from None


def router(service: OrgService, principal: Callable[[Request], Awaitable[Principal]]) -> APIRouter:
    routes = APIRouter()
    # This module has no ``from __future__ import annotations``: FastAPI reads the local
    # alias ``Me`` when it builds the routes.
    Me = Annotated[Principal, Depends(principal)]

    @routes.post("/orgs", status_code=201)
    async def create_org(me: Me, body: NewOrg) -> dict[str, Any]:
        return await _call(
            service.create_org(
                me.auth_user_id, name=body.name, slug=body.slug, display_name=body.display_name
            )
        )

    @routes.get("/orgs/{org_id}/members")
    async def list_members(me: Me, org_id: uuid.UUID) -> list[dict[str, Any]]:
        return await _call(service.members(me.auth_user_id, org_id))

    @routes.patch("/orgs/{org_id}/members/{user_id}")
    async def change_member(
        me: Me, org_id: uuid.UUID, user_id: uuid.UUID, body: MemberChange
    ) -> dict[str, Any]:
        return await _call(
            service.change_member(
                me.auth_user_id,
                org_id,
                user_id,
                role=body.role,
                workspace_ids=body.workspace_ids,
            )
        )

    @routes.delete("/orgs/{org_id}/members/{user_id}", status_code=204)
    async def remove_member(me: Me, org_id: uuid.UUID, user_id: uuid.UUID) -> Response:
        await _call(service.remove_member(me.auth_user_id, org_id, user_id))
        return Response(status_code=204)

    @routes.get("/orgs/{org_id}/invites")
    async def list_invites(me: Me, org_id: uuid.UUID) -> list[dict[str, Any]]:
        return await _call(service.list_invites(me.auth_user_id, org_id))

    @routes.post("/orgs/{org_id}/invites", status_code=201)
    async def create_invite(me: Me, org_id: uuid.UUID, body: NewInvite) -> dict[str, Any]:
        return await _call(
            service.create_invite(
                me.auth_user_id,
                org_id,
                email=body.email,
                role=body.role,
                workspace_id=body.workspace_id,
            )
        )

    @routes.delete("/orgs/{org_id}/invites/{invite_id}", status_code=204)
    async def revoke_invite(me: Me, org_id: uuid.UUID, invite_id: uuid.UUID) -> Response:
        await _call(service.revoke_invite(me.auth_user_id, org_id, invite_id))
        return Response(status_code=204)

    @routes.get("/invites")
    async def my_invites(me: Me) -> list[dict[str, Any]]:
        return await _call(service.my_invites(me.auth_user_id))

    @routes.post("/invites/accept")
    async def accept_invite(me: Me, body: AcceptInvite) -> dict[str, Any]:
        return await _call(
            service.accept_invite(
                me.auth_user_id,
                token=body.token,
                invite_id=body.invite_id,
                display_name=body.display_name,
            )
        )

    return routes
