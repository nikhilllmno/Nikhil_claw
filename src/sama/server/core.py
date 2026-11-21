"""The operations of sama as a server (docs/architecture.md, Section 10.7, M7.6).

The OpenAI-compatible endpoint and the sama MCP server for other agents share this code. Each
call takes the ``KeyContext`` of a checked API key. The key acts as its user in one workspace,
so the code uses the same guards as the HTTP API: the session rules of the user (code and
RLS), the run queue with its rate limits, quotas, and profile budget, and the tool gate.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api.app import session_access
from sama.api.auth import Principal
from sama.apikeys.store import KeyContext
from sama.db.engine import as_user
from sama.db.models import (
    ENDED_RUN_STATUSES,
    AgentProfile,
    AuditEntry,
    Message,
    ModelCall,
    Run,
    Session,
)
from sama.gateway.sessions import final_text, read_run
from sama.orchestrator.queue import QueueError, QuotaExceededError, RateLimitedError, RunQueue
from sama.scheduler.jobs import Actor as JobActor
from sama.search import MAX_LIMIT, MAX_QUERY_CHARS, SessionSearch, SessionSearchError
from sama.skills.service import SkillLibrary
from sama.workflows.service import WorkflowService
from sama.workflows.spec import WorkflowError, WorkflowNotFoundError

MAX_TEXT_CHARS: Final = 32_000
"""The longest message, the same limit as ``POST /sessions/{id}/messages``."""
TITLE_CHARS: Final = 80
READ_MAX_MESSAGES: Final = 50
READ_MESSAGE_CHARS: Final = 8_000
DEFAULT_MODEL: Final = "sama"
"""The model name that means the default agent profile of the key."""


class ServerSettings(BaseModel):
    """The limits of sama as a server. Each limit is per process."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    poll_s: float = Field(default=0.5, gt=0)
    """How often a waiting request reads the run."""
    max_wait_s: float = Field(default=1800.0, gt=0)
    """The longest time that one chat completion waits for its run."""
    keepalive_s: float = Field(default=15.0, gt=0)
    """A waiting answer sends a byte at least this often, so a proxy keeps the connection."""
    first_wait_s: float = Field(default=20.0, ge=0)
    """A chat completion without ``stream`` waits this long for a normal answer. Then it starts
    the answer and sends spaces until the run ends."""
    max_waiters: int = Field(default=200, ge=1)
    """The most requests in this process that wait for a run at one time."""
    max_waiters_per_key: int = Field(default=16, ge=1)


class ServerError(Exception):
    """A refused request. ``status`` is the HTTP status, ``code`` a short machine name. The
    message is safe to show."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retry_after_s: int | None = None,
        param: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after_s = retry_after_s
        self.param = param


@dataclass(frozen=True, slots=True)
class Profile:
    id: uuid.UUID
    name: str
    model: str
    runtime: str
    created_at: float
    model_id: str = ""
    """The name that the OpenAI-compatible endpoint uses for the profile."""


@dataclass(frozen=True, slots=True)
class Approval:
    tool: str
    reason: str


@dataclass(slots=True)
class RunResult:
    """A run of a key, as the endpoints show it."""

    run_id: uuid.UUID
    session_id: uuid.UUID
    status: str
    text: str = ""
    failure_code: str | None = None
    failure_message: str | None = None
    approvals: list[Approval] = field(default_factory=list)

    @property
    def ended(self) -> bool:
        return self.status in ENDED_RUN_STATUSES

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "run_id": str(self.run_id),
            "session_id": str(self.session_id),
            "status": self.status,
        }
        if self.status == "completed":
            body["reply"] = self.text
        if self.failure_code is not None or self.status == "failed":
            body["failure"] = {"code": self.failure_code, "message": self.failure_message}
        if self.approvals:
            body["waiting_for"] = [
                {"tool": item.tool, "reason": item.reason} for item in self.approvals
            ]
            body["note"] = (
                "The run waits for a person to decide the approval in the sama web app. "
                "Ask again later with run_get."
            )
        elif not self.ended:
            body["note"] = "The run did not end yet. Ask again later with run_get."
        return body


def principal(key: KeyContext) -> Principal:
    """The user of the key as a signed-in user, for the checks that the API shares."""
    return Principal(key.auth_user_id, None)


def model_ids(profiles: list[Profile]) -> list[Profile]:
    """Gives each profile its model id: the name when no other profile has the same name (in
    any case), else the id."""
    counts = Counter(profile.name.casefold() for profile in profiles)
    return [
        Profile(
            id=profile.id,
            name=profile.name,
            model=profile.model,
            runtime=profile.runtime,
            created_at=profile.created_at,
            model_id=profile.name if counts[profile.name.casefold()] == 1 else str(profile.id),
        )
        for profile in profiles
    ]


class ServerCore:
    """The shared operations of the OpenAI-compatible endpoint and the MCP server."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        queue: RunQueue | None = None,
        search: SessionSearch | None = None,
        skills: SkillLibrary | None = None,
        workflows: WorkflowService | None = None,
        settings: ServerSettings | None = None,
    ) -> None:
        self._engine = engine
        self._queue = queue or RunQueue(engine)
        self._search = search or SessionSearch(engine)
        self._skills = skills
        self._workflows = workflows
        self.settings = settings or ServerSettings()
        self._waiters: Counter[uuid.UUID] = Counter()

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    # -- limits -----------------------------------------------------------------------------

    def hold_waiter(self, key: KeyContext) -> Callable[[], None]:
        """Takes one place for a request that waits for a run. Returns the call that gives the
        place back. Raises ``ServerError`` (``429``) when the process or the key is full."""
        total = sum(self._waiters.values())
        if total >= self.settings.max_waiters:
            raise ServerError(
                429, "rate_limit_exceeded", "sama is busy; try again soon", retry_after_s=5
            )
        if self._waiters[key.key_id] >= self.settings.max_waiters_per_key:
            raise ServerError(
                429,
                "rate_limit_exceeded",
                f"this key waits for {self.settings.max_waiters_per_key} runs already",
                retry_after_s=5,
            )
        self._waiters[key.key_id] += 1
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            self._waiters[key.key_id] -= 1
            if self._waiters[key.key_id] <= 0:
                del self._waiters[key.key_id]

        return release

    # -- profiles ---------------------------------------------------------------------------

    async def profiles(self, key: KeyContext) -> list[Profile]:
        """The agent profiles that the key can use, oldest first. A key with a profile has
        only that profile."""
        query = select(
            AgentProfile.id,
            AgentProfile.name,
            AgentProfile.model,
            AgentProfile.runtime,
            AgentProfile.created_at,
        ).where(
            AgentProfile.org_id == key.org_id,
            or_(
                AgentProfile.workspace_id.is_(None),
                AgentProfile.workspace_id == key.workspace_id,
            ),
        )
        if key.agent_profile_id is not None:
            query = query.where(AgentProfile.id == key.agent_profile_id)
        async with as_user(self._engine, key.auth_user_id) as connection:
            rows = (
                await connection.execute(query.order_by(AgentProfile.created_at, AgentProfile.id))
            ).all()
        return model_ids(
            [
                Profile(row.id, row.name, row.model, row.runtime, row.created_at.timestamp())
                for row in rows
            ]
        )

    async def resolve(self, key: KeyContext, model: str | None) -> Profile:
        """The profile for a model name: ``sama`` (or none) is the default profile, else a
        model id of ``profiles``, a profile name (any case), or a profile id."""
        profiles = await self.profiles(key)
        if not profiles:
            raise ServerError(404, "model_not_found", "the key has no agent profile to use")
        wanted = (model or "").strip()
        if wanted in ("", DEFAULT_MODEL):
            return profiles[0]
        for profile in profiles:
            if wanted in (profile.model_id, str(profile.id)):
                return profile
        named = [profile for profile in profiles if profile.name.casefold() == wanted.casefold()]
        if len(named) == 1:
            return named[0]
        if named:
            raise ServerError(
                400,
                "model_not_found",
                "more than one agent profile has this name; use the id from /v1/models",
                param="model",
            )
        raise ServerError(
            404, "model_not_found", f"the model {wanted[:80]!r} does not exist", param="model"
        )

    # -- sessions ---------------------------------------------------------------------------

    async def open_session(
        self, key: KeyContext, profile: Profile, *, title: str | None, channel: str
    ) -> uuid.UUID:
        """Makes a private session of the key user with the profile, in the workspace of the
        key. The web app shows it to the user."""
        session_id = uuid.uuid4()
        clean = " ".join((title or "").split())[:TITLE_CHARS] or None
        async with self._engine.begin() as connection:
            await connection.execute(
                insert(Session).values(
                    id=session_id,
                    org_id=key.org_id,
                    workspace_id=key.workspace_id,
                    agent_profile_id=profile.id,
                    audience="private",
                    owner_user_id=key.user_id,
                    title=clean,
                )
            )
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=key.org_id,
                    workspace_id=key.workspace_id,
                    actor_kind="user",
                    actor_user_id=key.user_id,
                    action="session.created",
                    target_type="session",
                    target_id=str(session_id),
                    details={
                        "audience": "private",
                        "channel": channel,
                        "api_key_id": str(key.key_id),
                    },
                )
            )
        return session_id

    async def session(self, key: KeyContext, session_id: uuid.UUID) -> Any:
        """The session row, when the key can use it: the user of the key can read it (both
        guards), it is in the workspace of the key, and it has a profile of the key. Else
        ``404``, the same answer as for a session that does not exist."""
        async with as_user(self._engine, key.auth_user_id) as connection:
            try:
                access = await session_access(connection, principal(key), session_id)
            except HTTPException:
                raise ServerError(404, "session_not_found", "the session does not exist") from None
        row = access.session
        if row.workspace_id != key.workspace_id or (
            key.agent_profile_id is not None and row.agent_profile_id != key.agent_profile_id
        ):
            raise ServerError(404, "session_not_found", "the session does not exist")
        return row

    async def send(self, key: KeyContext, session_id: uuid.UUID, text: str) -> uuid.UUID:
        """Queues the message as the user of the key and returns the run id. A key with the
        approvals ``deny`` makes an unattended run: the gate turns ``ask`` into ``deny``."""
        if not text.strip():
            raise ServerError(400, "invalid_request", "the message is empty")
        if len(text) > MAX_TEXT_CHARS:
            raise ServerError(
                400,
                "context_length_exceeded",
                f"the message has more than {MAX_TEXT_CHARS} characters; send the earlier "
                "messages once and continue the sama session",
            )
        try:
            return await self._queue.enqueue(
                org_id=key.org_id,
                session_id=session_id,
                text=text,
                user_id=key.user_id,
                unattended=key.unattended,
            )
        except RateLimitedError as error:
            raise ServerError(
                429, "rate_limit_exceeded", str(error), retry_after_s=error.retry_after_s
            ) from None
        except QuotaExceededError as error:
            raise ServerError(429, "insufficient_quota", str(error)) from None
        except QueueError as error:
            raise ServerError(409, "session_not_active", str(error)) from None

    # -- runs -------------------------------------------------------------------------------

    async def result(self, key: KeyContext, run_id: uuid.UUID) -> RunResult:
        """The state of a chat run in a session that the key can use. Else ``404``."""
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(Run.session_id, Run.status, Run.failure_code, Run.failure_message).where(
                        Run.org_id == key.org_id, Run.id == run_id, Run.kind == "chat"
                    )
                )
            ).one_or_none()
        if row is None:
            raise ServerError(404, "run_not_found", "the run does not exist")
        try:
            await self.session(key, row.session_id)
        except ServerError:
            raise ServerError(404, "run_not_found", "the run does not exist") from None
        return await self._result(key.org_id, run_id, row)

    async def _result(self, org_id: uuid.UUID, run_id: uuid.UUID, row: Any) -> RunResult:
        result = RunResult(
            run_id=run_id,
            session_id=row.session_id,
            status=row.status,
            failure_code=row.failure_code,
            failure_message=row.failure_message,
        )
        if row.status == "completed":
            result.text = await final_text(self._engine, org_id, run_id)
        elif row.status == "paused":
            read = await read_run(self._engine, org_id, run_id, 2**31 - 1)
            result.approvals = [
                Approval(str(item.tool_name), str(item.reason)) for item in read.pending
            ]
        return result

    async def wait(self, key: KeyContext, run_id: uuid.UUID, wait_s: float) -> RunResult:
        """Waits up to ``wait_s`` for the run to end, and returns its state."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, wait_s)
        result = await self.result(key, run_id)
        while not result.ended and loop.time() < deadline:
            await asyncio.sleep(min(self.settings.poll_s, max(0.0, deadline - loop.time())))
            result = await self.result(key, run_id)
        return result

    async def usage(self, org_id: uuid.UUID, run_id: uuid.UUID) -> tuple[int, int]:
        """The input and the output tokens of the model calls of the run."""
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(
                        func.coalesce(
                            func.sum(
                                ModelCall.input_tokens
                                + ModelCall.cache_read_tokens
                                + ModelCall.cache_write_tokens
                            ),
                            0,
                        ).label("input"),
                        func.coalesce(func.sum(ModelCall.output_tokens), 0).label("output"),
                    ).where(ModelCall.org_id == org_id, ModelCall.run_id == run_id)
                )
            ).one()
        return int(row.input), int(row.output)

    # -- reads ------------------------------------------------------------------------------

    async def read(
        self, key: KeyContext, session_id: uuid.UUID, *, after_seq: int, limit: int
    ) -> dict[str, Any]:
        """The user and assistant messages of a session after ``after_seq``, oldest first."""
        row = await self.session(key, session_id)
        limit = max(1, min(limit, READ_MAX_MESSAGES))
        async with as_user(self._engine, key.auth_user_id) as connection:
            messages = (
                await connection.execute(
                    select(Message.seq, Message.role, Message.content, Message.created_at)
                    .where(
                        Message.org_id == key.org_id,
                        Message.session_id == session_id,
                        Message.role.in_(("user", "assistant")),
                        Message.seq > after_seq,
                    )
                    .order_by(Message.seq)
                    .limit(limit + 1)
                )
            ).all()
            open_run = (
                await connection.execute(
                    select(Run.id, Run.status)
                    .where(
                        Run.org_id == key.org_id,
                        Run.session_id == session_id,
                        Run.kind == "chat",
                        Run.status.not_in(ENDED_RUN_STATUSES),
                    )
                    .order_by(Run.created_at)
                    .limit(1)
                )
            ).one_or_none()
        more = len(messages) > limit
        shown = messages[:limit]
        items = []
        for message in shown:
            text = str((message.content or {}).get("text") or "")
            cut = len(text) > READ_MESSAGE_CHARS
            items.append(
                {
                    "seq": message.seq,
                    "role": message.role,
                    "created_at": message.created_at.isoformat(),
                    "text": text[:READ_MESSAGE_CHARS] + (" [cut]" if cut else ""),
                }
            )
        body: dict[str, Any] = {
            "session_id": str(session_id),
            "title": row.title,
            "status": row.status,
            "messages": items,
            "more": more,
        }
        if shown:
            body["next_after_seq"] = shown[-1].seq
        if open_run is not None:
            body["open_run"] = {"run_id": str(open_run.id), "status": open_run.status}
        return body

    async def search(self, key: KeyContext, query: str, limit: int) -> list[dict[str, Any]]:
        """The sessions of the key workspace that the key user sees and that match the query.
        An empty query gives the latest sessions."""
        if len(query) > MAX_QUERY_CHARS:
            raise ServerError(
                400, "invalid_request", f"the query has more than {MAX_QUERY_CHARS} characters"
            )
        try:
            hits = await self._search.search_as_user(
                key.org_id,
                key.user_id,
                query.strip() or None,
                max(1, min(limit, MAX_LIMIT)),
                workspace_id=key.workspace_id,
            )
        except SessionSearchError as error:
            raise ServerError(400, "invalid_request", str(error)) from None
        return [
            {
                "session_id": str(hit.session_id),
                "title": hit.title,
                "audience": hit.audience,
                "updated_at": hit.updated_at.isoformat(),
                "snippets": [
                    {"role": snippet.role, "text": snippet.text} for snippet in hit.snippets
                ],
            }
            for hit in hits
        ]

    async def skills(self, key: KeyContext) -> list[dict[str, Any]]:
        """The skills that the key user sees in the workspace of the key."""
        if self._skills is None:
            raise ServerError(409, "not_available", "skills are off in this process")
        async with as_user(self._engine, key.auth_user_id) as connection:
            docs = await self._skills.catalog(
                connection,
                org_id=key.org_id,
                workspace_id=key.workspace_id,
                user_id=key.user_id,
            )
        return [
            {"name": doc.name, "description": doc.description, "scope": doc.scope} for doc in docs
        ]

    # -- workflows --------------------------------------------------------------------------

    def _workflow_actor(self, key: KeyContext) -> tuple[WorkflowService, JobActor]:
        if self._workflows is None:
            raise ServerError(409, "not_available", "workflows are off in this process")
        return self._workflows, JobActor(org_id=key.org_id, user_id=key.user_id, kind="user")

    async def workflows(self, key: KeyContext) -> list[dict[str, Any]]:
        """The workflows of the key user in the workspace of the key."""
        service, actor = self._workflow_actor(key)
        rows = await service.list(actor)
        return [row for row in rows if row.get("workspace_id") == str(key.workspace_id)]

    async def start_workflow(
        self, key: KeyContext, workflow_id: uuid.UUID, inputs: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Starts an instance of a workflow of the key user in the key workspace. A version
        that waits for a human approval does not start (Section 12.3)."""
        service, actor = self._workflow_actor(key)
        if str(workflow_id) not in {row["id"] for row in await self.workflows(key)}:
            raise ServerError(404, "workflow_not_found", "the workflow does not exist")
        try:
            return await service.start(actor, workflow_id, dict(inputs))
        except WorkflowNotFoundError:
            raise ServerError(404, "workflow_not_found", "the workflow does not exist") from None
        except WorkflowError as error:
            raise ServerError(400, "workflow_error", str(error)) from None

    async def workflow_instance(self, key: KeyContext, instance_id: uuid.UUID) -> dict[str, Any]:
        service, actor = self._workflow_actor(key)
        try:
            instance = await service.instance(actor, instance_id)
        except (WorkflowNotFoundError, WorkflowError):
            raise ServerError(404, "workflow_not_found", "the instance does not exist") from None
        workflows = {row["id"] for row in await self.workflows(key)}
        if str(instance.get("workflow_id")) not in workflows:
            raise ServerError(404, "workflow_not_found", "the instance does not exist")
        return instance
