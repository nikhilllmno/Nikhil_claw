"""The routes of coding mode (docs/architecture.md, Sections 11.4 and 15.5).

Each route first checks that the user can read the session, with both guards of the session
routes (``access``). The reads of the run events run as the user, so row-level security
applies. The writes go through ``CodingService`` and ``RunQueue`` after the check.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api.auth import Principal
from sama.api.events import MAX_MESSAGE_CHARS
from sama.coding_mode.activity import MAX_COMMANDS, list_commands, list_pull_requests
from sama.coding_mode.service import (
    MAX_PLAN_LIST,
    MAX_PR_TITLE_CHARS,
    CodingError,
    CodingService,
    PlanView,
    RepoView,
    pull_request_message,
)
from sama.db.engine import as_user
from sama.db.models import MAX_PLAN_CHARS, MAX_PLAN_FEEDBACK_CHARS
from sama.orchestrator.checkpoints import (
    CheckpointService,
    CheckpointServiceError,
    CheckpointView,
)
from sama.orchestrator.queue import QueueError, QuotaExceededError, RateLimitedError
from sama.runtime.checkpoints import MAX_HISTORY


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AttachRepo(_Body):
    repo: str = Field(min_length=1, max_length=300)
    ref: str | None = Field(default=None, max_length=200)
    folder: str | None = Field(default=None, max_length=100)


class PlanMode(_Body):
    on: bool


class PlanTask(_Body):
    task: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class Approve(_Body):
    body: str | None = Field(default=None, min_length=1, max_length=MAX_PLAN_CHARS)


class Reject(_Body):
    feedback: str | None = Field(default=None, max_length=MAX_PLAN_FEEDBACK_CHARS)


class PullRequestAsk(_Body):
    repo: str | None = Field(default=None, max_length=300)
    title: str | None = Field(default=None, max_length=MAX_PR_TITLE_CHARS)
    base: str | None = Field(default=None, max_length=200)
    draft: bool = False


class RepoOut(BaseModel):
    id: uuid.UUID
    repo: str
    url: str
    ref: str | None
    folder: str
    path: str | None
    status: str
    proxied: bool | None
    commit: str | None
    branch: str | None
    error: str | None
    created_at: datetime


class PlanOut(BaseModel):
    id: uuid.UUID
    version: int
    body: str
    proposed_body: str
    status: str
    edited: bool
    feedback: str | None
    run_id: uuid.UUID | None
    decided_at: datetime | None
    created_at: datetime


class CodingOut(BaseModel):
    plan_mode: bool
    repos: list[RepoOut]
    plan: PlanOut | None


class CheckpointRef(BaseModel):
    number: int
    created_at: datetime
    tool: str
    kind: str
    commit: str


class FileDiffOut(BaseModel):
    path: str
    old_path: str | None
    status: str
    additions: int | None
    deletions: int | None
    binary: bool
    patch: str
    truncated: bool


class DiffOut(BaseModel):
    base: CheckpointRef
    head: CheckpointRef | None
    files: list[FileDiffOut]
    truncated: bool


class CommandOut(BaseModel):
    run_id: uuid.UUID
    call_id: str
    tool: str
    command: str
    test: bool
    status: str
    output: str
    summary: str | None
    started_at: datetime
    finished_at: datetime | None


class PullRequestOut(BaseModel):
    run_id: uuid.UUID
    call_id: str
    repo: str | None
    title: str | None
    head: str | None
    base: str | None
    url: str | None
    status: str
    output: str
    created_at: datetime


class Access(BaseModel):
    """What the router needs from the session check of ``SamaApi``."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    org_id: uuid.UUID
    user_id: uuid.UUID


SessionCheck = Callable[[Principal, uuid.UUID], Awaitable[Access]]


def _repo(view: RepoView) -> RepoOut:
    return RepoOut.model_validate(view, from_attributes=True)


def _plan(view: PlanView) -> PlanOut:
    return PlanOut.model_validate(view, from_attributes=True)


def _ref(view: CheckpointView) -> CheckpointRef:
    return CheckpointRef.model_validate(view, from_attributes=True)


def _fail(error: CodingError) -> HTTPException:
    return HTTPException(status_code=error.status, detail=str(error))


def _queue_answer(error: QueueError) -> JSONResponse:
    if isinstance(error, RateLimitedError):
        return JSONResponse(
            status_code=429,
            content={"detail": str(error)},
            headers={"Retry-After": str(error.retry_after_s)},
        )
    if isinstance(error, QuotaExceededError):
        return JSONResponse(status_code=402, content={"detail": str(error), "quota": error.quota})
    return JSONResponse(status_code=409, content={"detail": str(error)})


def router(
    engine: AsyncEngine,
    service: CodingService | None,
    checkpoints: CheckpointService | None,
    principal: Callable[[Request], Awaitable[Principal]],
    check: SessionCheck,
) -> APIRouter:
    """Without ``service`` the write routes answer ``409``. Without ``checkpoints`` the diff
    route answers ``409``."""
    routes = APIRouter()
    # This module has no ``from __future__ import annotations``: FastAPI reads the local alias
    # ``Me`` when it builds the routes.
    Me = Annotated[Principal, Depends(principal)]

    def coding() -> CodingService:
        if service is None:
            raise HTTPException(status_code=409, detail="coding mode is not configured")
        return service

    async def queued(org_id: uuid.UUID, run_id: uuid.UUID, **more: Any) -> JSONResponse:
        waits = await coding().queue.waits(org_id=org_id, run_id=run_id)
        content = {"run_id": str(run_id), "queued": waits, **more}
        return JSONResponse(status_code=202, content=content)

    @routes.get("/sessions/{session_id}/coding")
    async def state(me: Me, session_id: uuid.UUID) -> CodingOut:
        access = await check(me, session_id)
        try:
            found = await coding().state(org_id=access.org_id, session_id=session_id)
        except CodingError as error:
            raise _fail(error) from None
        return CodingOut(
            plan_mode=found.plan_mode,
            repos=[_repo(item) for item in found.repos],
            plan=None if found.plan is None else _plan(found.plan),
        )

    @routes.get("/sessions/{session_id}/plans")
    async def plans(
        me: Me,
        session_id: uuid.UUID,
        limit: Annotated[int, Query(ge=1, le=MAX_PLAN_LIST)] = 10,
    ) -> list[PlanOut]:
        access = await check(me, session_id)
        listed = await coding().plans(org_id=access.org_id, session_id=session_id, limit=limit)
        return [_plan(item) for item in listed]

    @routes.post("/sessions/{session_id}/repos", status_code=201)
    async def attach(me: Me, session_id: uuid.UUID, body: AttachRepo) -> RepoOut:
        access = await check(me, session_id)
        try:
            view = await coding().attach(
                org_id=access.org_id,
                session_id=session_id,
                user_id=access.user_id,
                repo=body.repo,
                ref=body.ref,
                folder=body.folder,
            )
        except CodingError as error:
            raise _fail(error) from None
        return _repo(view)

    @routes.delete("/sessions/{session_id}/repos/{repo_id}", status_code=204)
    async def detach(me: Me, session_id: uuid.UUID, repo_id: uuid.UUID) -> Response:
        access = await check(me, session_id)
        try:
            await coding().detach(
                org_id=access.org_id,
                session_id=session_id,
                user_id=access.user_id,
                repo_id=repo_id,
            )
        except CodingError as error:
            raise _fail(error) from None
        return Response(status_code=204)

    @routes.put("/sessions/{session_id}/plan-mode")
    async def plan_mode(me: Me, session_id: uuid.UUID, body: PlanMode) -> dict[str, bool]:
        access = await check(me, session_id)
        try:
            on = await coding().set_plan_mode(
                org_id=access.org_id, session_id=session_id, user_id=access.user_id, on=body.on
            )
        except CodingError as error:
            raise _fail(error) from None
        return {"plan_mode": on}

    @routes.post("/sessions/{session_id}/plan", status_code=202)
    async def start_plan(me: Me, session_id: uuid.UUID, body: PlanTask) -> JSONResponse:
        access = await check(me, session_id)
        try:
            run_id = await coding().start_plan(
                org_id=access.org_id,
                session_id=session_id,
                user_id=access.user_id,
                task=body.task,
            )
        except CodingError as error:
            raise _fail(error) from None
        except QueueError as error:
            return _queue_answer(error)
        return await queued(access.org_id, run_id)

    async def decide(
        me: Principal, session_id: uuid.UUID, plan_id: uuid.UUID, approve: bool, text: str | None
    ) -> JSONResponse:
        access = await check(me, session_id)
        try:
            decided, run_id = await coding().decide(
                org_id=access.org_id,
                session_id=session_id,
                user_id=access.user_id,
                plan_id=plan_id,
                approve=approve,
                text=text,
            )
        except CodingError as error:
            raise _fail(error) from None
        except QueueError as error:
            # The decision went back: the plan is open again, so the person can retry.
            return _queue_answer(error)
        plan = _plan(decided.plan).model_dump(mode="json")
        if run_id is None:
            return JSONResponse({"run_id": None, "queued": False, "plan": plan})
        waits = await coding().queue.waits(org_id=access.org_id, run_id=run_id)
        return JSONResponse(
            {"run_id": str(run_id), "queued": waits, "plan": plan},
            status_code=202 if approve else 200,
        )

    @routes.post("/sessions/{session_id}/plans/{plan_id}/approve", status_code=202)
    async def approve(
        me: Me, session_id: uuid.UUID, plan_id: uuid.UUID, body: Approve | None = None
    ) -> JSONResponse:
        return await decide(me, session_id, plan_id, True, None if body is None else body.body)

    @routes.post("/sessions/{session_id}/plans/{plan_id}/reject")
    async def reject(
        me: Me, session_id: uuid.UUID, plan_id: uuid.UUID, body: Reject | None = None
    ) -> JSONResponse:
        return await decide(me, session_id, plan_id, False, None if body is None else body.feedback)

    @routes.get("/sessions/{session_id}/diff")
    async def diff(
        me: Me,
        session_id: uuid.UUID,
        base: Annotated[str, Query(pattern=r"^(oldest|[1-9][0-9]{0,3})$")] = "oldest",
        head: Annotated[int | None, Query(ge=1, le=MAX_HISTORY)] = None,
    ) -> DiffOut:
        access = await check(me, session_id)
        if checkpoints is None:
            raise HTTPException(status_code=409, detail="checkpoints are not configured")
        number = None if base == "oldest" else int(base)
        if number is not None and number > MAX_HISTORY:
            raise HTTPException(status_code=422, detail=f"base is at most {MAX_HISTORY}")
        try:
            found = await checkpoints.diff(
                org_id=access.org_id, session_id=session_id, base=number, head=head
            )
        except CheckpointServiceError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return DiffOut(
            base=_ref(found.base),
            head=None if found.head is None else _ref(found.head),
            files=[FileDiffOut.model_validate(f, from_attributes=True) for f in found.files],
            truncated=found.truncated,
        )

    @routes.get("/sessions/{session_id}/commands")
    async def commands(
        me: Me,
        session_id: uuid.UUID,
        tests: bool = False,
        limit: Annotated[int, Query(ge=1, le=MAX_COMMANDS)] = 50,
    ) -> list[CommandOut]:
        access = await check(me, session_id)
        async with as_user(engine, me.auth_user_id) as connection:
            listed = await list_commands(
                connection,
                org_id=access.org_id,
                session_id=session_id,
                tests_only=tests,
                limit=limit,
            )
        return [CommandOut.model_validate(item, from_attributes=True) for item in listed]

    @routes.get("/sessions/{session_id}/pull-requests")
    async def pull_requests(me: Me, session_id: uuid.UUID) -> list[PullRequestOut]:
        access = await check(me, session_id)
        async with as_user(engine, me.auth_user_id) as connection:
            listed = await list_pull_requests(
                connection, org_id=access.org_id, session_id=session_id
            )
        return [PullRequestOut.model_validate(item, from_attributes=True) for item in listed]

    @routes.post("/sessions/{session_id}/pull-requests", status_code=202)
    async def ask_pull_request(
        me: Me, session_id: uuid.UUID, body: PullRequestAsk | None = None
    ) -> JSONResponse:
        access = await check(me, session_id)
        ask = body or PullRequestAsk()
        text = pull_request_message(
            repo=(ask.repo or "").strip() or None,
            title=(ask.title or "").strip() or None,
            base=(ask.base or "").strip() or None,
            draft=ask.draft,
        )
        try:
            run_id = await coding().send(
                org_id=access.org_id, session_id=session_id, user_id=access.user_id, text=text
            )
        except QueueError as error:
            return _queue_answer(error)
        return await queued(access.org_id, run_id)

    return routes
