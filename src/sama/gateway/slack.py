"""The Slack channel (docs/architecture.md, Sections 11.1 to 11.3 and 11.6).

Each org connects its own Slack app as an integration. One ``SlackGateway`` serves one app in
one Slack workspace. The ``SlackHub`` (``sama.integrations.slack_hub``) checks the request
signature and gives each event, button click, and ``/sama`` command to the gateway. The gateway:

1. drops repeated events and handles each event in the background;
2. finds the linked user of the sender (``channel_identities``) and, in a channel, the linked
   channel (``channel_groups``). An unknown sender gets a pairing prompt in a direct message
   and a message that only the sender sees in a channel;
3. checks the memberships of the user again for each message;
4. finds or makes the session of the conversation (``session_bindings``) and queues a run;
5. follows the run: it updates one reply message as text arrives, sends approval cards with
   buttons, and tells the user when the run fails or stops.

A direct message with the app is a ``private`` session of the user. In a channel the app reacts
only to a mention. Each thread in a linked channel is a ``shared`` session of the channel
workspace, and the app answers in the thread.

The gateway sends all text as plain text: it escapes ``&``, ``<``, and ``>``, so that the text of
a model cannot mention people or channels.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import ChannelGroup, ChannelIdentity, Session, SessionBinding
from sama.gate.service import ApprovalError, GateService
from sama.gateway.pairing import Lockout, normalize, redeem
from sama.gateway.sessions import (
    Bound,
    FollowSettings,
    KeyedLocks,
    Reply,
    Target,
    approval_text,
    decide,
    find_session,
    follow_run,
    new_session,
    send_ending,
    stop_runs,
)
from sama.gateway.slack_client import SlackClient, SlackError
from sama.orchestrator.queue import QueueError, RunQueue
from sama.tenancy.access import Grants, load_grants

log: Final = logging.getLogger("sama.gateway.slack")

CHANNEL: Final = "slack"
MAX_INPUT_CHARS: Final = 32_000
"""The same cap as ``POST /sessions/{id}/messages``."""
MAX_CARD_CHARS: Final = 2900
"""A ``plain_text`` section of Block Kit holds 3,000 characters."""

BUSY: Final = "sama is busy now. Send the message again in a few minutes."
PAIR_PROMPT: Final = (
    "This Slack account is not linked to sama. Open the sama web page, make a Slack pairing"
    " code, and send the code here."
)
LINK_FIRST: Final = (
    "Link your Slack account first: make a Slack pairing code on the sama web page and send it"
    " to the sama app in a direct message."
)
HELP: Final = (
    "Send a direct message to the sama app, or mention it in a linked channel.\n"
    "/sama new - start a new session (direct messages)\n"
    "/sama stop - stop the open runs of this conversation\n"
    "/sama pair CODE - link your account, or link this channel with a group code\n"
    "/sama help - show this help"
)

_ACTION: Final = re.compile(r"ap:([0-9a-f]{32}):([yn])")
_SLACK_ID: Final = re.compile(r"[A-Z0-9]{2,32}")


def escape(text: str) -> str:
    """Slack reads ``&``, ``<``, and ``>`` as control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def unescape(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


@dataclass(frozen=True, slots=True)
class SlackApp:
    """The app of one Slack integration (Section 11.6)."""

    integration_id: uuid.UUID
    org_id: uuid.UUID
    team_id: str
    bot_user_id: str
    """The Slack user of the app. A mention of it addresses the app."""
    app_id: str | None = None


@dataclass(frozen=True, slots=True)
class _Place:
    """A conversation: a direct message channel, or a thread in a channel."""

    channel: str
    thread_ts: str | None
    """The root of the thread. None in a direct message: the app answers in the channel."""
    sender: str


class _Unlinked(Exception):
    """The conversation lost the link that it had when the run started."""


class _Outbox:
    """Sends the output of one run to one conversation.

    Each Web API call first asks ``allowed``, so that an unlink stops the output at once. When
    Slack asks the app to wait, the outbox waits and asks ``allowed`` again before it tries
    again.
    """

    def __init__(
        self,
        client: SlackClient,
        place: _Place,
        allowed: Callable[[], Awaitable[bool]],
        *,
        max_tries: int = 4,
    ) -> None:
        self._client = client.without_retries()
        self._max_wait_s = client.max_wait_s
        self._place = place
        self._allowed = allowed
        self._max_tries = max_tries

    async def send_message(
        self, text: str, *, reply_to: Any = None, blocks: list[dict[str, Any]] | None = None
    ) -> str:
        """``reply_to`` is not used: the outbox always answers in its conversation."""
        place = self._place
        return await self._call(
            lambda: self._client.post_message(
                place.channel, escape(text), thread_ts=place.thread_ts, blocks=blocks
            )
        )

    async def edit_message(self, message_id: Any, text: str) -> None:
        await self._call(
            lambda: self._client.update_message(self._place.channel, str(message_id), escape(text))
        )

    async def _call[T](self, send: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(1, self._max_tries + 1):
            if not await self._allowed():
                raise _Unlinked
            try:
                return await send()
            except SlackError as error:
                wait = error.retry_after
                if wait is None or wait > self._max_wait_s or attempt == self._max_tries:
                    raise
                await asyncio.sleep(wait)
        raise AssertionError("unreachable")


def approval_blocks(row: Any) -> tuple[str, list[dict[str, Any]]]:
    """The fallback text and the Block Kit blocks of an approval card."""
    text = approval_text(row)[:MAX_CARD_CHARS]
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "plain_text", "text": text, "emoji": False}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "sama_approve",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "value": f"ap:{row.id.hex}:y",
                },
                {
                    "type": "button",
                    "action_id": "sama_deny",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "value": f"ap:{row.id.hex}:n",
                },
            ],
        },
    ]
    return text, blocks


class SeenEvents:
    """The event ids that the gateways took in the last ``keep_s`` seconds. Slack sends an event
    again when the answer is slow (after about 1 and 5 minutes), each time with a new signature.
    The hub keeps one object for all its gateways, so that a gateway that is built again (after a
    reconnect) also drops a repeat. A key holds the integration id and the event id."""

    def __init__(
        self,
        *,
        keep_s: float = 600.0,
        max_size: int = 50_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._keep_s = keep_s
        self._max_size = max_size
        self._clock = clock
        self._seen: OrderedDict[str, float] = OrderedDict()

    def is_repeat(self, key: str) -> bool:
        """True when ``key`` came before. Else it records ``key`` and returns False."""
        now = self._clock()
        while self._seen:
            oldest, at = next(iter(self._seen.items()))
            if now - at < self._keep_s and len(self._seen) < self._max_size:
                break
            del self._seen[oldest]
        if key in self._seen:
            return True
        self._seen[key] = now
        return False


class SlackGateway:
    def __init__(
        self,
        engine: AsyncEngine,
        client: SlackClient,
        *,
        app: SlackApp,
        settings: FollowSettings | None = None,
        queue: RunQueue | None = None,
        gate: GateService | None = None,
        lockout: Lockout | None = None,
        followers: set[object] | None = None,
        seen: SeenEvents | None = None,
    ) -> None:
        """``lockout``, ``followers``, and ``seen`` can be shared by all gateways of the process,
        so that the limits and the repeat check hold for the whole process."""
        self._engine = engine
        self._client = client
        self._app = app
        self._settings = settings or FollowSettings()
        self._queue = queue or RunQueue(engine)
        self._gate = gate or GateService(engine)
        self._lockout = lockout or Lockout()
        self._seen = seen or SeenEvents()
        self._tasks: set[asyncio.Task[None]] = set()
        self._followers: set[object] = set() if followers is None else followers
        self._locks = KeyedLocks()
        self._mention = re.compile(rf"<@{re.escape(app.bot_user_id)}(?:\|[^>]*)?>")

    def accept(self, envelope: Mapping[str, Any]) -> None:
        """Takes one ``event_callback`` from the events route. A repeated event (Slack sends
        an event again when the answer was slow) is dropped; the others are handled in the
        background, so Slack gets its answer at once."""
        if self._is_repeat(envelope.get("event_id")):
            return
        event = envelope.get("event")
        if isinstance(event, dict):
            self._spawn(self.handle_event(event))

    def accept_action(self, payload: Mapping[str, Any]) -> None:
        """Takes one ``block_actions`` payload (a button click) in the background."""
        self._spawn(self.handle_action(payload))

    def _is_repeat(self, event_id: Any) -> bool:
        if not isinstance(event_id, str) or not event_id:
            return False
        return self._seen.is_repeat(f"{self._app.integration_id}:{event_id}")

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Waits for the background work (for tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # -- events ---------------------------------------------------------------------------

    async def handle_event(self, event: Mapping[str, Any]) -> None:
        """Handles one event. Errors are logged and never reach Slack."""
        try:
            place = self._place(event)
            if place is None:
                return
            async with self._locks.hold((place.channel, place.thread_ts)):
                if place.thread_ts is None:
                    await self._on_direct(place, str(event.get("text") or "").strip())
                else:
                    await self._on_mention(place, str(event.get("text") or ""))
        except Exception:
            log.exception("a Slack event failed type=%s", event.get("type"))

    def _place(self, event: Mapping[str, Any]) -> _Place | None:
        """The conversation of a message to the app, or None for an event to ignore."""
        user = event.get("user")
        channel = event.get("channel")
        if (
            not isinstance(user, str)
            or not isinstance(channel, str)
            or event.get("bot_id")
            or user == self._app.bot_user_id
        ):
            return None
        kind = event.get("type")
        if kind == "message" and event.get("channel_type") == "im" and not event.get("subtype"):
            return _Place(channel, None, user)
        if kind == "app_mention" and event.get("channel_type") != "im":
            root = event.get("thread_ts") or event.get("ts")
            return _Place(channel, str(root), user) if root else None
        return None

    async def _on_direct(self, place: _Place, text: str) -> None:
        text = unescape(text)
        identity = await self._identity(place.sender)
        if identity is None:
            code = normalize(text)
            if code is None:
                await self._say(place, PAIR_PROMPT)
            else:
                await self._say(place, await self._pair(place.sender, place.channel, code, None))
            return
        await self._start(place, Target.private(identity), text)

    async def _on_mention(self, place: _Place, text: str) -> None:
        text = unescape(self._mention.sub("", text)).strip()
        identity = await self._identity(place.sender)
        if identity is None:
            await self._whisper(place, LINK_FIRST)
            return
        target = await self._group_target(place.channel, identity)
        if target is None:
            await self._whisper(
                place,
                "This channel is not linked to sama. Make a group pairing code on the sama web"
                " page and send /sama pair CODE in this channel.",
            )
            return
        await self._start(place, target, text)

    async def _start(self, place: _Place, target: Target, text: str) -> None:
        async with self._engine.connect() as connection:
            grants = await load_grants(connection, target.org_id, target.user_id)
        if not grants.sees_workspace(target.workspace_id):
            await self._say(place, "You do not have access to this workspace now.")
            return
        if not text:
            return
        if len(text) > MAX_INPUT_CHARS:
            await self._say(place, "The message is too long.")
            return
        if len(self._followers) >= self._settings.max_followers:
            log.warning("too many runs to follow; channel=%s must try again", place.channel)
            await self._say(place, BUSY)
            return
        holder = object()
        self._followers.add(holder)
        try:
            bound = await self._session(place, target, grants, create=True)
            if bound is None:
                self._followers.discard(holder)
                return
            run_id = await self._queue.enqueue(
                org_id=target.org_id, session_id=bound.session_id, text=text, user_id=target.user_id
            )
        except QueueError as error:
            self._followers.discard(holder)
            await self._say(place, str(error))
            return
        except BaseException:
            self._followers.discard(holder)
            raise
        self._spawn(self._follow(place, target, run_id, holder))

    async def _identity(self, sender: str) -> Any:
        """The ``channel_identities`` row of the Slack user, or None."""
        async with self._engine.connect() as connection:
            return (
                await connection.execute(
                    select(ChannelIdentity).where(
                        ChannelIdentity.integration_id == self._app.integration_id,
                        ChannelIdentity.external_id == sender,
                    )
                )
            ).one_or_none()

    async def _group_target(self, channel: str, identity: Any) -> Target | None:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(ChannelGroup).where(
                        ChannelGroup.integration_id == self._app.integration_id,
                        ChannelGroup.external_id == channel,
                    )
                )
            ).one_or_none()
        if row is None or row.org_id != identity.org_id:
            return None
        return Target(
            row.org_id, identity.user_id, row.workspace_id, row.agent_profile_id, "shared"
        )

    async def _pair(self, sender: str, channel: str, text: str, identity: Any) -> str:
        """Uses a pairing code and returns the answer. A code in a direct message links the
        Slack account; a code in a channel links the channel."""
        group = not channel.startswith("D")
        key = f"{CHANNEL}:{self._app.integration_id}:{sender}"
        # ``attempt`` counts the try before the database work, so tries at the same time
        # cannot pass the limit.
        if not self._lockout.attempt(key):
            return "Too many wrong codes. Try again later."
        if group and identity is None:
            return LINK_FIRST
        code = normalize(text)
        linked = None
        if code is not None:
            async with self._engine.begin() as connection:
                linked = await redeem(
                    connection,
                    integration_id=self._app.integration_id,
                    code=code,
                    kind="group" if group else "user",
                    external_id=channel if group else sender,
                    sender_user_id=None if identity is None else identity.user_id,
                )
        if linked is None:
            return "This code is wrong, used, or expired. Make a new code on the sama web page."
        self._lockout.succeeded(key)
        if group:
            return "This channel is linked to sama. Mention the sama app to talk."
        return "This Slack account is linked to sama. Send a message to start."

    # -- commands -------------------------------------------------------------------------

    async def command(self, form: Mapping[str, str]) -> str:
        """Runs one ``/sama`` command and returns the answer that only the sender sees."""
        sender = form.get("user_id", "")
        channel = form.get("channel_id", "")
        if not _SLACK_ID.fullmatch(sender) or not _SLACK_ID.fullmatch(channel):
            return "This command is not complete."
        words = form.get("text", "").strip().split(maxsplit=1)
        name = words[0].lower() if words else "help"
        rest = words[1].strip() if len(words) > 1 else ""
        try:
            identity = await self._identity(sender)
            if name == "pair" and rest:
                return await self._pair(sender, channel, rest, identity)
            if name not in ("new", "stop"):
                return HELP
            if identity is None:
                return PAIR_PROMPT if channel.startswith("D") else LINK_FIRST
            if name == "new":
                return await self._new(sender, channel, identity)
            return await self._stop(sender, channel, identity)
        except Exception:
            log.exception("a Slack command failed name=%s", name)
            return "sama cannot do this now. Try again later."

    async def _new(self, sender: str, channel: str, identity: Any) -> str:
        if not channel.startswith("D"):
            return "Mention the sama app in a new message to start a new thread session."
        place = _Place(channel, None, sender)
        target = Target.private(identity)
        async with self._engine.connect() as connection:
            grants = await load_grants(connection, target.org_id, target.user_id)
        if not grants.sees_workspace(target.workspace_id):
            return "You do not have access to this workspace now."
        # The lock of the messages: a message at the same time cannot bind another session.
        async with self._locks.hold((place.channel, place.thread_ts)):
            current = await self._session(place, target, grants, create=False)
            profile = target.agent_profile_id if current is None else current.agent_profile_id
            await new_session(self._engine, CHANNEL, self._key(place), target, profile)
        return "A new session started."

    async def _stop(self, sender: str, channel: str, identity: Any) -> str:
        """Stops the open runs of the direct message, or of all threads of the channel."""
        if channel.startswith("D"):
            place = _Place(channel, None, sender)
            target = Target.private(identity)
            async with self._engine.connect() as connection:
                grants = await load_grants(connection, target.org_id, target.user_id)
            current = await self._session(place, target, grants, create=False)
            session_ids = [] if current is None else [current.session_id]
        else:
            group = await self._group_target(channel, identity)
            if group is None:
                return "This channel is not linked to sama."
            async with self._engine.connect() as connection:
                grants = await load_grants(connection, group.org_id, group.user_id)
                if not grants.sees_workspace(group.workspace_id):
                    return "You do not have access to this workspace now."
                session_ids = list(
                    (
                        await connection.scalars(
                            select(SessionBinding.session_id)
                            .join(
                                Session,
                                (Session.org_id == SessionBinding.org_id)
                                & (Session.id == SessionBinding.session_id),
                            )
                            .where(
                                SessionBinding.org_id == group.org_id,
                                SessionBinding.channel == CHANNEL,
                                SessionBinding.external_key.startswith(
                                    f"{self._app.integration_id}:thread:{channel}:",
                                    autoescape=True,
                                ),
                                Session.workspace_id == group.workspace_id,
                                Session.audience == "shared",
                            )
                        )
                    ).all()
                )
            target = group
        stopped = await stop_runs(self._engine, self._queue, target.org_id, session_ids)
        return "Stopped." if stopped else "No run is open now."

    # -- sessions -------------------------------------------------------------------------

    def _key(self, place: _Place) -> str:
        """A channel id is unique only in one Slack workspace, so the key holds the
        integration."""
        if place.thread_ts is None:
            return f"{self._app.integration_id}:dm:{place.channel}"
        return f"{self._app.integration_id}:thread:{place.channel}:{place.thread_ts}"

    async def _session(
        self, place: _Place, target: Target, grants: Grants, *, create: bool
    ) -> Bound | None:
        """The session of the conversation. A session that the user cannot use now is not
        reused."""
        key = self._key(place)
        bound = await find_session(self._engine, CHANNEL, key, target, grants)
        if bound is not None or not create:
            return bound
        return await new_session(self._engine, CHANNEL, key, target, target.agent_profile_id)

    # -- outbound -------------------------------------------------------------------------

    async def _say(self, place: _Place, text: str) -> None:
        try:
            await self._client.post_message(place.channel, escape(text), thread_ts=place.thread_ts)
        except SlackError as error:
            log.warning("a Slack send failed channel=%s error=%s", place.channel, error)

    async def _whisper(self, place: _Place, text: str) -> None:
        """A message that only the sender sees."""
        try:
            await self._client.post_ephemeral(
                place.channel, place.sender, escape(text), thread_ts=place.thread_ts
            )
        except SlackError as error:
            log.warning("a Slack ephemeral send failed channel=%s error=%s", place.channel, error)

    async def _may_deliver(
        self, connection: AsyncConnection, place: _Place, target: Target
    ) -> bool:
        """True when the conversation is still linked as it was when the run started.

        An unlinked account, a channel linked to another workspace, or a user who lost the
        workspace stops the delivery (docs/architecture.md, Section 11.3).
        """
        if target.audience == "private":
            linked = await connection.scalar(
                select(ChannelIdentity.id).where(
                    ChannelIdentity.integration_id == self._app.integration_id,
                    ChannelIdentity.external_id == place.sender,
                    ChannelIdentity.org_id == target.org_id,
                    ChannelIdentity.user_id == target.user_id,
                )
            )
            if linked is None:
                return False
            grants = await load_grants(connection, target.org_id, target.user_id)
            return grants.sees_workspace(target.workspace_id)
        linked = await connection.scalar(
            select(ChannelGroup.id).where(
                ChannelGroup.integration_id == self._app.integration_id,
                ChannelGroup.external_id == place.channel,
                ChannelGroup.org_id == target.org_id,
                ChannelGroup.workspace_id == target.workspace_id,
            )
        )
        return linked is not None

    async def _follow(
        self, place: _Place, target: Target, run_id: uuid.UUID, holder: object
    ) -> None:
        settings = self._settings
        org_id = target.org_id

        async def allowed() -> bool:
            async with self._engine.connect() as connection:
                return await self._may_deliver(connection, place, target)

        outbox = _Outbox(self._client, place, allowed)
        reply = Reply(outbox, None, settings.edit_interval_s)

        async def send_card(row: Any) -> None:
            text, blocks = approval_blocks(row)
            await outbox.send_message(text, blocks=blocks)

        try:
            status = await follow_run(self._engine, org_id, run_id, reply, settings, send_card)
            await send_ending(self._engine, outbox, org_id, run_id, status, reply)
        except _Unlinked:
            log.warning("the conversation lost its link; run=%s is not delivered", run_id)
        except SlackError as error:
            log.warning("a Slack reply failed run=%s error=%s", run_id, error)
        finally:
            self._followers.discard(holder)

    # -- approvals ------------------------------------------------------------------------

    async def handle_action(self, payload: Mapping[str, Any]) -> None:
        """Handles one button click on an approval card. Errors are logged."""
        try:
            await self._on_action(payload)
        except Exception:
            log.exception("a Slack button click failed")

    async def _on_action(self, payload: Mapping[str, Any]) -> None:
        if payload.get("type") != "block_actions":
            return
        sender = str((payload.get("user") or {}).get("id") or "")
        channel = str((payload.get("channel") or {}).get("id") or "")
        message = payload.get("message") or {}
        actions = payload.get("actions") or []
        value = str(actions[0].get("value") or "") if actions else ""
        match = _ACTION.fullmatch(value)
        if match is None or not _SLACK_ID.fullmatch(sender) or not _SLACK_ID.fullmatch(channel):
            return
        place = _Place(channel, message.get("thread_ts"), sender)
        identity = await self._identity(sender)
        if identity is None:
            await self._whisper(place, "Link your Slack account to sama first.")
            return
        try:
            decided = await decide(
                self._engine,
                self._gate,
                self._queue,
                org_id=identity.org_id,
                user_id=identity.user_id,
                approval_id=uuid.UUID(match.group(1)),
                approve=match.group(2) == "y",
            )
        except ApprovalError as error:
            await self._whisper(place, str(error))
            return
        card = _card_text(message) or f"Approval needed: {decided.tool_name}"
        ts = message.get("ts")
        if not ts:
            return
        try:
            await self._client.update_message(
                channel, str(ts), f"{escape(card)}\n\n{decided.outcome} by <@{sender}>."
            )
        except SlackError as error:
            log.warning("an approval card update failed error=%s", error)


def _card_text(message: Mapping[str, Any]) -> str:
    """The text of the first section of a card, as the app sent it."""
    for block in message.get("blocks") or []:
        if isinstance(block, dict) and block.get("type") == "section":
            text = (block.get("text") or {}).get("text")
            if isinstance(text, str):
                return text
    return ""
