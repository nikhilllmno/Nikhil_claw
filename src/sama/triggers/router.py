"""The public routes of event triggers (docs/architecture.md, Section 12.2).

These routes have no user token: the signature of each delivery is the authentication. The body
limit holds while the body streams, before sama checks the signature.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from sama.triggers.service import TriggerService
from sama.triggers.signature import read_body


def router(service: TriggerService) -> APIRouter:
    routes = APIRouter()

    @routes.post("/triggers/{trigger_id}/webhook", include_in_schema=False)
    async def webhook(trigger_id: uuid.UUID, request: Request) -> JSONResponse:
        body = await read_body(request)
        return _answer(await service.webhook(trigger_id, request.headers, body))

    @routes.post("/composio/webhook", include_in_schema=False)
    async def composio(request: Request) -> JSONResponse:
        body = await read_body(request)
        return _answer(await service.composio(request.headers, body))

    return routes


def _answer(result: dict[str, Any]) -> JSONResponse:
    return JSONResponse(result, status_code=202 if result.get("accepted") else 200)
