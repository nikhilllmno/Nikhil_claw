"""The Telegram channel (docs/architecture.md, Sections 11.1 to 11.3).

One shared sama bot serves the team. Telegram sends each update to ``POST /telegram/webhook``
with the webhook secret in ``X-Telegram-Bot-Api-Secret-Token``. The gateway:

1. checks the secret, drops repeated updates, and handles the update in the background;
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
import hmac
import json
import logging
import re
import uuid
from collections import OrderedDict
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Final

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import (
    ENDED_RUN_STATUSES,
    AgentProfile,
    Approval,
    AuditEntry,
    ChannelGroup,
    ChannelIdentity,
    Message,
    Run,
    RunEvent,
    Session,
    SessionBinding,
)
from sama.gate.service import ApprovalError, ApprovalExpiredError, GateService
from sama.gateway.pairing import Lockout, normalize, redeem
from sama.gateway.telegram_client import MAX_TEXT, TelegramClient, TelegramError
from sama.gateway.transcribe import TranscribeError, Transcriber
from sama.orchestrator.queue import QueueError, RunQueue
from sama.tenancy.access import Grants, load_grants

log: Final = logging.getLogger("sama.gateway.telegram")

CHANNEL: Final = "telegram"
MAX_INPUT_CHARS: Final = 32_000
"""The same cap as ``POST /sessions/{id}/messages``."""
MAX_VOICE_BYTES: Final = 10 * 1024 * 1024
MAX_VOICE_S: Final = 300
SPLIT_AT: Final = 4000
"""A reply longer than this goes on in a new message (Telegram accepts 4,096 characters)."""
OPEN_STATUSES: Final = ("queued", "running", "paused")

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


class TelegramSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bot_username: str = Field(min_length=1)
    """Without the ``@``. Group messages must mention it."""
    poll_s: float = Field(default=0.5, gt=0)
    paused_poll_s: float = Field(default=5.0, gt=0)
    """The poll interval while a run waits for an approval."""
    edit_interval_s: float = Field(default=1.5, ge=0)
    """The shortest time between two edits of one reply (Telegram limits edits)."""
    max_follow_s: float = Field(default=6 * 3600, gt=0)
    max_followers: int = Field(default=200, ge=1)
    """The most runs that this process follows at one time. Others get only the end."""


@dataclass(frozen=True, slots=True)
class Target:
    """Where the messages of one chat go."""

    org_id: uuid.UUID
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    agent_profile_id: uuid.UUID
    audience: str
    """``private`` for a direct chat, ``shared`` for a group."""

    @property
    def owner_user_id(self) -> uuid.UUID | None:
        return self.user_id if self.audience == "private" else None


@dataclass(frozen=True, slots=True)
class _Bound:
    session_id: uuid.UUID
    agent_profile_id: uuid.UUID


def _binding_key(chat_id: int) -> str:
    return f"chat:{chat_id}"


def split_point(text: str, limit: int = SPLIT_AT) -> int:
    """Where to cut ``text`` that is longer than ``limit``: a line end, else a space."""
    for mark in ("\n", " "):
        cut = text.rfind(mark, limit // 2, limit)
        if cut > 0:
            return cut + 1
    return limit


class _Reply:
    """One reply that grows as text arrives. It edits its message at most once in
    ``interval_s`` and goes on in a new message after ``SPLIT_AT`` characters."""

    def __init__(
        self, client: TelegramClient, chat_id: int, reply_to: int | None, interval_s: float
    ) -> None:
        self._client = client
        self._chat_id = chat_id
        self._reply_to = reply_to
        self._interval_s = interval_s
        self._text = ""
        self._shown = ""
        self._message_id: int | None = None
        self._last = 0.0
        self.sent_any = False
        """True when text was shown since the start or the last ``restart``."""

    async def add(self, text: str) -> None:
        self._text += text
        while len(self._text) > SPLIT_AT:
            cut = split_point(self._text)
            head, self._text = self._text[:cut], self._text[cut:]
            await self._show(head)
            self._message_id, self._shown = None, ""
        await self.flush(final=False)

    async def flush(self, *, final: bool) -> None:
        loop = asyncio.get_running_loop()
        if final or loop.time() - self._last >= self._interval_s:
            await self._show(self._text)

    def restart(self) -> None:
        """The next text goes in a new message, for example after an approval card."""
        self._text, self._shown, self._message_id = "", "", None
        self.sent_any = False

    async def _show(self, text: str) -> None:
        if not text.strip() or text == self._shown:
            return
        if self._message_id is None:
            self._message_id = await self._client.send_message(
                self._chat_id, text, reply_to=self._reply_to
            )
        else:
            await self._client.edit_message(self._chat_id, self._message_id, text)
        self._shown = text
        self._last = asyncio.get_running_loop().time()
        self.sent_any = True


def _approval_card(row: Any) -> tuple[str, list[list[dict[str, str]]]]:
    arguments = json.dumps(row.arguments, ensure_ascii=False, indent=1)
    if len(arguments) > 1500:
        arguments = arguments[:1500] + "\n..."
    text = f"Approval needed: {row.tool_name}\n{row.reason}\n\n{arguments}".strip()
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
        settings: TelegramSettings,
        webhook_secret: SecretStr,
        bot_user_id: int | None = None,
        queue: RunQueue | None = None,
        gate: GateService | None = None,
        transcriber: Transcriber | None = None,
        lockout: Lockout | None = None,
    ) -> None:
        if len(webhook_secret.get_secret_value()) < 16:
            raise ValueError("the webhook secret needs at least 16 characters")
        self._engine = engine
        self._client = client
        self._settings = settings
        self._secret = webhook_secret
        self._bot_user_id = bot_user_id
        self._queue = queue or RunQueue(engine)
        self._gate = gate or GateService(engine)
        self._transcriber = transcriber
        self._lockout = lockout or Lockout()
        self._seen: OrderedDict[int, None] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        self._followers: set[uuid.UUID] = set()
        self._chat_locks: dict[int, asyncio.Lock] = {}
        mention = re.escape(settings.bot_username)
        self._mention = re.compile(rf"(?<!\w)@{mention}\b", re.IGNORECASE)

    # -- HTTP -----------------------------------------------------------------------------

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.post("/telegram/webhook")
        async def webhook(
            request: Request,
            x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
        ) -> dict[str, str]:
            given = (x_telegram_bot_api_secret_token or "").encode()
            if not hmac.compare_digest(given, self._secret.get_secret_value().encode()):
                raise HTTPException(status_code=401, detail="wrong webhook secret")
            try:
                update = await request.json()
            except ValueError:
                raise HTTPException(status_code=400, detail="the body is not JSON") from None
            if not isinstance(update, dict):
                raise HTTPException(status_code=400, detail="the body is not an update")
            if self._is_repeat(update.get("update_id")):
                return {}
            self._spawn(self.handle_update(update))
            return {}

        return router

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
                lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
                try:
                    async with lock:
                        await self._on_message(message)
                finally:
                    if not lock.locked():
                        self._chat_locks.pop(chat_id, None)
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
        if to is not None and to.lower() != self._settings.bot_username.lower():
            return None, text
        return f"/{name.lower()}", (rest or "").strip()

    def _addressed(self, message: Mapping[str, Any], text: str) -> bool:
        if self._mention.search(text):
            return True
        replied = message.get("reply_to_message") or {}
        sender = replied.get("from") or {}
        return self._bot_user_id is not None and sender.get("id") == self._bot_user_id

    async def _on_message(self, message: Mapping[str, Any]) -> None:
        chat = message["chat"]
        sender = message.get("from") or {}
        if chat.get("type") not in ("private", "group", "supergroup") or sender.get("is_bot"):
            return
        if "id" not in sender:
            return
        chat_id = int(chat["id"])
        group = chat.get("type") != "private"
        sender_id = str(sender["id"])
        message_id = int(message["message_id"])
        text = str(message.get("text") or message.get("caption") or "").strip()
        command, rest = self._command(text)
        voice = message.get("voice") or message.get("audio")
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
        if not text and isinstance(voice, dict):
            text = await self._transcribe(chat_id, message_id, voice) or ""
        if not text:
            return
        if len(text) > MAX_INPUT_CHARS:
            await self._say(chat_id, "The message is too long.", reply_to=message_id)
            return
        bound = await self._session(chat_id, target, grants, create=True)
        if bound is None:
            return
        try:
            run_id = await self._queue.enqueue(
                org_id=target.org_id, session_id=bound.session_id, text=text, user_id=target.user_id
            )
        except QueueError as error:
            await self._say(chat_id, str(error), reply_to=message_id)
            return
        self._follow(chat_id, target.org_id, run_id, message_id)

    async def _identity(self, sender_id: str) -> Any:
        """The `channel_identities` row of the sender, or None."""
        async with self._engine.connect() as connection:
            return (
                await connection.execute(
                    select(ChannelIdentity).where(
                        ChannelIdentity.channel == CHANNEL, ChannelIdentity.external_id == sender_id
                    )
                )
            ).one_or_none()

    async def _target(
        self, chat_id: int, message_id: int, group: bool, identity: Any
    ) -> Target | None:
        if not group:
            return Target(
                identity.org_id,
                identity.user_id,
                identity.workspace_id,
                identity.agent_profile_id,
                "private",
            )
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(ChannelGroup).where(
                        ChannelGroup.channel == CHANNEL, ChannelGroup.external_id == str(chat_id)
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
        key = f"{CHANNEL}:{sender_id}"
        if self._lockout.locked(key):
            await self._say(chat_id, "Too many wrong codes. Try again later.", reply_to=message_id)
            return
        if group and identity is None:
            await self._say(
                chat_id,
                f"Link your own Telegram account first: send your code to"
                f" @{self._settings.bot_username} in a direct chat.",
                reply_to=message_id,
            )
            return
        code = normalize(text)
        linked = None
        if code is not None:
            async with self._engine.begin() as connection:
                linked = await redeem(
                    connection,
                    channel=CHANNEL,
                    code=code,
                    kind="group" if group else "user",
                    external_id=str(chat_id) if group else sender_id,
                    sender_user_id=None if identity is None else identity.user_id,
                )
        if linked is None:
            self._lockout.failed(key)
            await self._say(
                chat_id,
                "This code is wrong, used, or expired. Make a new code on the sama web page.",
                reply_to=message_id,
            )
            return
        self._lockout.succeeded(key)
        done = (
            f"This group is linked to sama. Mention @{self._settings.bot_username} to talk."
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
        async with self._engine.connect() as connection:
            runs = (
                await connection.execute(
                    select(Run.id).where(
                        Run.org_id == org_id,
                        Run.session_id == session_id,
                        Run.status.in_(OPEN_STATUSES),
                    )
                )
            ).scalars()
            run_ids = list(runs)
        for run_id in run_ids:
            await self._queue.cancel(org_id=org_id, run_id=run_id)
        return len(run_ids)

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

    async def _session(
        self, chat_id: int, target: Target, grants: Grants, *, create: bool
    ) -> _Bound | None:
        """The session of the chat. A session that the user cannot use now is not reused."""
        async with self._engine.connect() as connection:
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
                    .where(
                        SessionBinding.channel == CHANNEL,
                        SessionBinding.external_key == _binding_key(chat_id),
                    )
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
            return _Bound(row.id, row.agent_profile_id)
        if not create:
            return None
        return await self._new_session(chat_id, target, target.agent_profile_id, parent=None)

    async def _new_session(
        self,
        chat_id: int,
        target: Target,
        agent_profile_id: uuid.UUID,
        *,
        parent: uuid.UUID | None,
    ) -> _Bound:
        session_id = uuid.uuid4()
        async with self._engine.begin() as connection:
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
            await _bind(connection, chat_id, target, session_id)
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=target.org_id,
                    workspace_id=target.workspace_id,
                    actor_kind="user",
                    actor_user_id=target.user_id,
                    action="session.created",
                    target_type="session",
                    target_id=str(session_id),
                    details={"audience": target.audience, "channel": CHANNEL},
                )
            )
        return _Bound(session_id, agent_profile_id)

    # -- outbound -------------------------------------------------------------------------

    async def _say(self, chat_id: int, text: str, *, reply_to: int | None = None) -> None:
        try:
            await self._client.send_message(chat_id, text, reply_to=reply_to)
        except TelegramError as error:
            log.warning("a Telegram send failed chat=%s error=%s", chat_id, error)

    def _follow(self, chat_id: int, org_id: uuid.UUID, run_id: uuid.UUID, reply_to: int) -> None:
        if len(self._followers) >= self._settings.max_followers:
            log.warning("too many runs to follow; run=%s gets no stream", run_id)
            return
        self._followers.add(run_id)
        self._spawn(self._follow_run(chat_id, org_id, run_id, reply_to))

    async def _follow_run(
        self, chat_id: int, org_id: uuid.UUID, run_id: uuid.UUID, reply_to: int
    ) -> None:
        settings = self._settings
        reply = _Reply(self._client, chat_id, reply_to, settings.edit_interval_s)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settings.max_follow_s
        seq = -1
        carded: set[uuid.UUID] = set()
        status = "queued"
        try:
            with contextlib.suppress(TelegramError):
                await self._client.typing(chat_id)
            while loop.time() < deadline:
                async with self._engine.connect() as connection:
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
                                RunEvent.seq > seq,
                            )
                            .order_by(RunEvent.seq)
                            .limit(500)
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
                                ).where(
                                    Approval.org_id == org_id,
                                    Approval.run_id == run_id,
                                    Approval.status == "pending",
                                )
                            )
                        ).all()
                    )
                for event in events:
                    seq = event.seq
                    if event.type == "text_delta":
                        await reply.add(str(event.payload.get("text", "")))
                new = [row for row in pending if row.id not in carded]
                if new:
                    await reply.flush(final=True)
                    reply.restart()
                    for row in new:
                        carded.add(row.id)
                        text, buttons = _approval_card(row)
                        await self._client.send_message(chat_id, text, buttons=buttons)
                if status in ENDED_RUN_STATUSES and len(events) < 500:
                    break
                await reply.flush(final=False)
                paused = status == "paused"
                await asyncio.sleep(settings.paused_poll_s if paused else settings.poll_s)
            await reply.flush(final=True)
            await self._ending(chat_id, org_id, run_id, status, reply)
        except TelegramError as error:
            log.warning("a Telegram reply failed run=%s error=%s", run_id, error)
        finally:
            self._followers.discard(run_id)

    async def _ending(
        self, chat_id: int, org_id: uuid.UUID, run_id: uuid.UUID, status: str, reply: _Reply
    ) -> None:
        if status == "completed" and not reply.sent_any:
            # A runtime that sends no text deltas: show the stored answer.
            async with self._engine.connect() as connection:
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
            text = str((output or {}).get("text") or "")
            for start in range(0, min(len(text), 4 * MAX_TEXT), SPLIT_AT):
                await self._client.send_message(chat_id, text[start : start + SPLIT_AT])
        elif status == "failed":
            async with self._engine.connect() as connection:
                message = (
                    await connection.execute(
                        select(Run.failure_message).where(Run.org_id == org_id, Run.id == run_id)
                    )
                ).scalar_one_or_none()
            await self._client.send_message(chat_id, f"The run failed: {message or 'unknown'}")
        elif status == "cancelled":
            await self._client.send_message(chat_id, "The run stopped.")

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
        async with self._engine.connect() as connection:
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
                    .where(Approval.org_id == identity.org_id, Approval.id == approval_id)
                )
            ).one_or_none()
            grants = await load_grants(connection, identity.org_id, identity.user_id)
        if row is None or not grants.sees_session(
            row.workspace_id, row.audience, row.owner_user_id
        ):
            await self._answer(callback_id, "You cannot decide this approval.")
            return
        try:
            await self._gate.decide_approval(
                org_id=identity.org_id,
                run_id=row.run_id,
                call_id=row.call_id,
                user_id=identity.user_id,
                approve=approve,
            )
            outcome = "Approved" if approve else "Denied"
        except ApprovalExpiredError:
            outcome = "Expired"
        except ApprovalError as error:
            await self._answer(callback_id, str(error))
            return
        await self._queue.release_decided(org_id=identity.org_id, run_id=row.run_id)
        await self._answer(callback_id, outcome)
        if "message_id" in message and "chat" in message:
            name = sender.get("first_name") or sender.get("username") or "a user"
            try:
                await self._client.edit_message(
                    int(message["chat"]["id"]),
                    int(message["message_id"]),
                    f"{message.get('text', row.tool_name)}\n\n{outcome} by {name}.",
                )
            except TelegramError as error:
                log.warning("an approval card edit failed error=%s", error)

    async def _answer(self, callback_id: str, text: str) -> None:
        try:
            await self._client.answer_callback(callback_id, text)
        except TelegramError as error:
            log.warning("a callback answer failed error=%s", error)


async def _bind(
    connection: AsyncConnection, chat_id: int, target: Target, session_id: uuid.UUID
) -> None:
    statement = pg_insert(SessionBinding).values(
        org_id=target.org_id,
        workspace_id=target.workspace_id,
        session_id=session_id,
        channel=CHANNEL,
        external_key=_binding_key(chat_id),
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
