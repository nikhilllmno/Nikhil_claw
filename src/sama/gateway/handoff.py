"""``/handoff``: moves a private session to another channel (docs/architecture.md, Section 11.1).

A handoff binds the session to the direct chat of the user in the target channel (Telegram or
Slack) and removes all other chat bindings of the session. The web app shows every session that
the user can see, so a handoff to the web app only removes the chat bindings. The handoff writes
a ``session_handoffs`` row and an audit entry in one transaction.

Each chat channel registers a door: a function that gives the gateway of an integration. The
door opens the direct chat of the user, and after the move it follows the open runs of the
session there and sends a short note.

The handoff row keeps the open runs that it gave to the target (``run_ids``). Each run has one
owner of its output: the follower that the newest handoff of the run started, or the first
follower when no handoff took the run. Every other follower of the run stops at its next send
(``moved_away``), and does not go on to the run of the late ``/steer`` notes of the run.
So:

- a round trip (Telegram to Slack to Telegram) does not give two followers in Telegram;
- a run that ended before the handoff is not taken, and its first follower still sends the
  answer in the old chat;
- a follower in the old chat that starts after the handoff does not send.

A handoff to the web app takes the open runs too, but no chat follows them: the web app shows
the output.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import (
    ENDED_RUN_STATUSES,
    AuditEntry,
    ChannelIdentity,
    Run,
    Session,
    SessionBinding,
    SessionHandoff,
)
from sama.gateway.sessions import Target, bind
from sama.tenancy.access import load_grants

log: Final = logging.getLogger("sama.gateway.handoff")

HANDOFF_CHANNELS: Final = ("telegram", "slack", "web")
"""The places that can take a session."""

_LABELS: Final = {"telegram": "Telegram", "slack": "Slack", "web": "the web app"}

USAGE: Final = "Write where the session goes: {command} telegram, {command} slack, or {command} web"


class HandoffError(Exception):
    """The handoff is not possible. The text is for the user."""


@dataclass(frozen=True, slots=True)
class Chat:
    """The direct chat of a user in a channel."""

    key: str
    """The ``session_bindings.external_key`` of the chat."""
    address: Any
    """What the gateway needs to send to the chat (a Telegram chat id, a Slack channel id)."""


class Door(Protocol):
    """The part of a chat gateway that takes a session."""

    async def open_chat(self, identity: Any) -> Chat:
        """Opens the direct chat of the ``channel_identities`` row."""
        ...

    async def adopt(
        self,
        chat: Chat,
        target: Target,
        run_ids: Sequence[uuid.UUID],
        text: str,
        *,
        handoff_id: int,
    ) -> None:
        """Follows the open runs of the session as the followers of the handoff ``handoff_id``,
        then sends ``text`` to the chat. The followers do not count against the limit of the
        gateway: the handoff only moves them from the old chat, which stops its followers."""
        ...


DoorFinder = Callable[[uuid.UUID], Awaitable[Door | None]]
"""Gives the door of an integration id, or ``None`` when the integration is not active."""


@dataclass(frozen=True, slots=True)
class Moved:
    """The result of a handoff."""

    session_id: uuid.UUID
    to_channel: str
    text: str
    """The answer for the user in the old place."""
    url: str | None = None
    """The web page of the session, for a handoff to the web app."""


class Handoffs:
    def __init__(self, engine: AsyncEngine, *, web_url: str | None = None) -> None:
        """``web_url`` is the address of the web app, for the link of a handoff to it."""
        self._engine = engine
        self._web_url = (web_url or "").rstrip("/") or None
        self._doors: dict[str, DoorFinder] = {}

    def register(self, channel: str, finder: DoorFinder) -> None:
        self._doors[channel] = finder

    async def move(
        self,
        *,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        from_channel: str,
        from_key: str | None,
        to_channel: str,
    ) -> Moved:
        """Moves the session of the user to ``to_channel``. Raises ``HandoffError``."""
        to_channel = to_channel.strip().lower()
        if to_channel not in HANDOFF_CHANNELS:
            raise HandoffError(f"sama cannot move a session to {to_channel or 'nothing'}.")
        if to_channel == from_channel:
            raise HandoffError(f"The session is in {_LABELS[to_channel]} now.")
        async with self._engine.connect() as connection:
            session = await self._session(connection, org_id, user_id, session_id)
            identity = None
            if to_channel != "web":
                identity = await self._identity(
                    connection, org_id, user_id, to_channel, session.workspace_id
                )
        door: Door | None = None
        chat: Chat | None = None
        target: Target | None = None
        if identity is not None:
            finder = self._doors.get(to_channel)
            door = None if finder is None else await finder(identity.integration_id)
            if door is None:
                raise HandoffError(f"The {_LABELS[to_channel]} app of sama is not available now.")
            try:
                chat = await door.open_chat(identity)
            except Exception:
                log.exception("a handoff cannot open the chat session=%s", session_id)
                raise HandoffError(
                    f"sama cannot open your {_LABELS[to_channel]} chat now. Try again later."
                ) from None
            target = Target.private(identity)
        handoff_id, run_ids = await self._commit(
            org_id=org_id,
            user_id=user_id,
            session_id=session_id,
            workspace_id=session.workspace_id,
            from_channel=from_channel,
            from_key=from_key,
            to_channel=to_channel,
            chat=chat,
            target=target,
        )
        if door is not None and chat is not None and target is not None:
            note = "This chat now continues your sama session."
            if run_ids:
                note += " The open run answers here."
            try:
                await door.adopt(chat, target, run_ids, note, handoff_id=handoff_id)
            except Exception:
                log.exception("a handoff note failed session=%s", session_id)
        if to_channel == "web":
            url = None if self._web_url is None else f"{self._web_url}/s/{session_id}"
            text = (
                f"The session moved to the web app: {url}"
                if url
                else "The session moved to the web app. Open sama in the browser to go on."
            )
            return Moved(session_id, to_channel, text, url)
        return Moved(
            session_id,
            to_channel,
            f"The session moved to {_LABELS[to_channel]}. Go on there. A new message here"
            " starts a new session.",
        )

    async def follow(
        self, *, org_id: uuid.UUID, session_id: uuid.UUID, run_id: uuid.UUID, text: str
    ) -> int:
        """Sends the answer of ``run_id`` to the direct chats (Telegram, Slack) that the private
        session is bound to, after ``text``. A run that sama starts by itself (a finished
        sign-in, M8.1) has no chat follower otherwise. Returns how many chats follow it. The
        followers use the handoff id ``0``: no handoff took the run, so ``moved_away`` lets
        them send until a real handoff moves the session."""
        async with self._engine.connect() as connection:
            session = (
                await connection.execute(
                    select(
                        Session.workspace_id,
                        Session.audience,
                        Session.owner_user_id,
                        Session.worker_bot_id,
                    ).where(Session.org_id == org_id, Session.id == session_id)
                )
            ).one_or_none()
            if (
                session is None
                or session.audience != "private"
                or session.owner_user_id is None
                or session.worker_bot_id is not None
            ):
                return 0
            bindings = (
                await connection.execute(
                    select(SessionBinding.channel, SessionBinding.external_key).where(
                        SessionBinding.org_id == org_id, SessionBinding.session_id == session_id
                    )
                )
            ).all()
            found: list[tuple[Any, str]] = []
            for binding in bindings:
                if binding.channel not in self._doors:
                    continue
                try:
                    identity = await self._identity(
                        connection,
                        org_id,
                        session.owner_user_id,
                        binding.channel,
                        session.workspace_id,
                    )
                except HandoffError:
                    continue
                found.append((identity, binding.external_key))
        count = 0
        for identity, key in found:
            door = await self._doors[identity.channel](identity.integration_id)
            if door is None:
                continue
            chat = await door.open_chat(identity)
            if chat.key != key:
                continue  # The session is bound to another chat of the user, not the direct one.
            await door.adopt(chat, Target.private(identity), [run_id], text, handoff_id=0)
            count += 1
        return count

    @staticmethod
    async def _session(
        connection: AsyncConnection, org_id: uuid.UUID, user_id: uuid.UUID, session_id: uuid.UUID
    ) -> Any:
        row = (
            await connection.execute(
                select(
                    Session.workspace_id,
                    Session.audience,
                    Session.owner_user_id,
                    Session.status,
                    Session.worker_bot_id,
                ).where(Session.org_id == org_id, Session.id == session_id)
            )
        ).one_or_none()
        grants = await load_grants(connection, org_id, user_id)
        if row is None or not grants.sees_session(
            row.workspace_id, row.audience, row.owner_user_id
        ):
            raise HandoffError("sama cannot find this session.")
        if row.audience != "private" or row.owner_user_id != user_id or row.worker_bot_id:
            raise HandoffError("Only your own private sessions can move to another channel.")
        if row.status != "active":
            raise HandoffError("This session is archived.")
        return row

    @staticmethod
    async def _identity(
        connection: AsyncConnection,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        channel: str,
        workspace_id: uuid.UUID,
    ) -> Any:
        rows = (
            await connection.execute(
                select(ChannelIdentity)
                .where(
                    ChannelIdentity.org_id == org_id,
                    ChannelIdentity.user_id == user_id,
                    ChannelIdentity.channel == channel,
                )
                .order_by(ChannelIdentity.updated_at.desc())
            )
        ).all()
        if not rows:
            raise HandoffError(
                f"Link your {_LABELS[channel]} account to sama first: make a pairing code on the"
                " sama web page."
            )
        for row in rows:
            if row.workspace_id == workspace_id:
                return row
        raise HandoffError(
            f"Your {_LABELS[channel]} chat uses another workspace. The session cannot move there."
        )

    async def _commit(
        self,
        *,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        workspace_id: uuid.UUID,
        from_channel: str,
        from_key: str | None,
        to_channel: str,
        chat: Chat | None,
        target: Target | None,
    ) -> tuple[int, list[uuid.UUID]]:
        """Moves the bindings, writes the handoff and the audit entry, and returns the id of
        the handoff and the open runs of the session that it gives to the target chat."""
        async with self._engine.begin() as connection:
            status = await connection.scalar(
                select(Session.status)
                .where(Session.org_id == org_id, Session.id == session_id)
                .with_for_update()
            )
            if status != "active":
                raise HandoffError("This session is archived.")
            others = delete(SessionBinding).where(
                SessionBinding.org_id == org_id, SessionBinding.session_id == session_id
            )
            if chat is not None and target is not None:
                await bind(connection, to_channel, chat.key, target, session_id)
                others = others.where(
                    ~(
                        (SessionBinding.channel == to_channel)
                        & (SessionBinding.external_key == chat.key)
                    )
                )
            await connection.execute(others)
            run_ids = list(
                (
                    await connection.scalars(
                        select(Run.id)
                        .where(
                            Run.org_id == org_id,
                            Run.session_id == session_id,
                            Run.status.not_in(ENDED_RUN_STATUSES),
                        )
                        .order_by(Run.created_at)
                    )
                ).all()
            )
            inserted = await connection.execute(
                insert(SessionHandoff)
                .values(
                    org_id=org_id,
                    workspace_id=workspace_id,
                    session_id=session_id,
                    user_id=user_id,
                    from_channel=from_channel,
                    from_key=from_key,
                    to_channel=to_channel,
                    to_key=None if chat is None else chat.key,
                    run_ids=run_ids,
                )
                .returning(SessionHandoff.id)
            )
            handoff_id = int(inserted.scalar_one())
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=org_id,
                    workspace_id=workspace_id,
                    actor_kind="user",
                    actor_user_id=user_id,
                    action="session.handoff",
                    target_type="session",
                    target_id=str(session_id),
                    details={
                        "from_channel": from_channel,
                        "to_channel": to_channel,
                        "handoff_id": handoff_id,
                    },
                )
            )
            return handoff_id, run_ids


async def moved_away(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    via: int | None,
) -> bool:
    """True when a follower of the run must stop: a handoff that is not ``via`` (the handoff
    that started the follower, ``None`` for the first follower) is the newest handoff that took
    the run."""
    session_id = select(Run.session_id).where(Run.org_id == org_id, Run.id == run_id)
    newest = await connection.scalar(
        select(SessionHandoff.id)
        .where(
            SessionHandoff.org_id == org_id,
            SessionHandoff.session_id == session_id.scalar_subquery(),
            SessionHandoff.run_ids.contains([run_id]),
        )
        .order_by(SessionHandoff.id.desc())
        .limit(1)
    )
    return newest is not None and newest != via
