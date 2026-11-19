"""The Telegram channel (docs/architecture.md, Sections 11.1 to 11.3 and 11.6).

Each org connects its own bot as an integration. One ``TelegramGateway`` serves one bot. The
``TelegramHub`` (``sama.integrations.hub``) checks the webhook secret of the bot and gives each
update to ``accept``. The gateway:

1. drops repeated updates and handles the update in the background;
2. finds the linked user of the sender (``channel_identities``) and, in a group, the linked
   group (``channel_groups``). An unknown sender gets a pairing prompt in a direct chat and
   nothing in a group;
3. checks the memberships of the user again for each message;
4. finds or makes the session of the chat (``session_bindings``) and queues a run;
5. follows the run: it edits one reply message as text arrives, sends approval cards with
   buttons, and tells the user when the run fails or stops.

A direct chat is a ``private`` session of the user. A group chat is a ``shared`` session of the
group workspace. In a group the bot reacts only to commands, to a mention, and to a reply to
one of its messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import AgentProfile, ChannelGroup, ChannelIdentity
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
from sama.gateway.telegram_client import TelegramClient, TelegramError
from sama.gateway.transcribe import TranscribeError, Transcriber
from sama.orchestrator.queue import QueueError, RunQueue
from sama.tenancy.access import Grants, load_grants

log: Final = logging.getLogger("sama.gateway.telegram")

CHANNEL: Final = "telegram"
MAX_INPUT_CHARS: Final = 32_000
"""The same cap as ``POST /sessions/{id}/messages``."""
MAX_VOICE_BYTES: Final = 10 * 1024 * 1024
MAX_VOICE_S: Final = 300

BUSY: Final = "sama is busy now. Send the message again in a few minutes."
PAIR_PROMPT: Final = (
    "This Telegram account is not linked to sama. Open the sama web page, make a Telegram"
    " pairing code, and send the code here."
)
HELP: Final = (
    "Send a message to talk to sama.\n"
    "/new - start a new session\n"
    "/stop - stop the current run\n"
    "/model - show the agent profiles. /model <name> starts a new session with that profile\n"
    "/pair <code> - link this account, or link a group with a group code"
)


@dataclass(frozen=True, slots=True)
class TelegramBot:
    """The bot of one Telegram integration (Section 11.6)."""

    integration_id: uuid.UUID
    org_id: uuid.UUID
    username: str
    """Without the ``@``. Group messages must mention it."""
    user_id: int | None = None
    """The Telegram user id of the bot. A reply to a message of the bot addresses it."""


TelegramSettings = FollowSettings
"""The settings that all bots share."""


class _Unlinked(Exception):
    """The chat lost the link that it had when the run started."""


class _Outbox:
    """Sends the output of one run to one chat.

    Each Bot API call first asks ``allowed``, so that an unlink stops the output at once. When
    Telegram asks the bot to wait, the outbox waits and asks ``allowed`` again before it tries
    again.
    """

    def __init__(
        self,
        client: TelegramClient,
        chat_id: int,
        allowed: Callable[[], Awaitable[bool]],
        *,
        max_tries: int = 4,
    ) -> None:
        self._client = client.without_retries()
        self._max_wait_s = client.max_wait_s
        self._chat_id = chat_id
        self._allowed = allowed
        self._max_tries = max_tries

    async def send_message(
        self,
        text: str,
        *,
        reply_to: int | None = None,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> int:
        return await self._call(
            lambda: self._client.send_message(
                self._chat_id, text, reply_to=reply_to, buttons=buttons
            )
        )

    async def edit_message(self, message_id: int, text: str) -> None:
        await self._call(lambda: self._client.edit_message(self._chat_id, message_id, text))

    async def _call[T](self, send: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(1, self._max_tries + 1):
            if not await self._allowed():
                raise _Unlinked
            try:
                return await send()
            except TelegramError as error:
                wait = error.retry_after
                if wait is None or wait > self._max_wait_s or attempt == self._max_tries:
                    raise
                await asyncio.sleep(wait)
        raise AssertionError("unreachable")


def _approval_card(row: Any) -> tuple[str, list[list[dict[str, str]]]]:
    text = approval_text(row)
    buttons = [
        [
            {"text": "Approve", "callback_data": f"ap:{row.id.hex}:y"},
            {"text": "Deny", "callback_data": f"ap:{row.id.hex}:n"},
        ]
    ]
    return text, buttons


_CALLBACK: Final = re.compile(r"ap:([0-9a-f]{32}):([yn])")


class TelegramGateway:
    def __init__(
        self,
        engine: AsyncEngine,
        client: TelegramClient,
        *,
        bot: TelegramBot,
        settings: TelegramSettings | None = None,
        queue: RunQueue | None = None,
        gate: GateService | None = None,
        transcriber: Transcriber | None = None,
        lockout: Lockout | None = None,
        followers: set[object] | None = None,
    ) -> None:
        """``lockout`` and ``followers`` can be shared by the gateways of all bots, so that
        the limits hold for the whole process."""
        self._engine = engine
        self._client = client
        self._bot = bot
        self._settings = settings or TelegramSettings()
        self._queue = queue or RunQueue(engine)
        self._gate = gate or GateService(engine)
        self._transcriber = transcriber
        self._lockout = lockout or Lockout()
        self._seen: OrderedDict[int, None] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        self._followers: set[object] = set() if followers is None else followers
        """One place per followed run. A message takes its place before it queues the run."""
        self._chat_locks = KeyedLocks()
        mention = re.escape(bot.username)
        self._mention = re.compile(rf"(?<!\w)@{mention}\b", re.IGNORECASE)

    @property
    def bot(self) -> TelegramBot:
        return self._bot

    def accept(self, update: Mapping[str, Any]) -> None:
        """Takes one update from the webhook. A repeated update is dropped; the others are
        handled in the background, so Telegram gets its answer at once."""
        if self._is_repeat(update.get("update_id")):
            return
        self._spawn(self.handle_update(update))

    def _is_repeat(self, update_id: Any) -> bool:
        if not isinstance(update_id, int):
            return False
        if update_id in self._seen:
            return True
        self._seen[update_id] = None
        while len(self._seen) > 2000:
            self._seen.popitem(last=False)
        return False

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

    # -- inbound --------------------------------------------------------------------------

    async def handle_update(self, update: Mapping[str, Any]) -> None:
        """Handles one update. Errors are logged and never reach Telegram."""
        try:
            if isinstance(update.get("message"), dict):
                message = update["message"]
                chat_id = int(message["chat"]["id"])
                async with self._chat_locks.hold(chat_id):
                    await self._on_message(message)
            elif isinstance(update.get("callback_query"), dict):
                await self._on_callback(update["callback_query"])
        except Exception:
            log.exception("a Telegram update failed update_id=%s", update.get("update_id"))

    def _command(self, text: str) -> tuple[str | None, str]:
        """``("/new", "rest")`` for a command to this bot, else ``(None, text)``."""
        match = re.fullmatch(r"/([A-Za-z0-9_]{1,32})(?:@([A-Za-z0-9_]+))?(?:\s+(.*))?", text, re.S)
        if match is None:
            return None, text
        name, to, rest = match.groups()
        if to is not None and to.lower() != self._bot.username.lower():
            return None, text
        return f"/{name.lower()}", (rest or "").strip()

    def _addressed(self, message: Mapping[str, Any], text: str) -> bool:
        if self._mention.search(text):
            return True
        replied = message.get("reply_to_message") or {}
        sender = replied.get("from") or {}
        return self._bot.user_id is not None and sender.get("id") == self._bot.user_id

    async def _on_message(self, message: Mapping[str, Any]) -> None:
        chat = message["chat"]
        sender = message.get("from") or {}
        if (
            chat.get("type") not in ("private", "group", "supergroup")
            or sender.get("is_bot")
            or "id" not in sender
        ):
            return
        chat_id = int(chat["id"])
        group = chat.get("type") != "private"
        sender_id = str(sender["id"])
        message_id = int(message["message_id"])
        text = str(message.get("text") or message.get("caption") or "").strip()
        command, rest = self._command(text)
        if group and command is None and not self._addressed(message, text):
            return
        if group:
            text = self._mention.sub("", text).strip()

        identity = await self._identity(sender_id)
        if command in ("/start", "/pair") and rest:
            await self._pair(chat_id, message_id, group, sender_id, rest, identity)
            return
        if identity is None:
            if not group:
                code = normalize(text) if command is None else None
                if code is not None:
                    await self._pair(chat_id, message_id, group, sender_id, code, identity)
                else:
                    await self._say(chat_id, PAIR_PROMPT, reply_to=message_id)
            return

        target = await self._target(chat_id, message_id, group, identity)
        if target is None:
            return
        async with self._engine.connect() as connection:
            grants = await load_grants(connection, target.org_id, target.user_id)
        if not grants.sees_workspace(target.workspace_id):
            await self._say(
                chat_id, "You do not have access to this workspace now.", reply_to=message_id
            )
            return

        if command is not None:
            await self._run_command(chat_id, message_id, command, rest, target, grants)
            return
        voice = message.get("voice") or message.get("audio")
        if not text and isinstance(voice, dict):
            text = await self._transcribe(chat_id, message_id, voice) or ""
        if not text:
            return
        if len(text) > MAX_INPUT_CHARS:
            await self._say(chat_id, "The message is too long.", reply_to=message_id)
            return
        if len(self._followers) >= self._settings.max_followers:
            log.warning("too many runs to follow; chat=%s must try again", chat_id)
            await self._say(chat_id, BUSY, reply_to=message_id)
            return
        place = object()
        self._followers.add(place)
        try:
            bound = await self._session(chat_id, target, grants, create=True)
            if bound is None:
                self._followers.discard(place)
                return
            run_id = await self._queue.enqueue(
                org_id=target.org_id, session_id=bound.session_id, text=text, user_id=target.user_id
            )
        except QueueError as error:
            self._followers.discard(place)
            await self._say(chat_id, str(error), reply_to=message_id)
            return
        except BaseException:
            self._followers.discard(place)
            raise
        self._spawn(self._follow_run(chat_id, target, run_id, message_id, place))

    async def _identity(self, sender_id: str) -> Any:
        """The `channel_identities` row of the sender, or None."""
        async with self._engine.connect() as connection:
            return (
                await connection.execute(
                    select(ChannelIdentity).where(
                        ChannelIdentity.integration_id == self._bot.integration_id,
                        ChannelIdentity.external_id == sender_id,
                    )
                )
            ).one_or_none()

    async def _target(
        self, chat_id: int, message_id: int, group: bool, identity: Any
    ) -> Target | None:
        if not group:
            return Target.private(identity)
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(ChannelGroup).where(
                        ChannelGroup.integration_id == self._bot.integration_id,
                        ChannelGroup.external_id == str(chat_id),
                    )
                )
            ).one_or_none()
        if row is None:
            await self._say(
                chat_id,
                "This group is not linked to sama. A member can make a group pairing code on the"
                " sama web page and send /pair <code> here.",
                reply_to=message_id,
            )
            return None
        if row.org_id != identity.org_id:
            await self._say(chat_id, "You cannot use sama in this group.", reply_to=message_id)
            return None
        return Target(
            row.org_id, identity.user_id, row.workspace_id, row.agent_profile_id, "shared"
        )

    async def _pair(
        self,
        chat_id: int,
        message_id: int,
        group: bool,
        sender_id: str,
        text: str,
        identity: Any,
    ) -> None:
        key = f"{CHANNEL}:{self._bot.integration_id}:{sender_id}"
        # ``attempt`` counts the try before the database work, so tries at the same time
        # cannot pass the limit.
        if not self._lockout.attempt(key):
            await self._say(chat_id, "Too many wrong codes. Try again later.", reply_to=message_id)
            return
        if group and identity is None:
            await self._say(
                chat_id,
                f"Link your own Telegram account first: send your code to"
                f" @{self._bot.username} in a direct chat.",
                reply_to=message_id,
            )
            return
        code = normalize(text)
        linked = None
        if code is not None:
            async with self._engine.begin() as connection:
                linked = await redeem(
                    connection,
                    integration_id=self._bot.integration_id,
                    code=code,
                    kind="group" if group else "user",
                    external_id=str(chat_id) if group else sender_id,
                    sender_user_id=None if identity is None else identity.user_id,
                )
        if linked is None:
            await self._say(
                chat_id,
                "This code is wrong, used, or expired. Make a new code on the sama web page.",
                reply_to=message_id,
            )
            return
        self._lockout.succeeded(key)
        done = (
            f"This group is linked to sama. Mention @{self._bot.username} to talk."
            if group
            else "This Telegram account is linked to sama. Send a message to start."
        )
        await self._say(chat_id, done, reply_to=message_id)

    async def _transcribe(self, chat_id: int, message_id: int, voice: Mapping[str, Any]) -> str:
        if self._transcriber is None:
            await self._say(chat_id, "Voice notes are off. Send text.", reply_to=message_id)
            return ""
        if int(voice.get("duration") or 0) > MAX_VOICE_S:
            await self._say(
                chat_id, "The voice note is too long (5 minutes at most).", reply_to=message_id
            )
            return ""
        try:
            audio = await self._client.download(str(voice["file_id"]), max_bytes=MAX_VOICE_BYTES)
            text = await self._transcriber.transcribe(
                audio,
                filename="voice.ogg",
                mime_type=str(voice.get("mime_type") or "audio/ogg"),
            )
        except (TelegramError, TranscribeError) as error:
            log.warning("a voice note failed chat=%s error=%s", chat_id, error)
            await self._say(chat_id, "sama cannot read this voice note.", reply_to=message_id)
            return ""
        if not text:
            await self._say(chat_id, "The voice note has no words.", reply_to=message_id)
        return text

    # -- commands -------------------------------------------------------------------------

    async def _run_command(
        self,
        chat_id: int,
        message_id: int,
        command: str,
        rest: str,
        target: Target,
        grants: Grants,
    ) -> None:
        if command == "/new":
            current = await self._session(chat_id, target, grants, create=False)
            profile = target.agent_profile_id if current is None else current.agent_profile_id
            await self._new_session(chat_id, target, profile, parent=None)
            await self._say(chat_id, "A new session started.", reply_to=message_id)
        elif command == "/stop":
            current = await self._session(chat_id, target, grants, create=False)
            stopped = 0 if current is None else await self._stop(target.org_id, current.session_id)
            await self._say(
                chat_id, "Stopped." if stopped else "No run is open now.", reply_to=message_id
            )
        elif command == "/model":
            await self._model(chat_id, message_id, rest, target, grants)
        else:
            await self._say(chat_id, HELP, reply_to=message_id)

    async def _stop(self, org_id: uuid.UUID, session_id: uuid.UUID) -> int:
        return await stop_runs(self._engine, self._queue, org_id, [session_id])

    async def _model(
        self, chat_id: int, message_id: int, name: str, target: Target, grants: Grants
    ) -> None:
        async with self._engine.connect() as connection:
            profiles = (
                await connection.execute(
                    select(AgentProfile.id, AgentProfile.name, AgentProfile.model)
                    .where(
                        AgentProfile.org_id == target.org_id,
                        (AgentProfile.workspace_id.is_(None))
                        | (AgentProfile.workspace_id == target.workspace_id),
                    )
                    .order_by(AgentProfile.name)
                )
            ).all()
        current = await self._session(chat_id, target, grants, create=False)
        current_profile = target.agent_profile_id if current is None else current.agent_profile_id
        if not name:
            lines = [
                f"{'* ' if p.id == current_profile else '  '}{p.name} ({p.model})" for p in profiles
            ]
            await self._say(
                chat_id,
                "Agent profiles:\n" + "\n".join(lines) + "\n/model <name> starts a new session.",
                reply_to=message_id,
            )
            return
        chosen = [p for p in profiles if p.name.lower() == name.lower()]
        if len(chosen) != 1:
            await self._say(
                chat_id, f"No agent profile has the name {name!r}.", reply_to=message_id
            )
            return
        parent = None if current is None else current.session_id
        await self._new_session(chat_id, target, chosen[0].id, parent=parent)
        await self._say(
            chat_id,
            f"A new session with {chosen[0].name} ({chosen[0].model}) started.",
            reply_to=message_id,
        )

    # -- sessions -------------------------------------------------------------------------

    def _binding_key(self, chat_id: int) -> str:
        """A chat id is unique only for one bot, so the key holds the integration."""
        return f"{self._bot.integration_id}:chat:{chat_id}"

    async def _session(
        self, chat_id: int, target: Target, grants: Grants, *, create: bool
    ) -> Bound | None:
        """The session of the chat. A session that the user cannot use now is not reused."""
        key = self._binding_key(chat_id)
        bound = await find_session(self._engine, CHANNEL, key, target, grants)
        if bound is not None or not create:
            return bound
        return await self._new_session(chat_id, target, target.agent_profile_id, parent=None)

    async def _new_session(
        self,
        chat_id: int,
        target: Target,
        agent_profile_id: uuid.UUID,
        *,
        parent: uuid.UUID | None,
    ) -> Bound:
        return await new_session(
            self._engine,
            CHANNEL,
            self._binding_key(chat_id),
            target,
            agent_profile_id,
            parent=parent,
        )

    # -- outbound -------------------------------------------------------------------------

    async def _say(self, chat_id: int, text: str, *, reply_to: int | None = None) -> None:
        try:
            await self._client.send_message(chat_id, text, reply_to=reply_to)
        except TelegramError as error:
            log.warning("a Telegram send failed chat=%s error=%s", chat_id, error)

    async def _may_deliver(self, connection: AsyncConnection, chat_id: int, target: Target) -> bool:
        """True when the chat is still linked as it was when the run started.

        An unlinked account, a group linked to another workspace, or a user who lost the
        workspace stops the delivery (docs/architecture.md, Section 11.3).
        """
        if target.audience == "private":
            linked = await connection.scalar(
                select(ChannelIdentity.id).where(
                    ChannelIdentity.integration_id == self._bot.integration_id,
                    ChannelIdentity.external_id == str(chat_id),
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
                ChannelGroup.integration_id == self._bot.integration_id,
                ChannelGroup.external_id == str(chat_id),
                ChannelGroup.org_id == target.org_id,
                ChannelGroup.workspace_id == target.workspace_id,
            )
        )
        return linked is not None

    async def _follow_run(
        self, chat_id: int, target: Target, run_id: uuid.UUID, reply_to: int, place: object
    ) -> None:
        settings = self._settings
        org_id = target.org_id

        async def allowed() -> bool:
            async with self._engine.connect() as connection:
                return await self._may_deliver(connection, chat_id, target)

        outbox = _Outbox(self._client, chat_id, allowed)
        reply = Reply(outbox, reply_to, settings.edit_interval_s)

        async def send_card(row: Any) -> None:
            text, buttons = _approval_card(row)
            await outbox.send_message(text, buttons=buttons)

        try:
            with contextlib.suppress(TelegramError):
                await self._client.typing(chat_id)
            status = await follow_run(self._engine, org_id, run_id, reply, settings, send_card)
            await send_ending(self._engine, outbox, org_id, run_id, status, reply)
        except _Unlinked:
            log.warning("the chat lost its link; run=%s is not delivered", run_id)
        except TelegramError as error:
            log.warning("a Telegram reply failed run=%s error=%s", run_id, error)
        finally:
            self._followers.discard(place)

    # -- approvals ------------------------------------------------------------------------

    async def _on_callback(self, callback: Mapping[str, Any]) -> None:
        callback_id = str(callback.get("id", ""))
        match = _CALLBACK.fullmatch(str(callback.get("data", "")))
        sender = callback.get("from") or {}
        message = callback.get("message") or {}
        if match is None or "id" not in sender:
            await self._answer(callback_id, "This button is not known.")
            return
        approval_id, approve = uuid.UUID(match.group(1)), match.group(2) == "y"
        identity = await self._identity(str(sender["id"]))
        if identity is None:
            await self._answer(callback_id, "Link your Telegram account to sama first.")
            return
        try:
            decided = await decide(
                self._engine,
                self._gate,
                self._queue,
                org_id=identity.org_id,
                user_id=identity.user_id,
                approval_id=approval_id,
                approve=approve,
            )
        except ApprovalError as error:
            await self._answer(callback_id, str(error))
            return
        outcome = decided.outcome
        await self._answer(callback_id, outcome)
        if "message_id" in message and "chat" in message:
            name = sender.get("first_name") or sender.get("username") or "a user"
            try:
                await self._client.edit_message(
                    int(message["chat"]["id"]),
                    int(message["message_id"]),
                    f"{message.get('text', decided.tool_name)}\n\n{outcome} by {name}.",
                )
            except TelegramError as error:
                log.warning("an approval card edit failed error=%s", error)

    async def _answer(self, callback_id: str, text: str) -> None:
        try:
            await self._client.answer_callback(callback_id, text)
        except TelegramError as error:
            log.warning("a callback answer failed error=%s", error)
