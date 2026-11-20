"""The sama HTTP API and the minimal web page (docs/architecture.md, Section 11.4).

Every request carries a Supabase Auth access token. The API has two guards for each session:

1. **Code.** It finds the user row of the token in the org of the session and checks the
   memberships with the rules of the RLS helpers (``sama.tenancy.access``).
2. **RLS.** It reads as the Supabase role ``authenticated`` with the claims of the user
   (``sama.db.as_user``), so the database shows only the rows that the user can read.

A session that the user cannot read gets ``404``, the same answer as a session that does not
exist. Writes go through the run queue and the tool gate, which connect as the table owner.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import Annotated, Any, Final, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import and_, delete, func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.types import Lifespan

from sama.api.auth import AuthError, JwtVerifier, Principal
from sama.db.engine import as_user
from sama.db.models import (
    AgentProfile,
    Approval,
    AuditEntry,
    ChannelIdentity,
    Integration,
    Message,
    Org,
    ReviewProposal,
    Run,
    RunEvent,
    Session,
    Skill,
    SkillVersion,
    User,
    Workspace,
)
from sama.gate.service import ApprovalError, ApprovalExpiredError, GateService
from sama.gateway.pairing import PairingError, create_code
from sama.integrations.service import IntegrationError, Integrations
from sama.memory import (
    CAPS,
    MemoryChangeError,
    MemoryDeniedError,
    MemoryScope,
    MemoryService,
    PostgresMemoryStore,
    Writer,
    scope_of,
)
from sama.memory import (
    MAX_REASON_CHARS as MAX_MEMORY_REASON_CHARS,
)
from sama.memory.core import MAX_ENTRY_CHARS, Target, used
from sama.memory.store import MemoryState, read_revisions, read_state
from sama.orchestrator.queue import QueueError, RateLimitedError, RunQueue
from sama.review.apply import ReviewApplier
from sama.review.shown import shown_proposal
from sama.skills.service import SkillLibrary
from sama.tenancy.access import Grants, load_grants
from sama.tools.clarify import ClarifyBroker, ClarifyError

log = logging.getLogger("sama.api")

MAX_MESSAGE_CHARS: Final = 32_000
MAX_TITLE_CHARS: Final = 200
MAX_REASON_CHARS: Final = 500
STREAM_BATCH: Final = 500
RECENT_RUNS: Final = 20
STREAM_GRACE_S: Final = 1.0
"""The response of a stream stops this long after the end time of the stream at the latest."""
OPEN_RUN_STATUSES: Final = frozenset({"queued", "running", "paused"})
NOT_FOUND: Final = "the session does not exist"
STATIC: Final = resources.files("sama.api") / "static"


class ApiSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    """The public key of the Supabase project. The web page needs it to sign in."""
    poll_s: float = Field(default=0.5, gt=0)
    """How often an event stream reads new rows."""
    heartbeat_s: float = Field(default=15.0, gt=0)
    max_stream_s: float = Field(default=3600.0, gt=0)
    """A stream ends after this time, or when the access token expires if that is earlier.
    The client connects again with a fresh token and ``Last-Event-ID``."""
    max_streams: int = Field(default=1000, gt=0)
    """The open event streams of this process. One more stream gets ``429``."""
    max_streams_per_user: int = Field(default=8, gt=0)
    """The open event streams of one user in this process."""


# -- request and response bodies ------------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewSession(_Body):
    workspace_id: uuid.UUID
    agent_profile_id: uuid.UUID
    audience: Literal["private", "shared"] = "private"
    title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)


class NewMessage(_Body):
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class Decision(_Body):
    reason: str | None = Field(default=None, max_length=MAX_REASON_CHARS)


class Approve(Decision):
    always_allow: bool = False
    """Also allow this tool for this user and this agent profile from now on."""


class NewPairingCode(_Body):
    workspace_id: uuid.UUID
    agent_profile_id: uuid.UUID
    kind: Literal["user", "group"] = "user"
    """``user`` links the chat account of the user. ``group`` links a group chat (Telegram)
    or a channel (Slack)."""


class TelegramConnect(_Body):
    bot_token: SecretStr = Field(min_length=1, max_length=200)
    """The token of the bot from @BotFather."""


class SlackConnect(_Body):
    bot_token: SecretStr = Field(min_length=1, max_length=300)
    """The bot token of the Slack app (``xoxb-``), from OAuth & Permissions."""
    signing_secret: SecretStr = Field(min_length=1, max_length=100)
    """The signing secret of the Slack app, from Basic Information."""


Channel = Literal["telegram", "slack"]

MAX_MEMORY_ENTRIES: Final = 100


class MemoryEdit(_Body):
    """The new notes of one scope. They replace all entries."""

    entries: list[Annotated[str, Field(max_length=MAX_ENTRY_CHARS)]] = Field(
        max_length=MAX_MEMORY_ENTRIES
    )
    agent_profile_id: uuid.UUID | None = None
    """Needed for the ``agent`` target."""
    reason: str | None = Field(default=None, max_length=MAX_MEMORY_REASON_CHARS)


class MemoryRevert(_Body):
    agent_profile_id: uuid.UUID | None = None
    reason: str | None = Field(default=None, max_length=MAX_MEMORY_REASON_CHARS)


class Answers(_Body):
    answers: list[str] = Field(min_length=1, max_length=10)


class SessionOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    agent_profile_id: uuid.UUID
    audience: str
    title: str | None
    status: str
    created_at: datetime


class RunOut(BaseModel):
    id: uuid.UUID
    status: str
    failure_code: str | None
    failure_message: str | None
    created_at: datetime


class SessionDetail(SessionOut):
    runs: list[RunOut]
    """The last runs, newest first."""
    last_event_id: int
    """Give it to the event stream as ``after`` to get only new events."""


# -- access ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionAccess:
    session: Any
    """The ``sessions`` row as read with RLS."""
    user_id: uuid.UUID
    """The user row of the token in the org of the session."""

    @property
    def org_id(self) -> uuid.UUID:
        return self.session.org_id  # type: ignore[no-any-return]


def _not_found(what: str = NOT_FOUND) -> HTTPException:
    return HTTPException(status_code=404, detail=what)


async def _user_id(
    connection: AsyncConnection, principal: Principal, org_id: uuid.UUID
) -> uuid.UUID | None:
    return (
        await connection.execute(
            select(User.id).where(
                User.org_id == org_id, User.auth_user_id == principal.auth_user_id
            )
        )
    ).scalar_one_or_none()


_SESSION_COLUMNS: Final = (
    Session.id,
    Session.org_id,
    Session.workspace_id,
    Session.agent_profile_id,
    Session.audience,
    Session.owner_user_id,
    Session.title,
    Session.status,
    Session.created_at,
)


async def session_access(
    connection: AsyncConnection, principal: Principal, session_id: uuid.UUID
) -> SessionAccess:
    """Checks that the user can read the session, with both guards. ``connection`` must run
    as the user (``as_user``). Raises ``404`` if not."""
    row = (
        await connection.execute(select(*_SESSION_COLUMNS).where(Session.id == session_id))
    ).one_or_none()
    if row is None:
        raise _not_found()
    user_id = await _user_id(connection, principal, row.org_id)
    if user_id is None:
        raise _not_found()
    grants = await load_grants(connection, row.org_id, user_id)
    if not grants.sees_session(row.workspace_id, row.audience, row.owner_user_id):
        raise _not_found()
    return SessionAccess(row, user_id)


def _session_out(row: Any) -> SessionOut:
    return SessionOut.model_validate(row, from_attributes=True)


def _sse(event: str, data: Any, event_id: int | None = None) -> str:
    lines = [] if event_id is None else [f"id: {event_id}"]
    lines += [f"event: {event}", f"data: {json.dumps(data, separators=(',', ':'))}"]
    return "\n".join(lines) + "\n\n"


class _EventStream(StreamingResponse):
    """A streaming response with a hard end time that gives back its stream slot at the end.

    The slot goes back also when the client goes away before the first chunk, because the
    release is in the response, not in the generator. The time limit covers the whole
    response, also a database read or a send to a slow client that blocks.
    """

    def __init__(
        self,
        content: AsyncIterator[str],
        *,
        release: Callable[[], None],
        lifetime: float,
        **kwargs: Any,
    ) -> None:
        super().__init__(content, **kwargs)
        self._release = release
        self._lifetime = lifetime

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            async with asyncio.timeout(max(0.0, self._lifetime) + STREAM_GRACE_S):
                await super().__call__(scope, receive, send)
        except TimeoutError:
            log.info("an event stream reached its end time")
        finally:
            self._release()


def _run_out(run: Any) -> dict[str, Any]:
    return {
        "run_id": str(run.id),
        "status": run.status,
        "failure_code": run.failure_code,
        "failure_message": run.failure_message,
    }


def _cursor(after: int | None, last_event_id: str | None) -> int | None:
    if last_event_id:
        try:
            return max(0, int(last_event_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="Last-Event-ID must be a number") from None
    return after


async def _principal(request: Request) -> Principal:
    api: SamaApi = request.app.state.sama_api
    return await api.principal(request)


Me = Annotated[Principal, Depends(_principal)]
"""The user of the request. A route with this parameter needs a valid access token."""


def _chat_runs(session_id: uuid.UUID) -> Any:
    """The chat runs of a session. A background review run (Section 9.4) is not part of the
    chat: its runs and events stay out of the run list and the event stream."""
    return select(Run.id).where(Run.session_id == session_id, Run.kind == "chat")


# -- the service ----------------------------------------------------------------------------


class SamaApi:
    """The API of one control plane process."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        verifier: JwtVerifier,
        queue: RunQueue | None = None,
        gate: GateService | None = None,
        clarify: ClarifyBroker | None = None,
        settings: ApiSettings | None = None,
        integrations: Integrations | None = None,
        memory: MemoryService | None = None,
        skills: SkillLibrary | None = None,
        reviews: ReviewApplier | None = None,
    ) -> None:
        """Without ``integrations`` the integration routes answer ``409``. Without ``skills``,
        the skill routes answer ``409`` and a message that starts with ``/`` is not a skill
        command."""
        self._engine = engine
        self._memory = memory or MemoryService(PostgresMemoryStore(engine))
        self._integrations = integrations
        self._skills = skills
        self._reviews = reviews
        self._verifier = verifier
        self._queue = queue or RunQueue(engine)
        self._gate = gate or GateService(engine)
        self._clarify = clarify
        self._settings = settings or ApiSettings()
        self._streams: Counter[uuid.UUID] = Counter()

    async def principal(self, request: Request) -> Principal:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(
                status_code=401,
                detail="sign in first",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            return await self._verifier.verify(token.strip())
        except AuthError as error:
            raise HTTPException(
                status_code=401,
                detail=str(error),
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            ) from None

    def app(self, *, lifespan: Lifespan[FastAPI] | None = None) -> FastAPI:
        app = FastAPI(title="sama", docs_url=None, redoc_url=None, lifespan=lifespan)
        app.state.sama_api = self
        settings = self._settings

        @app.middleware("http")
        async def security_headers(request: Request, call_next: Any) -> Response:
            response: Response = await call_next(request)
            connect = "'self'"
            if settings.supabase_url:
                connect += f" {settings.supabase_url.rstrip('/')}"
            response.headers["Content-Security-Policy"] = (
                f"default-src 'self'; connect-src {connect}; img-src 'self' data:; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response

        @app.get("/healthz")
        async def healthz() -> dict[str, str]:
            return {"status": "ok"}

        @app.get("/", include_in_schema=False)
        async def page() -> FileResponse:
            return FileResponse(str(STATIC / "index.html"), media_type="text/html")

        app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

        @app.get("/config")
        async def config() -> dict[str, Any]:
            return {
                "supabase_url": settings.supabase_url,
                "supabase_anon_key": settings.supabase_anon_key,
                "integrations": self._integrations is not None and self._integrations.enabled,
            }

        @app.get("/me")
        async def me(principal: Me) -> dict[str, Any]:
            return {"orgs": await self._orgs(principal)}

        @app.get("/sessions")
        async def list_sessions(
            principal: Me, limit: Annotated[int, Query(ge=1, le=100)] = 50
        ) -> list[SessionOut]:
            return await self._sessions(principal, limit)

        @app.post("/sessions", status_code=201)
        async def create_session(principal: Me, body: NewSession) -> SessionOut:
            return await self._create_session(principal, body)

        @app.get("/sessions/{session_id}")
        async def get_session(principal: Me, session_id: uuid.UUID) -> SessionDetail:
            return await self._detail(principal, session_id)

        @app.get("/sessions/{session_id}/messages")
        async def list_messages(
            principal: Me,
            session_id: uuid.UUID,
            after: Annotated[int, Query(ge=0)] = 0,
            limit: Annotated[int, Query(ge=1, le=500)] = 200,
        ) -> list[dict[str, Any]]:
            return await self._messages(principal, session_id, after, limit)

        @app.post("/sessions/{session_id}/messages", status_code=202)
        async def send_message(
            principal: Me, session_id: uuid.UUID, body: NewMessage
        ) -> JSONResponse:
            return await self._send(principal, session_id, body)

        @app.get("/sessions/{session_id}/events")
        async def events(
            principal: Me,
            session_id: uuid.UUID,
            after: Annotated[int | None, Query(ge=0)] = None,
            last_event_id: Annotated[str | None, Header()] = None,
        ) -> StreamingResponse:
            cursor = _cursor(after, last_event_id)
            async with as_user(self._engine, principal.auth_user_id) as connection:
                await session_access(connection, principal, session_id)
                if cursor is None:
                    cursor = await self._last_event_id(connection, session_id)
            lifetime = self._stream_lifetime(principal)
            release = self._open_stream(principal.auth_user_id)
            return _EventStream(
                self._stream(principal, session_id, cursor, lifetime),
                release=release,
                lifetime=lifetime,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        @app.post("/runs/{run_id}/cancel")
        async def cancel(principal: Me, run_id: uuid.UUID) -> dict[str, str]:
            return await self._cancel(principal, run_id)

        @app.get("/sessions/{session_id}/approvals")
        async def approvals(principal: Me, session_id: uuid.UUID) -> list[dict[str, Any]]:
            return await self._approvals(principal, session_id)

        @app.get("/workspaces/{workspace_id}/review-proposals")
        async def review_proposals(principal: Me, workspace_id: uuid.UUID) -> list[dict[str, Any]]:
            return await self.review_proposals(principal, workspace_id)

        @app.post("/approvals/{approval_id}/approve")
        async def approve(
            principal: Me, approval_id: uuid.UUID, body: Approve | None = None
        ) -> JSONResponse:
            body = body or Approve()
            return await self._decide(principal, approval_id, True, body.always_allow, body.reason)

        @app.post("/approvals/{approval_id}/deny")
        async def deny(
            principal: Me, approval_id: uuid.UUID, body: Decision | None = None
        ) -> JSONResponse:
            reason = (body or Decision()).reason
            return await self._decide(principal, approval_id, False, False, reason)

        @app.get("/sessions/{session_id}/questions")
        async def questions(principal: Me, session_id: uuid.UUID) -> list[dict[str, Any]]:
            return await self._questions(principal, session_id)

        @app.post("/sessions/{session_id}/questions/{request_id}/answer", status_code=204)
        async def answer(
            principal: Me, session_id: uuid.UUID, request_id: str, body: Answers
        ) -> Response:
            await self._answer(principal, session_id, request_id, body.answers)
            return Response(status_code=204)

        @app.get("/orgs/{org_id}/integrations")
        async def list_integrations(principal: Me, org_id: uuid.UUID) -> list[dict[str, Any]]:
            await self._org_grants(principal, org_id)
            return [view.public() for view in await self._require_integrations().list(org_id)]

        @app.post("/orgs/{org_id}/integrations/telegram", status_code=201)
        async def connect_telegram(
            principal: Me, org_id: uuid.UUID, body: TelegramConnect
        ) -> dict[str, Any]:
            grants = await self._org_admin(principal, org_id)
            try:
                view = await self._require_integrations().connect_telegram(
                    org_id, grants.user_id, body.bot_token
                )
            except IntegrationError as error:
                raise HTTPException(status_code=error.status_code, detail=str(error)) from None
            return view.public()

        @app.post("/orgs/{org_id}/integrations/slack/setup")
        async def setup_slack(principal: Me, org_id: uuid.UUID) -> dict[str, Any]:
            grants = await self._org_admin(principal, org_id)
            try:
                setup = await self._require_integrations().setup_slack(org_id, grants.user_id)
            except IntegrationError as error:
                raise HTTPException(status_code=error.status_code, detail=str(error)) from None
            return setup.public()

        @app.post("/orgs/{org_id}/integrations/slack", status_code=201)
        async def connect_slack(
            principal: Me, org_id: uuid.UUID, body: SlackConnect
        ) -> dict[str, Any]:
            grants = await self._org_admin(principal, org_id)
            try:
                view = await self._require_integrations().connect_slack(
                    org_id, grants.user_id, body.bot_token, body.signing_secret
                )
            except IntegrationError as error:
                raise HTTPException(status_code=error.status_code, detail=str(error)) from None
            return view.public()

        @app.delete("/orgs/{org_id}/integrations/{kind}", status_code=204)
        async def disconnect(principal: Me, org_id: uuid.UUID, kind: Channel) -> Response:
            grants = await self._org_admin(principal, org_id)
            try:
                gone = await self._require_integrations().disconnect(org_id, kind, grants.user_id)
            except IntegrationError as error:
                raise HTTPException(status_code=error.status_code, detail=str(error)) from None
            if not gone:
                raise _not_found("the org has no such integration")
            return Response(status_code=204)

        @app.get("/orgs/{org_id}/skills")
        async def list_skills(
            principal: Me, org_id: uuid.UUID, workspace_id: uuid.UUID
        ) -> list[dict[str, Any]]:
            return await self._skills_of(principal, org_id, workspace_id)

        @app.get("/skills/{skill_id}/versions")
        async def skill_versions(principal: Me, skill_id: uuid.UUID) -> list[dict[str, Any]]:
            return await self._skill_versions(principal, skill_id)

        @app.post("/channels/{channel}/pairing-codes", status_code=201)
        async def pairing_code(
            principal: Me, channel: Channel, body: NewPairingCode
        ) -> dict[str, Any]:
            return await self._pairing_code(principal, channel, body)

        @app.get("/channels/{channel}/identities")
        async def identities(principal: Me, channel: Channel) -> list[dict[str, Any]]:
            return await self._identities(principal, channel)

        @app.delete("/channels/{channel}/identities/{identity_id}", status_code=204)
        async def unlink(principal: Me, channel: Channel, identity_id: uuid.UUID) -> Response:
            await self._unlink(principal, channel, identity_id)
            return Response(status_code=204)

        @app.get("/workspaces/{workspace_id}/memory")
        async def memory_notes(
            principal: Me, workspace_id: uuid.UUID, agent_profile_id: uuid.UUID | None = None
        ) -> dict[str, Any]:
            return await self._memory_notes(principal, workspace_id, agent_profile_id)

        @app.get("/workspaces/{workspace_id}/memory/{target}/revisions")
        async def memory_revisions(
            principal: Me,
            workspace_id: uuid.UUID,
            target: Target,
            agent_profile_id: uuid.UUID | None = None,
            limit: Annotated[int, Query(ge=1, le=100)] = 20,
        ) -> list[dict[str, Any]]:
            return await self._memory_revisions(
                principal, workspace_id, target, agent_profile_id, limit
            )

        @app.put("/workspaces/{workspace_id}/memory/{target}")
        async def memory_edit(
            principal: Me, workspace_id: uuid.UUID, target: Target, body: MemoryEdit
        ) -> dict[str, Any]:
            return await self._memory_write(
                principal, workspace_id, target, body.agent_profile_id, body.reason, None, body
            )

        @app.post("/workspaces/{workspace_id}/memory/{target}/revisions/{seq}/revert")
        async def memory_revert(
            principal: Me, workspace_id: uuid.UUID, target: Target, seq: int, body: MemoryRevert
        ) -> dict[str, Any]:
            return await self._memory_write(
                principal, workspace_id, target, body.agent_profile_id, body.reason, seq, None
            )

        return app

    # -- reads ------------------------------------------------------------------------------

    async def _orgs(self, principal: Principal) -> list[dict[str, Any]]:
        """The orgs, workspaces, and agent profiles that the user can see."""
        result: list[dict[str, Any]] = []
        async with as_user(self._engine, principal.auth_user_id) as connection:
            users = await connection.execute(
                select(User.id, User.org_id, User.display_name, Org.name)
                .join(Org, Org.id == User.org_id)
                .where(User.auth_user_id == principal.auth_user_id)
                .order_by(Org.name, Org.id)
            )
            for user in users.all():
                grants = await load_grants(connection, user.org_id, user.id)
                if not grants.org_member:
                    continue
                workspaces = [
                    {"id": str(w.id), "name": w.name}
                    for w in await connection.execute(
                        select(Workspace.id, Workspace.name)
                        .where(Workspace.org_id == user.org_id)
                        .order_by(Workspace.name, Workspace.id)
                    )
                    if grants.sees_workspace(w.id)
                ]
                profiles = [
                    {
                        "id": str(p.id),
                        "name": p.name,
                        "workspace_id": None if p.workspace_id is None else str(p.workspace_id),
                        "runtime": p.runtime,
                        "model": p.model,
                    }
                    for p in await connection.execute(
                        select(
                            AgentProfile.id,
                            AgentProfile.name,
                            AgentProfile.workspace_id,
                            AgentProfile.runtime,
                            AgentProfile.model,
                        )
                        .where(AgentProfile.org_id == user.org_id)
                        .order_by(AgentProfile.name, AgentProfile.id)
                    )
                    if p.workspace_id is None or grants.sees_workspace(p.workspace_id)
                ]
                integrations = [
                    {"kind": i.kind, "status": i.status, "name": i.name}
                    for i in await connection.execute(
                        select(Integration.kind, Integration.status, Integration.name)
                        .where(Integration.org_id == user.org_id)
                        .order_by(Integration.kind)
                    )
                ]
                result.append(
                    {
                        "id": str(user.org_id),
                        "name": user.name,
                        "user_id": str(user.id),
                        "display_name": user.display_name,
                        "admin": grants.org_admin,
                        "workspaces": workspaces,
                        "agent_profiles": profiles,
                        "integrations": integrations,
                    }
                )
        return result

    async def _sessions(self, principal: Principal, limit: int) -> list[SessionOut]:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            rows = (
                await connection.execute(
                    select(*_SESSION_COLUMNS)
                    .where(Session.status == "active")
                    .order_by(Session.created_at.desc(), Session.id)
                    .limit(limit)
                )
            ).all()
            grants: dict[uuid.UUID, Grants | None] = {}
            for org_id in {row.org_id for row in rows}:
                user_id = await _user_id(connection, principal, org_id)
                grants[org_id] = (
                    None if user_id is None else await load_grants(connection, org_id, user_id)
                )
        return [
            _session_out(row)
            for row in rows
            if (g := grants[row.org_id]) is not None
            and g.sees_session(row.workspace_id, row.audience, row.owner_user_id)
        ]

    async def _detail(self, principal: Principal, session_id: uuid.UUID) -> SessionDetail:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            access = await session_access(connection, principal, session_id)
            runs = await connection.execute(
                select(
                    Run.id,
                    Run.status,
                    Run.failure_code,
                    Run.failure_message,
                    Run.created_at,
                )
                .where(
                    Run.org_id == access.org_id,
                    Run.session_id == session_id,
                    Run.kind == "chat",
                )
                .order_by(Run.created_at.desc(), Run.id.desc())
                .limit(RECENT_RUNS)
            )
            run_list = [RunOut.model_validate(row, from_attributes=True) for row in runs]
            last = await self._last_event_id(connection, session_id)
        return SessionDetail(
            **_session_out(access.session).model_dump(), runs=run_list, last_event_id=last
        )

    @staticmethod
    async def _last_event_id(connection: AsyncConnection, session_id: uuid.UUID) -> int:
        value = (
            await connection.execute(
                select(func.coalesce(func.max(RunEvent.id), 0)).where(
                    RunEvent.session_id == session_id, RunEvent.run_id.in_(_chat_runs(session_id))
                )
            )
        ).scalar_one()
        return int(value)

    async def _messages(
        self, principal: Principal, session_id: uuid.UUID, after: int, limit: int
    ) -> list[dict[str, Any]]:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            access = await session_access(connection, principal, session_id)
            rows = await connection.execute(
                select(
                    Message.id,
                    Message.seq,
                    Message.run_id,
                    Message.role,
                    Message.author_user_id,
                    Message.content,
                    Message.created_at,
                )
                .where(
                    Message.org_id == access.org_id,
                    Message.session_id == session_id,
                    Message.seq > after,
                )
                .order_by(Message.seq)
                .limit(limit)
            )
            return [
                {
                    "id": str(row.id),
                    "seq": row.seq,
                    "run_id": None if row.run_id is None else str(row.run_id),
                    "role": row.role,
                    "author_user_id": None
                    if row.author_user_id is None
                    else str(row.author_user_id),
                    "text": str((row.content or {}).get("text", "")),
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

    async def _approvals(self, principal: Principal, session_id: uuid.UUID) -> list[dict[str, Any]]:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            access = await session_access(connection, principal, session_id)
            rows = await connection.execute(
                select(
                    Approval.id,
                    Approval.run_id,
                    Approval.call_id,
                    Approval.tool_name,
                    Approval.arguments,
                    Approval.reason,
                    Approval.created_at,
                    Approval.expires_at,
                    ReviewProposal.kind.label("proposal_kind"),
                    ReviewProposal.scope.label("proposal_scope"),
                    ReviewProposal.payload.label("proposal_payload"),
                )
                .outerjoin(
                    ReviewProposal,
                    and_(
                        ReviewProposal.org_id == Approval.org_id,
                        ReviewProposal.approval_id == Approval.id,
                    ),
                )
                .where(
                    Approval.org_id == access.org_id,
                    Approval.session_id == session_id,
                    Approval.status == "pending",
                )
                .order_by(Approval.created_at, Approval.id)
            )
            return [self._approval_entry(row) for row in rows]

    @staticmethod
    def _approval_entry(row: Any) -> dict[str, Any]:
        arguments, reason = row.arguments, row.reason
        if row.proposal_payload is not None:
            # A proposal of a background review. The stored text is not shown: the redactor
            # may know more secrets now (Section 9.4).
            shown = shown_proposal(row.proposal_kind, row.proposal_scope, row.proposal_payload)
            arguments, reason = shown.arguments, shown.reason
        return {
            "id": str(row.id),
            "run_id": str(row.run_id),
            "call_id": row.call_id,
            "tool_name": row.tool_name,
            "arguments": arguments,
            "reason": reason,
            "created_at": row.created_at.isoformat(),
            "expires_at": row.expires_at.isoformat(),
        }

    async def _questions(self, principal: Principal, session_id: uuid.UUID) -> list[dict[str, Any]]:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            access = await session_access(connection, principal, session_id)
        if self._clarify is None:
            return []
        return [
            {
                "id": request.id,
                "run_id": request.run_id,
                "questions": [q.model_dump(mode="json") for q in request.questions],
            }
            for request in self._clarify.pending(str(access.org_id), str(session_id))
        ]

    # -- memory -----------------------------------------------------------------------------

    async def _memory_access(
        self,
        connection: AsyncConnection,
        principal: Principal,
        workspace_id: uuid.UUID,
        target: Target,
        agent_profile_id: uuid.UUID | None,
    ) -> tuple[MemoryScope, uuid.UUID, bool]:
        """The scope, the user row, and whether the user is a workspace admin. Raises ``404``
        if the user cannot see the workspace or the profile is not in it, and ``422`` if an
        ``agent`` call has no profile."""
        org_id = (
            await connection.execute(select(Workspace.org_id).where(Workspace.id == workspace_id))
        ).scalar_one_or_none()
        user_id = None if org_id is None else await _user_id(connection, principal, org_id)
        if org_id is None or user_id is None:
            raise _not_found("the workspace does not exist")
        grants = await load_grants(connection, org_id, user_id)
        if not grants.sees_workspace(workspace_id):
            raise _not_found("the workspace does not exist")
        if target is Target.AGENT:
            if agent_profile_id is None:
                raise HTTPException(status_code=422, detail="agent_profile_id is needed")
            profile_workspace = (
                await connection.execute(
                    select(AgentProfile.workspace_id).where(
                        AgentProfile.org_id == org_id, AgentProfile.id == agent_profile_id
                    )
                )
            ).one_or_none()
            if profile_workspace is None or profile_workspace[0] not in (None, workspace_id):
                raise _not_found("the agent profile does not exist in this workspace")
        scope = scope_of(
            target,
            org_id=org_id,
            workspace_id=workspace_id,
            agent_profile_id=agent_profile_id,
            user_id=user_id,
        )
        return scope, user_id, grants.admins_workspace(workspace_id)

    async def _memory_notes(
        self, principal: Principal, workspace_id: uuid.UUID, agent_profile_id: uuid.UUID | None
    ) -> dict[str, Any]:
        targets = [Target.WORKSPACE, Target.USER]
        if agent_profile_id is not None:
            targets.insert(0, Target.AGENT)
        notes: list[dict[str, Any]] = []
        async with as_user(self._engine, principal.auth_user_id) as connection:
            for target in targets:
                scope, _, admin = await self._memory_access(
                    connection, principal, workspace_id, target, agent_profile_id
                )
                state = await read_state(connection, scope)
                notes.append(
                    {
                        "target": target.value,
                        "entries": list(state.entries),
                        "chars": used(state.entries),
                        "cap": CAPS[target],
                        "revision": state.revision,
                        "can_edit": admin or target is not Target.WORKSPACE,
                    }
                )
        return {"workspace_id": str(workspace_id), "targets": notes}

    async def _memory_revisions(
        self,
        principal: Principal,
        workspace_id: uuid.UUID,
        target: Target,
        agent_profile_id: uuid.UUID | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            scope, _, _ = await self._memory_access(
                connection, principal, workspace_id, target, agent_profile_id
            )
            revisions = await read_revisions(connection, scope, limit)
        return [
            {
                "seq": r.seq,
                "action": r.action,
                "entries": list(r.entries),
                "author_kind": r.author_kind,
                "author_user_id": None if r.author_user_id is None else str(r.author_user_id),
                "session_id": None if r.session_id is None else str(r.session_id),
                "run_id": None if r.run_id is None else str(r.run_id),
                "reason": r.reason,
                "reverted_to": r.reverted_to,
                "created_at": r.created_at.isoformat(),
            }
            for r in revisions
        ]

    async def _memory_write(
        self,
        principal: Principal,
        workspace_id: uuid.UUID,
        target: Target,
        agent_profile_id: uuid.UUID | None,
        reason: str | None,
        revert_to: int | None,
        edit: MemoryEdit | None,
    ) -> dict[str, Any]:
        """A person replaces the notes of a scope (``edit``) or sets them back to an old
        revision (``revert_to``)."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            scope, user_id, admin = await self._memory_access(
                connection, principal, workspace_id, target, agent_profile_id
            )
            if revert_to is not None and not await read_revisions(
                connection, scope, 1, seq=revert_to
            ):
                raise _not_found("the revision does not exist")
        if target is Target.WORKSPACE and not admin:
            raise HTTPException(
                status_code=403, detail="only a workspace admin can change workspace memory"
            )
        writer = Writer("user", user_id)

        async def audit(connection: AsyncConnection, state: MemoryState) -> None:
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=scope.org_id,
                    workspace_id=workspace_id,
                    actor_kind="user",
                    actor_user_id=user_id,
                    action="memory.reverted" if revert_to is not None else "memory.edited",
                    target_type="memory",
                    target_id=target.value,
                    details={"revision": state.revision, "reverted_to": revert_to},
                )
            )

        try:
            if edit is not None:
                state = await self._memory.edit(scope, edit.entries, writer, reason, after=audit)
            elif revert_to is not None:
                state = await self._memory.revert(scope, revert_to, writer, reason, after=audit)
        except MemoryDeniedError as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        except MemoryChangeError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return {
            "target": target.value,
            "entries": list(state.entries),
            "chars": used(state.entries),
            "cap": CAPS[target],
            "revision": state.revision,
        }

    # -- writes -----------------------------------------------------------------------------

    async def _workspace_access(
        self, principal: Principal, workspace_id: uuid.UUID, agent_profile_id: uuid.UUID
    ) -> tuple[uuid.UUID, uuid.UUID]:
        """The org and the user row for a new session in the workspace with the agent profile.
        Raises ``404`` if the user cannot see the workspace or the profile is not in it."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            org_id = (
                await connection.execute(
                    select(Workspace.org_id).where(Workspace.id == workspace_id)
                )
            ).scalar_one_or_none()
            user_id = None if org_id is None else await _user_id(connection, principal, org_id)
            if org_id is None or user_id is None:
                raise _not_found("the workspace does not exist")
            grants = await load_grants(connection, org_id, user_id)
            if not grants.sees_workspace(workspace_id):
                raise _not_found("the workspace does not exist")
            profile_workspace = (
                await connection.execute(
                    select(AgentProfile.workspace_id).where(
                        AgentProfile.org_id == org_id, AgentProfile.id == agent_profile_id
                    )
                )
            ).one_or_none()
            if profile_workspace is None or profile_workspace[0] not in (None, workspace_id):
                raise _not_found("the agent profile does not exist in this workspace")
        return org_id, user_id

    async def _create_session(self, principal: Principal, body: NewSession) -> SessionOut:
        org_id, user_id = await self._workspace_access(
            principal, body.workspace_id, body.agent_profile_id
        )
        session_id = uuid.uuid4()
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    insert(Session)
                    .values(
                        id=session_id,
                        org_id=org_id,
                        workspace_id=body.workspace_id,
                        agent_profile_id=body.agent_profile_id,
                        audience=body.audience,
                        owner_user_id=user_id if body.audience == "private" else None,
                        title=body.title,
                    )
                    .returning(*_SESSION_COLUMNS)
                )
            ).one()
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=org_id,
                    workspace_id=body.workspace_id,
                    actor_kind="user",
                    actor_user_id=user_id,
                    action="session.created",
                    target_type="session",
                    target_id=str(session_id),
                    details={"audience": body.audience, "channel": "web"},
                )
            )
        return _session_out(row)

    async def _pairing_code(
        self, principal: Principal, channel: Channel, body: NewPairingCode
    ) -> dict[str, Any]:
        org_id, user_id = await self._workspace_access(
            principal, body.workspace_id, body.agent_profile_id
        )
        async with self._engine.connect() as connection:
            bot = (
                await connection.execute(
                    select(
                        Integration.id,
                        Integration.name,
                        Integration.external_id,
                        Integration.settings,
                    ).where(
                        Integration.org_id == org_id,
                        Integration.kind == channel,
                        Integration.status == "active",
                    )
                )
            ).one_or_none()
        if bot is None:
            app_name = "Telegram bot" if channel == "telegram" else "Slack app"
            raise HTTPException(
                status_code=409,
                detail=f"the org has no {app_name}. An org admin connects one under Integrations.",
            )
        try:
            async with self._engine.begin() as connection:
                code, expires_at = await create_code(
                    connection,
                    org_id=org_id,
                    user_id=user_id,
                    integration_id=bot.id,
                    channel=channel,
                    kind=body.kind,
                    workspace_id=body.workspace_id,
                    agent_profile_id=body.agent_profile_id,
                )
        except PairingError as error:
            raise HTTPException(status_code=429, detail=str(error)) from None
        if channel == "slack":
            # The link opens the direct messages with the app. The code goes in by hand.
            app_id = (bot.settings or {}).get("app_id")
            link = (
                f"https://slack.com/app_redirect?app={app_id}&team={bot.external_id}"
                if app_id
                else None
            )
            command = f"/sama pair {code}"
        else:
            # The link opens the bot and sends the code with /start. A group link adds the bot
            # to a group first.
            start = "start" if body.kind == "user" else "startgroup"
            link = f"https://t.me/{bot.name}?{start}={code}"
            command = f"/pair {code}"
        return {
            "code": code,
            "kind": body.kind,
            "expires_at": expires_at.isoformat(),
            "bot_username": bot.name,
            "link": link,
            "command": command,
        }

    def _require_integrations(self) -> Integrations:
        if self._integrations is None:
            raise HTTPException(status_code=409, detail="integrations are off in this process")
        return self._integrations

    async def _org_grants(self, principal: Principal, org_id: uuid.UUID) -> Grants:
        """The grants of the user in the org. Raises ``404`` for a user who is not a member."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            user_id = await _user_id(connection, principal, org_id)
            grants = None if user_id is None else await load_grants(connection, org_id, user_id)
        if grants is None or not grants.org_member:
            raise _not_found("the org does not exist")
        return grants

    async def _org_admin(self, principal: Principal, org_id: uuid.UUID) -> Grants:
        """Raises ``404`` for a user who is not a member and ``403`` for a member who is not an
        owner or an admin of the org."""
        grants = await self._org_grants(principal, org_id)
        if not grants.org_admin:
            raise HTTPException(status_code=403, detail="only an org owner or admin can do this")
        return grants

    async def _identities(self, principal: Principal, channel: Channel) -> list[dict[str, Any]]:
        """The chat accounts of ``channel`` that are linked to the user, in all orgs."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            rows = await connection.execute(
                select(
                    ChannelIdentity.id,
                    ChannelIdentity.org_id,
                    ChannelIdentity.workspace_id,
                    ChannelIdentity.agent_profile_id,
                    ChannelIdentity.integration_id,
                    ChannelIdentity.created_at,
                )
                .where(ChannelIdentity.channel == channel)
                .order_by(ChannelIdentity.created_at)
            )
            return [
                {
                    "id": str(row.id),
                    "org_id": str(row.org_id),
                    "workspace_id": str(row.workspace_id),
                    "agent_profile_id": str(row.agent_profile_id),
                    "integration_id": str(row.integration_id),
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

    async def _unlink(self, principal: Principal, channel: Channel, identity_id: uuid.UUID) -> None:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            row = (
                await connection.execute(
                    select(
                        ChannelIdentity.org_id,
                        ChannelIdentity.user_id,
                        ChannelIdentity.workspace_id,
                        ChannelIdentity.channel,
                        ChannelIdentity.external_id,
                    ).where(ChannelIdentity.id == identity_id, ChannelIdentity.channel == channel)
                )
            ).one_or_none()
        if row is None:
            raise _not_found("the linked account does not exist")
        async with self._engine.begin() as connection:
            await connection.execute(
                delete(ChannelIdentity).where(
                    ChannelIdentity.org_id == row.org_id, ChannelIdentity.id == identity_id
                )
            )
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=row.org_id,
                    workspace_id=row.workspace_id,
                    actor_kind="user",
                    actor_user_id=row.user_id,
                    action="channel_identity.unlinked",
                    target_type="channel_identity",
                    target_id=f"{row.channel}:{row.external_id}",
                )
            )

    async def _skills_of(
        self, principal: Principal, org_id: uuid.UUID, workspace_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        """The skills that the user sees in a workspace: builtin, org, workspace, and user."""
        if self._skills is None:
            raise HTTPException(status_code=409, detail="skills are not set up")
        async with as_user(self._engine, principal.auth_user_id) as connection:
            user_id = await _user_id(connection, principal, org_id)
            grants = None if user_id is None else await load_grants(connection, org_id, user_id)
            if user_id is None or grants is None or not grants.sees_workspace(workspace_id):
                raise _not_found()
            docs = await self._skills.catalog(
                connection, org_id=org_id, workspace_id=workspace_id, user_id=user_id
            )
        return [
            {
                "id": None if doc.skill_id is None else str(doc.skill_id),
                "name": doc.name,
                "description": doc.description,
                "scope": doc.scope,
                "version": doc.version,
                "tags": list(doc.tags),
            }
            for doc in docs
        ]

    async def _skill_versions(
        self, principal: Principal, skill_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        """The versions of a skill, with who changed it and in which run. RLS hides the skills
        that the user cannot read."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            # The skill comes first, under RLS. Its org then bounds the version rows.
            org_id = (
                await connection.execute(select(Skill.org_id).where(Skill.id == skill_id))
            ).scalar_one_or_none()
            if org_id is None:
                raise _not_found("the skill does not exist")
            rows = (
                await connection.execute(
                    select(
                        SkillVersion.version_no,
                        SkillVersion.description,
                        SkillVersion.change,
                        SkillVersion.summary,
                        SkillVersion.author_kind,
                        SkillVersion.author_user_id,
                        SkillVersion.session_id,
                        SkillVersion.run_id,
                        SkillVersion.created_at,
                    )
                    .where(SkillVersion.org_id == org_id, SkillVersion.skill_id == skill_id)
                    .order_by(SkillVersion.version_no.desc())
                )
            ).all()
        return [
            {
                "version": row.version_no,
                "description": row.description,
                "change": row.change,
                "summary": row.summary,
                "author_kind": row.author_kind,
                "author_user_id": None if row.author_user_id is None else str(row.author_user_id),
                "session_id": None if row.session_id is None else str(row.session_id),
                "run_id": None if row.run_id is None else str(row.run_id),
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]

    async def _expand_skill(self, access: SessionAccess, text: str) -> str:
        """The text of a skill command (Section 9.5), or ``text`` if it is not one."""
        if self._skills is None or not text.lstrip().startswith("/"):
            return text
        session = access.session
        async with self._engine.connect() as connection:
            actor = await self._skills.actor(
                connection,
                org_id=session.org_id,
                workspace_id=session.workspace_id,
                profile_id=session.agent_profile_id,
                user_id=access.user_id,
                private=session.audience == "private",
            )
        return await self._skills.expand(actor, text.strip()) or text

    async def _send(
        self, principal: Principal, session_id: uuid.UUID, body: NewMessage
    ) -> JSONResponse:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            access = await session_access(connection, principal, session_id)
        text = await self._expand_skill(access, body.text)
        try:
            run_id = await self._queue.enqueue(
                org_id=access.org_id, session_id=session_id, text=text, user_id=access.user_id
            )
        except RateLimitedError as error:
            return JSONResponse(
                status_code=429,
                content={"detail": str(error)},
                headers={"Retry-After": str(error.retry_after_s)},
            )
        except QueueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return JSONResponse(status_code=202, content={"run_id": str(run_id)})

    async def _cancel(self, principal: Principal, run_id: uuid.UUID) -> dict[str, str]:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            session_id = (
                await connection.execute(select(Run.session_id).where(Run.id == run_id))
            ).scalar_one_or_none()
            if session_id is None:
                raise _not_found("the run does not exist")
            access = await session_access(connection, principal, session_id)
        try:
            status = await self._queue.cancel(org_id=access.org_id, run_id=run_id)
        except QueueError:
            raise _not_found("the run does not exist") from None
        return {"status": status}

    async def _approval_target(
        self, principal: Principal, approval_id: uuid.UUID
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]:
        """The org, the user row, the run, and the call id of an approval that the user may
        decide. A user who reads the session decides here. The proposal of a background review
        is also open to the workspace admin and the org owner, who may not read the private
        session (Section 9.4); the gate checks their role. Raises ``404``."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            row = (
                await connection.execute(
                    select(Approval.session_id, Approval.run_id, Approval.call_id).where(
                        Approval.id == approval_id
                    )
                )
            ).one_or_none()
            if row is not None:
                try:
                    access = await session_access(connection, principal, row.session_id)
                except HTTPException as error:
                    if error.status_code != 404:
                        raise
                else:
                    return access.org_id, access.user_id, row.run_id, row.call_id
        proposal = await self._review_approval(principal, approval_id)
        if proposal is None:
            raise _not_found("the approval does not exist")
        return proposal

    async def _review_approval(
        self, principal: Principal, approval_id: uuid.UUID
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str] | None:
        """The approval of a review proposal in a workspace that the user sees. It reads as
        the table owner, because the user may not read the session. It gives ids only."""
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(Approval.org_id, Approval.run_id, Approval.call_id, Session.workspace_id)
                    .join(
                        ReviewProposal,
                        and_(
                            ReviewProposal.org_id == Approval.org_id,
                            ReviewProposal.approval_id == Approval.id,
                        ),
                    )
                    .join(
                        Session,
                        and_(Session.org_id == Approval.org_id, Session.id == Approval.session_id),
                    )
                    .where(Approval.id == approval_id)
                )
            ).one_or_none()
            if row is None:
                return None
            user_id = await _user_id(connection, principal, row.org_id)
            if user_id is None:
                return None
            grants = await load_grants(connection, row.org_id, user_id)
            if not grants.sees_workspace(row.workspace_id):
                return None
        return row.org_id, user_id, row.run_id, row.call_id

    async def review_proposals(
        self, principal: Principal, workspace_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        """The open proposals of background reviews that the user may decide in a workspace:
        ``agent`` and ``workspace`` scope for a workspace admin, ``org`` scope for the org
        owner. The user need not read the session. The answer has the redacted proposal and
        no session, run, or message (Section 9.4)."""
        async with self._engine.connect() as connection:
            org_id = (
                await connection.execute(
                    select(Workspace.org_id).where(Workspace.id == workspace_id)
                )
            ).scalar_one_or_none()
            user_id = None if org_id is None else await _user_id(connection, principal, org_id)
            if org_id is None or user_id is None:
                raise _not_found("the workspace does not exist")
            grants = await load_grants(connection, org_id, user_id)
            if not grants.sees_workspace(workspace_id):
                raise _not_found("the workspace does not exist")
            rows = await connection.execute(
                select(
                    ReviewProposal.id,
                    ReviewProposal.kind,
                    ReviewProposal.scope,
                    ReviewProposal.payload,
                    Approval.id.label("approval_id"),
                    Approval.created_at,
                    Approval.expires_at,
                )
                .join(
                    Approval,
                    and_(
                        Approval.org_id == ReviewProposal.org_id,
                        Approval.id == ReviewProposal.approval_id,
                    ),
                )
                .join(
                    Session,
                    and_(
                        Session.org_id == ReviewProposal.org_id,
                        Session.id == ReviewProposal.session_id,
                    ),
                )
                .where(
                    ReviewProposal.org_id == org_id,
                    Session.workspace_id == workspace_id,
                    ReviewProposal.status == "pending",
                    Approval.status == "pending",
                )
                .order_by(Approval.created_at, Approval.id)
            )
            # The stored text is not shown. The redactor of the process may know more secrets
            # now, so the view is made again from the payload (Section 9.4).
            views = [
                (row, shown_proposal(row.kind, row.scope, row.payload))
                for row in rows
                if (
                    grants.org_owner
                    if row.scope == "org"
                    else grants.admins_workspace(workspace_id)
                )
            ]
            return [
                {
                    "id": str(row.id),
                    "approval_id": str(row.approval_id),
                    "kind": row.kind,
                    "scope": row.scope,
                    "summary": shown.reason,
                    "proposal": shown.payload,
                    "created_at": row.created_at.isoformat(),
                    "expires_at": row.expires_at.isoformat(),
                }
                for row, shown in views
            ]

    async def _decide(
        self,
        principal: Principal,
        approval_id: uuid.UUID,
        approve: bool,
        always_allow: bool,
        reason: str | None,
    ) -> JSONResponse:
        org_id, user_id, run_id, call_id = await self._approval_target(principal, approval_id)
        try:
            status = await self._gate.decide_approval(
                org_id=org_id,
                run_id=run_id,
                call_id=call_id,
                user_id=user_id,
                approve=approve,
                always_allow=always_allow,
                reason=reason,
            )
        except ApprovalExpiredError as error:
            # An expired approval resumes the run as a reject.
            await self._queue.release_decided(org_id=org_id, run_id=run_id)
            await self._apply_reviews(org_id, run_id)
            return JSONResponse(status_code=409, content={"detail": str(error)})
        except ApprovalError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        resumed = await self._queue.release_decided(org_id=org_id, run_id=run_id)
        await self._apply_reviews(org_id, run_id)
        return JSONResponse(content={"status": status, "resumed": bool(resumed)})

    async def _apply_reviews(self, org_id: uuid.UUID, run_id: uuid.UUID) -> None:
        """Applies a decided proposal of a background review at once (Section 9.4). The worker
        does the same in its maintenance round, so a failure here only delays the change."""
        if self._reviews is None:
            return
        try:
            await self._reviews.apply_decided(org_id=org_id, run_id=run_id)
        except Exception:
            log.exception("the review proposals could not be applied run=%s", run_id)

    async def _answer(
        self, principal: Principal, session_id: uuid.UUID, request_id: str, answers: Sequence[str]
    ) -> None:
        async with as_user(self._engine, principal.auth_user_id) as connection:
            access = await session_access(connection, principal, session_id)
        broker = self._clarify
        if broker is None or request_id not in {
            r.id for r in broker.pending(str(access.org_id), str(session_id))
        }:
            raise _not_found("the question is not open")
        try:
            broker.answer(str(access.org_id), request_id, answers)
        except ClarifyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    # -- the event stream -------------------------------------------------------------------

    def _open_stream(self, user: uuid.UUID) -> Callable[[], None]:
        """Takes one stream slot of the user, or raises ``429``. Returns the release."""
        settings = self._settings
        if (
            self._streams.total() >= settings.max_streams
            or self._streams[user] >= settings.max_streams_per_user
        ):
            raise HTTPException(
                status_code=429,
                detail="too many open event streams. Close a page and try again.",
                headers={"Retry-After": "5"},
            )
        self._streams[user] += 1
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self._streams[user] -= 1
                if self._streams[user] <= 0:
                    del self._streams[user]

        return release

    def _stream_lifetime(self, principal: Principal) -> float:
        """Seconds until the stream ends: ``max_stream_s``, or the token expiry if earlier."""
        lifetime = self._settings.max_stream_s
        if principal.expires_at is not None:
            lifetime = min(lifetime, principal.expires_at - time.time())
        return lifetime

    async def _stream(
        self, principal: Principal, session_id: uuid.UUID, cursor: int, lifetime: float
    ) -> AsyncIterator[str]:
        """Server-sent events of a session.

        - ``runs``: the first event. The status of each open run and of the recent runs. A
          client that connects again uses it to correct the runs that it shows as open.
        - ``run_event``: one stored run event. The SSE id is the ``run_events.id``. One worker
          writes the events of a session at a time, so the ids of a session grow in the order
          of the commits.
        - ``run``: the status of a run changed.
        - ``end``: the user cannot read the session any more. The stream ends.

        Each poll checks the access again, with both guards. The stream ends when the access
        token expires, so that a stream does not live longer than its token.
        """
        settings = self._settings
        loop = asyncio.get_running_loop()
        deadline = loop.time() + lifetime
        next_ping = loop.time() + settings.heartbeat_s
        statuses: dict[uuid.UUID, str] = {}
        first = True
        yield "retry: 2000\n\n"
        while loop.time() < deadline:
            out: list[str] = []
            async with as_user(self._engine, principal.auth_user_id) as connection:
                try:
                    access = await session_access(connection, principal, session_id)
                except HTTPException:
                    yield _sse("end", {"reason": "the session is not visible"})
                    return
                events = await connection.execute(
                    select(
                        RunEvent.id,
                        RunEvent.run_id,
                        RunEvent.seq,
                        RunEvent.type,
                        RunEvent.payload,
                    )
                    .where(
                        RunEvent.org_id == access.org_id,
                        RunEvent.session_id == session_id,
                        RunEvent.run_id.in_(_chat_runs(session_id)),
                        RunEvent.id > cursor,
                    )
                    .order_by(RunEvent.id)
                    .limit(STREAM_BATCH)
                )
                for event in events:
                    cursor = event.id
                    out.append(
                        _sse(
                            "run_event",
                            {
                                "run_id": str(event.run_id),
                                "seq": event.seq,
                                "type": event.type,
                                "payload": event.payload,
                            },
                            event.id,
                        )
                    )
                runs = await self._stream_runs(connection, access.org_id, session_id, statuses)
                if first:
                    out.insert(0, _sse("runs", {"runs": [_run_out(run) for run in runs]}))
                changed = [run for run in runs if statuses.get(run.id) != run.status]
                statuses.update((run.id, run.status) for run in runs)
                if not first:
                    out += [_sse("run", _run_out(run)) for run in changed]
            first = False
            if loop.time() >= deadline:
                return  # The token can have expired during the reads.
            for chunk in out:
                yield chunk
            if loop.time() >= next_ping:
                next_ping = loop.time() + settings.heartbeat_s
                yield ": ping\n\n"
            if len(out) < STREAM_BATCH:
                await asyncio.sleep(max(0.0, min(settings.poll_s, deadline - loop.time())))

    @staticmethod
    async def _stream_runs(
        connection: AsyncConnection,
        org_id: uuid.UUID,
        session_id: uuid.UUID,
        statuses: dict[uuid.UUID, str],
    ) -> list[Any]:
        """The recent runs, every open run, and each run that the stream last saw open.

        A run that the stream saw open stays in the poll until the stream sends its end
        status, also when more than ``RECENT_RUNS`` newer runs are in the session.
        """
        columns = (Run.id, Run.status, Run.failure_code, Run.failure_message)
        scope = (Run.org_id == org_id, Run.session_id == session_id, Run.kind == "chat")
        recent = await connection.execute(
            select(*columns)
            .where(*scope)
            .order_by(Run.created_at.desc(), Run.id.desc())
            .limit(RECENT_RUNS)
        )
        watched = [run_id for run_id, status in statuses.items() if status in OPEN_RUN_STATUSES]
        rows = {run.id: run for run in recent}
        older = await connection.execute(
            select(*columns).where(
                *scope,
                or_(Run.status.in_(OPEN_RUN_STATUSES), Run.id.in_(watched)),
                Run.id.not_in(list(rows)),
            )
        )
        rows.update((run.id, run) for run in older)
        return list(rows.values())
