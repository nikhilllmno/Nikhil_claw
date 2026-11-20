"""The parts of a chat channel that do not depend on the chat app (docs/architecture.md,
Section 11.1): the session of a chat, the stop of its runs, the loop that follows a run into a
growing reply, and the approval decisions from chat buttons.

The Telegram and the Slack gateways use them. A chat is a ``session_bindings`` row with the
channel and a key that holds the integration, so that equal chat ids of two bots do not mix.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.context import Pin, PinError, PinStore
from sama.db.models import (
    ENDED_RUN_STATUSES,
    AgentProfile,
    Approval,
    AuditEntry,
    Message,
    ReviewProposal,
    Run,
    RunEvent,
    Session,
    SessionBinding,
    SessionCompaction,
    User,
)
from sama.gate.approvers import Approvers, approvers_of
from sama.gate.service import ApprovalError, ApprovalExpiredError, GateService
from sama.orchestrator.queue import RunQueue
from sama.review.shown import shown_proposal
from sama.tenancy.access import Grants, load_grants

EVENTS_PER_READ: Final = 500
MAX_ARGUMENT_CHARS: Final = 1500
SPLIT_AT: Final = 4000
"""A reply longer than this goes on in a new message (Telegram accepts 4,096 characters)."""
MAX_FINAL_CHARS: Final = 16_000
"""The most text of a stored answer that a chat gets when the runtime sent no text deltas."""


class FollowSettings(BaseModel):
    """The settings that all chat gateways of a process share."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    poll_s: float = Field(default=0.5, gt=0)
    paused_poll_s: float = Field(default=5.0, gt=0)
    """The poll interval while a run waits for an approval."""
    edit_interval_s: float = Field(default=1.5, ge=0)
    """The shortest time between two edits of one reply (the chat apps limit edits)."""
    max_follow_s: float = Field(default=6 * 3600, gt=0)
    max_followers: int = Field(default=200, ge=1)
    """The most runs that this process follows at one time. When all places are in use, the
    gateway does not queue a new run and tells the chat to try again later."""


@dataclass(frozen=True, slots=True)
class Target:
    """Where the messages of one chat go."""

    org_id: uuid.UUID
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    agent_profile_id: uuid.UUID
    audience: str
    """``private`` for a direct chat, ``shared`` for a group, a channel, or a thread."""

    @classmethod
    def private(cls, identity: Any) -> Target:
        """The direct chat of a linked user (a ``channel_identities`` row)."""
        return cls(
            identity.org_id,
            identity.user_id,
            identity.workspace_id,
            identity.agent_profile_id,
            "private",
        )

    @property
    def owner_user_id(self) -> uuid.UUID | None:
        return self.user_id if self.audience == "private" else None


@dataclass(frozen=True, slots=True)
class Bound:
    session_id: uuid.UUID
    agent_profile_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class RunRead:
    """One read of a followed run."""

    status: str
    events: list[Any]
    """``seq``, ``type``, and ``payload`` of the new events, at most ``EVENTS_PER_READ``."""
    pending: list[Any]
    """``id``, ``tool_name``, ``reason``, and ``arguments`` of the open approvals."""

    @property
    def ended(self) -> bool:
        """True when the run ended and all its events were read."""
        return self.status in ENDED_RUN_STATUSES and len(self.events) < EVENTS_PER_READ


def split_point(text: str, limit: int = SPLIT_AT) -> int:
    """Where to cut ``text`` that is longer than ``limit``: a line end, else a space."""
    for mark in ("\n", " "):
        cut = text.rfind(mark, limit // 2, limit)
        if cut > 0:
            return cut + 1
    return limit


class Outbox(Protocol):
    """Sends and edits the messages of one chat. A message id is what ``send_message``
    returns: a number in Telegram, a ``ts`` in Slack."""

    async def send_message(self, text: str, *, reply_to: Any = None) -> Any: ...

    async def edit_message(self, message_id: Any, text: str) -> None: ...


class Reply:
    """One reply that grows as text arrives. It edits its message at most once in
    ``interval_s`` and goes on in a new message after ``SPLIT_AT`` characters."""

    def __init__(self, outbox: Outbox, reply_to: Any, interval_s: float) -> None:
        self._outbox = outbox
        self._reply_to = reply_to
        self._interval_s = interval_s
        self._text = ""
        self._shown = ""
        self._message_id: Any = None
        self._last: float | None = None
        """Loop time of the last shown text. ``None`` until the first text shows."""
        self.sent_any = False
        """True when text was shown since the start or the last ``restart``."""

    async def add(self, text: str) -> None:
        self._text += text
        while len(self._text) > SPLIT_AT:
            cut = split_point(self._text)
            await self._show(self._text[:cut])
            self._text = self._text[cut:]
            self._message_id, self._shown = None, ""
        await self.flush(final=False)

    async def flush(self, *, final: bool) -> None:
        loop = asyncio.get_running_loop()
        if final or self._last is None or loop.time() - self._last >= self._interval_s:
            await self._show(self._text)

    def restart(self) -> None:
        """The next text goes in a new message, for example after an approval card."""
        self._text, self._shown, self._message_id = "", "", None
        self.sent_any = False

    async def _show(self, text: str) -> None:
        if not text.strip() or text == self._shown:
            return
        if self._message_id is None:
            self._message_id = await self._outbox.send_message(text, reply_to=self._reply_to)
        else:
            await self._outbox.edit_message(self._message_id, text)
        self._shown = text
        self._last = asyncio.get_running_loop().time()
        self.sent_any = True


async def find_session(
    engine: AsyncEngine, channel: str, key: str, target: Target, grants: Grants
) -> Bound | None:
    """The session of the chat, when the user can use it now in the same place."""
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                select(
                    Session.id,
                    Session.org_id,
                    Session.workspace_id,
                    Session.audience,
                    Session.owner_user_id,
                    Session.status,
                    Session.agent_profile_id,
                )
                .join(
                    SessionBinding,
                    (SessionBinding.org_id == Session.org_id)
                    & (SessionBinding.session_id == Session.id),
                )
                .where(SessionBinding.channel == channel, SessionBinding.external_key == key)
            )
        ).one_or_none()
    if (
        row is not None
        and row.status == "active"
        and row.org_id == target.org_id
        and row.workspace_id == target.workspace_id
        and row.audience == target.audience
        and grants.sees_session(row.workspace_id, row.audience, row.owner_user_id)
    ):
        return Bound(row.id, row.agent_profile_id)
    return None


async def new_session(
    engine: AsyncEngine,
    channel: str,
    key: str,
    target: Target,
    agent_profile_id: uuid.UUID,
    *,
    parent: uuid.UUID | None = None,
) -> Bound:
    """Makes a session and binds the chat to it."""
    session_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(Session).values(
                id=session_id,
                org_id=target.org_id,
                workspace_id=target.workspace_id,
                agent_profile_id=agent_profile_id,
                audience=target.audience,
                owner_user_id=target.owner_user_id,
                parent_session_id=parent,
                lineage=None if parent is None else "branch",
            )
        )
        await _bind(connection, channel, key, target, session_id)
        await connection.execute(
            insert(AuditEntry).values(
                org_id=target.org_id,
                workspace_id=target.workspace_id,
                actor_kind="user",
                actor_user_id=target.user_id,
                action="session.created",
                target_type="session",
                target_id=str(session_id),
                details={"audience": target.audience, "channel": channel},
            )
        )
    return Bound(session_id, agent_profile_id)


async def stop_runs(
    engine: AsyncEngine, queue: RunQueue, org_id: uuid.UUID, session_ids: list[uuid.UUID]
) -> int:
    """Cancels the open runs of the sessions and returns how many."""
    if not session_ids:
        return 0
    async with engine.connect() as connection:
        run_ids = (
            await connection.scalars(
                select(Run.id).where(
                    Run.org_id == org_id,
                    Run.session_id.in_(session_ids),
                    Run.status.not_in(ENDED_RUN_STATUSES),
                )
            )
        ).all()
    for run_id in run_ids:
        await queue.cancel(org_id=org_id, run_id=run_id)
    return len(run_ids)


async def read_run(
    engine: AsyncEngine, org_id: uuid.UUID, run_id: uuid.UUID, after_seq: int
) -> RunRead:
    async with engine.connect() as connection:
        # The status first: the events of an ended run are all stored before it.
        status = (
            await connection.execute(
                select(Run.status).where(Run.org_id == org_id, Run.id == run_id)
            )
        ).scalar_one()
        events = (
            await connection.execute(
                select(RunEvent.seq, RunEvent.type, RunEvent.payload)
                .where(
                    RunEvent.org_id == org_id,
                    RunEvent.run_id == run_id,
                    RunEvent.seq > after_seq,
                )
                .order_by(RunEvent.seq)
                .limit(EVENTS_PER_READ)
            )
        ).all()
        pending = (
            []
            if status != "paused"
            else (
                await connection.execute(
                    select(
                        Approval.id,
                        Approval.tool_name,
                        Approval.reason,
                        Approval.arguments,
                        ReviewProposal.kind.label("proposal_kind"),
                        ReviewProposal.scope.label("proposal_scope"),
                        ReviewProposal.payload.label("proposal_payload"),
                        Session.audience,
                        AgentProfile.policy,
                        User.display_name.label("requester_name"),
                        Run.triggered_by_user_id.label("requester_id"),
                    )
                    .join(Run, (Run.org_id == Approval.org_id) & (Run.id == Approval.run_id))
                    .join(
                        Session,
                        (Session.org_id == Approval.org_id) & (Session.id == Approval.session_id),
                    )
                    .join(
                        AgentProfile,
                        (AgentProfile.org_id == Run.org_id)
                        & (AgentProfile.id == Run.agent_profile_id),
                    )
                    .outerjoin(
                        User,
                        (User.org_id == Run.org_id) & (User.id == Run.triggered_by_user_id),
                    )
                    .outerjoin(
                        ReviewProposal,
                        (ReviewProposal.org_id == Approval.org_id)
                        & (ReviewProposal.approval_id == Approval.id),
                    )
                    .where(
                        Approval.org_id == org_id,
                        Approval.run_id == run_id,
                        Approval.status == "pending",
                    )
                )
            ).all()
        )
        shown = [_shown_approval(row) for row in pending]
    return RunRead(status, list(events), shown)


async def final_text(engine: AsyncEngine, org_id: uuid.UUID, run_id: uuid.UUID) -> str:
    """The stored answer of a run, for a runtime that sends no text deltas."""
    async with engine.connect() as connection:
        output = (
            await connection.execute(
                select(Message.content)
                .where(
                    Message.org_id == org_id,
                    Message.run_id == run_id,
                    Message.role == "assistant",
                )
                .order_by(Message.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return str((output or {}).get("text") or "")


async def compaction_text(engine: AsyncEngine, org_id: uuid.UUID, run_id: uuid.UUID) -> str:
    """The result of a ``/compact`` run for a chat, or an empty text for a run that did not
    compact."""
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                select(SessionCompaction.tokens_before, SessionCompaction.tokens_after)
                .where(
                    SessionCompaction.org_id == org_id,
                    SessionCompaction.run_id == run_id,
                    SessionCompaction.completed_at.is_not(None),
                )
                .order_by(SessionCompaction.created_at.desc())
                .limit(1)
            )
        ).one_or_none()
    if row is None:
        return ""
    if row.tokens_before is not None and row.tokens_after is not None:
        return (
            f"The context is compacted: {row.tokens_before} tokens before and "
            f"{row.tokens_after} tokens now."
        )
    return "The context is compacted."


async def failure_message(engine: AsyncEngine, org_id: uuid.UUID, run_id: uuid.UUID) -> str:
    async with engine.connect() as connection:
        message = (
            await connection.execute(
                select(Run.failure_message).where(Run.org_id == org_id, Run.id == run_id)
            )
        ).scalar_one_or_none()
    return f"The run failed: {message or 'unknown'}"


@dataclass(frozen=True, slots=True)
class ShownApproval:
    """``id``, ``tool_name``, ``reason``, and ``arguments`` for an approval card.
    ``answerers`` tells a group who may decide it, or is ``None`` where it is not needed."""

    id: uuid.UUID
    tool_name: str
    reason: str
    arguments: Any
    answerers: str | None = None


MAX_NAME_CHARS: Final = 60


def answerers_text(
    requester: str | None, approvers: Approvers, *, has_requester: bool = True
) -> str:
    """Who may decide an approval of a shared session (Section 11.3). It names the person who
    asked (or says so without a name), the approvers that the profile lists, and the admins
    of the workspace and of the org."""
    if approvers.any_member:
        return "Each member of the workspace can decide this."
    who: list[str] = []
    if has_requester:
        name = " ".join((requester or "").split())[:MAX_NAME_CHARS]
        who.append(f"{name} (who asked)" if name else "the person who asked")
    if approvers.users:
        who.append("a person who is listed as an approver")
    who.append("a workspace or org admin")
    text = who[0] if len(who) == 1 else f"{', '.join(who[:-1])} or {who[-1]}"
    return f"{text[0].upper()}{text[1:]} can decide this."


def _shown_approval(row: Any) -> ShownApproval:
    """The card of a proposal of a background review is made again from the payload with the
    redactor of now, and not from the stored text (Section 9.4)."""
    reason, arguments = row.reason, row.arguments
    if row.proposal_payload is not None:
        shown = shown_proposal(row.proposal_kind, row.proposal_scope, row.proposal_payload)
        return ShownApproval(row.id, row.tool_name, shown.reason, shown.arguments)
    answerers = (
        answerers_text(
            row.requester_name,
            approvers_of(row.policy),
            has_requester=row.requester_id is not None,
        )
        if row.audience == "shared"
        else None
    )
    return ShownApproval(row.id, row.tool_name, reason, arguments, answerers)


def approval_text(row: Any) -> str:
    """The text of an approval card: the tool, the reason, and the arguments (cut)."""
    arguments = json.dumps(row.arguments, ensure_ascii=False, indent=1)
    if len(arguments) > MAX_ARGUMENT_CHARS:
        arguments = arguments[:MAX_ARGUMENT_CHARS] + "\n..."
    head = f"Approval needed: {row.tool_name}\n{row.reason}"
    if row.answerers:
        head = f"{head}\n{row.answerers}"
    return f"{head}\n\n{arguments}".strip()


async def _bind(
    connection: AsyncConnection, channel: str, key: str, target: Target, session_id: uuid.UUID
) -> None:
    statement = pg_insert(SessionBinding).values(
        org_id=target.org_id,
        workspace_id=target.workspace_id,
        session_id=session_id,
        channel=channel,
        external_key=key,
        created_by_user_id=target.user_id,
    )
    await connection.execute(
        statement.on_conflict_do_update(
            index_elements=["channel", "external_key"],
            set_={
                "org_id": statement.excluded.org_id,
                "workspace_id": statement.excluded.workspace_id,
                "session_id": statement.excluded.session_id,
                "created_by_user_id": statement.excluded.created_by_user_id,
                "updated_at": func.now(),
            },
        )
    )


async def follow_run(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    reply: Reply,
    settings: FollowSettings,
    send_card: Callable[[Any], Awaitable[None]],
) -> str:
    """Shows the text of the run in ``reply`` and gives each new open approval to
    ``send_card``, until the run ends or ``max_follow_s`` passes. Returns the last status."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.max_follow_s
    seq = -1
    carded: set[uuid.UUID] = set()
    status = "queued"
    while loop.time() < deadline:
        read = await read_run(engine, org_id, run_id, seq)
        status = read.status
        for event in read.events:
            seq = event.seq
            if event.type == "text_delta":
                await reply.add(str(event.payload.get("text", "")))
        new = [row for row in read.pending if row.id not in carded]
        if new:
            await reply.flush(final=True)
            reply.restart()
            for row in new:
                await send_card(row)
                carded.add(row.id)
        if read.ended:
            break
        await reply.flush(final=False)
        await asyncio.sleep(settings.paused_poll_s if status == "paused" else settings.poll_s)
    await reply.flush(final=True)
    return status


async def send_ending(
    engine: AsyncEngine,
    outbox: Outbox,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    status: str,
    reply: Reply,
) -> None:
    """The last messages of a run: the stored answer when no text showed, the failure, or the
    stop."""
    if status == "completed" and not reply.sent_any:
        # A runtime that sends no text deltas: show the stored answer. A ``/compact`` run has
        # no answer, so it shows the result of the compaction.
        text = (await final_text(engine, org_id, run_id))[:MAX_FINAL_CHARS]
        text = text or await compaction_text(engine, org_id, run_id)
        for start in range(0, len(text), SPLIT_AT):
            await outbox.send_message(text[start : start + SPLIT_AT])
    elif status == "failed":
        await outbox.send_message(await failure_message(engine, org_id, run_id))
    elif status == "cancelled":
        await outbox.send_message("The run stopped.")


@dataclass(frozen=True, slots=True)
class Decided:
    outcome: str
    """``Approved``, ``Denied``, or ``Expired``."""
    tool_name: str


async def decide(
    engine: AsyncEngine,
    gate: GateService,
    queue: RunQueue,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    approval_id: uuid.UUID,
    approve: bool,
) -> Decided:
    """Records the decision of a linked chat user on an approval of a session that the user can
    see. Raises ``ApprovalError`` with a message that is safe to show."""
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                select(
                    Approval.run_id,
                    Approval.call_id,
                    Approval.tool_name,
                    Session.workspace_id,
                    Session.audience,
                    Session.owner_user_id,
                )
                .join(
                    Session,
                    (Session.org_id == Approval.org_id) & (Session.id == Approval.session_id),
                )
                .where(Approval.org_id == org_id, Approval.id == approval_id)
            )
        ).one_or_none()
        grants = await load_grants(connection, org_id, user_id)
    if row is None or not grants.sees_session(row.workspace_id, row.audience, row.owner_user_id):
        raise ApprovalError("You cannot decide this approval.")
    try:
        await gate.decide_approval(
            org_id=org_id, run_id=row.run_id, call_id=row.call_id, user_id=user_id, approve=approve
        )
        outcome = "Approved" if approve else "Denied"
    except ApprovalExpiredError:
        outcome = "Expired"
    await queue.release_decided(org_id=org_id, run_id=row.run_id)
    return Decided(outcome, row.tool_name)


async def wait_for_tasks(tasks: set[asyncio.Task[None]]) -> None:
    """Waits until every task in ``tasks`` is done, also the tasks that they start.

    A task leaves such a set through a done callback, and that callback runs in a later loop
    step. ``gather`` of tasks that are all done returns without a loop step, so a loop of
    ``gather`` over the whole set never lets the callback run and never ends. So wait only
    for the pending tasks."""
    while pending := [task for task in tasks if not task.done()]:
        await asyncio.gather(*pending, return_exceptions=True)


@dataclass(slots=True)
class _Held:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0
    """The updates that hold the lock or wait for it."""


class KeyedLocks:
    """One lock per chat, so that the messages of one chat run in order. A lock goes away when
    no update holds it or waits for it."""

    def __init__(self) -> None:
        self._held: dict[Hashable, _Held] = {}

    @asynccontextmanager
    async def hold(self, key: Hashable) -> AsyncIterator[None]:
        entry = self._held.setdefault(key, _Held())
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0:
                self._held.pop(key, None)

    def __len__(self) -> int:
        return len(self._held)


PIN_COMMANDS: Final = frozenset({"pin", "pins", "unpin"})
PIN_USAGE: Final = "Write the text to pin after the command."
COMPACTING: Final = "Compacting the context. This can take a minute."


def _pin_line(number: int, pin: Pin) -> str:
    text = " ".join(pin.text.split())
    return f"{number}. {text[:120]}{'...' if len(text) > 120 else ''}"


async def pin_reply(
    pins: PinStore, target: Target, session_id: uuid.UUID | None, name: str, rest: str
) -> str:
    """The answer to ``pin``, ``pins``, or ``unpin`` from a chat. ``session_id`` is the session
    of the chat. It is ``None`` when the chat has none yet."""
    if name == "pins" or (name == "unpin" and not rest.strip()):
        listed = [] if session_id is None else await pins.list(target.org_id, session_id)
        if not listed:
            return "This session has no pins."
        return "Pins:\n" + "\n".join(_pin_line(n, pin) for n, pin in enumerate(listed, start=1))
    if name == "pin":
        if not rest.strip():
            return PIN_USAGE
        if session_id is None:
            return "Send a message first. Then you can pin."
        try:
            await pins.add(
                org_id=target.org_id, session_id=session_id, user_id=target.user_id, text=rest
            )
        except PinError as error:
            return str(error)
        return "Pinned. The pin stays in the context after a compaction."
    # unpin
    if session_id is None or not rest.strip().isdecimal():
        return "Write the number of the pin after the command. The pin list shows the numbers."
    listed = await pins.list(target.org_id, session_id)
    number = int(rest.strip())
    if not 1 <= number <= len(listed):
        return "No pin has this number. The pin list shows the numbers."
    await pins.remove(target.org_id, session_id, listed[number - 1].id)
    return "Removed."
