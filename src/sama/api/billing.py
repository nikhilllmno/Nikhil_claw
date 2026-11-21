"""The routes of billing (docs/architecture.md, Sections 11.4 and 17.5).

The org routes need a sign-in and an org admin. ``POST /stripe/webhook`` is public: the
Stripe signature authenticates it. ``sama.billing.BillingService`` checks the rules; these
routes map its errors to HTTP answers.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from sama.api.auth import Principal
from sama.billing.service import MAX_EVENT_BYTES, BillingService
from sama.billing.signature import SignatureError
from sama.orgs.service import OrgError
from sama.triggers.signature import read_body

log = logging.getLogger(__name__)

PlanName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]


class PlanChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: PlanName


async def _call[T](call: Awaitable[T]) -> T:
    try:
        return await call
    except OrgError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from None


def router(
    service: BillingService, principal: Callable[[Request], Awaitable[Principal]]
) -> APIRouter:
    routes = APIRouter()
    # This module has no ``from __future__ import annotations``: FastAPI reads the local
    # alias ``Me`` when it builds the routes.
    Me = Annotated[Principal, Depends(principal)]

    @routes.get("/billing/plans")
    async def plans() -> dict[str, Any]:
        return service.plans()

    @routes.get("/orgs/{org_id}/billing")
    async def overview(me: Me, org_id: uuid.UUID) -> dict[str, Any]:
        return await _call(service.overview(me.auth_user_id, org_id))

    @routes.post("/orgs/{org_id}/billing/checkout")
    async def checkout(
        me: Me, org_id: uuid.UUID, body: PlanChoice, request: Request
    ) -> dict[str, Any]:
        origin = request.headers.get("origin")
        return await _call(service.checkout(me.auth_user_id, org_id, body.plan, origin))

    @routes.put("/orgs/{org_id}/billing/plan")
    async def change_plan(me: Me, org_id: uuid.UUID, body: PlanChoice) -> dict[str, Any]:
        return await _call(service.change_plan(me.auth_user_id, org_id, body.plan))

    @routes.post("/orgs/{org_id}/billing/portal")
    async def portal(me: Me, org_id: uuid.UUID, request: Request) -> dict[str, Any]:
        origin = request.headers.get("origin")
        return await _call(service.portal(me.auth_user_id, org_id, origin))

    @routes.post("/stripe/webhook", include_in_schema=False)
    async def stripe_webhook(request: Request) -> JSONResponse:
        if not service.enabled:
            raise HTTPException(status_code=404, detail="billing is not set up on this server")
        body = await read_body(request, MAX_EVENT_BYTES)
        try:
            result = await _call(service.webhook(body, request.headers.get("stripe-signature")))
        except SignatureError as error:
            log.warning("a Stripe webhook was refused: %s", error)
            raise HTTPException(status_code=400, detail=str(error)) from None
        return JSONResponse({"received": True, "applied": result.applied, "reason": result.reason})

    return routes
