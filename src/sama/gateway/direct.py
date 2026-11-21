"""The shared part of the WhatsApp and email gateways (docs/architecture.md, Section 11.9).

These channels have only direct conversations with one person, and they cannot edit a sent
message. So a ``DirectGateway``:

1. drops repeated messages and handles each message in the background, one at a time for each
   conversation;
2. finds the linked user of the sender (``channel_identities``). An unknown sender gets a
   pairing prompt, and a pairing code in the message links the sender;
3. loads the grants of the user again for each message;
4. finds or makes the ``private`` session of the conversation (``session_bindings``) and queues
   a run;
5. follows the run with a ``FinalReply``: the text goes out when the run ends or pauses for an
   approval. Approval cards and files go out as the channel allows.

A subclass gives the transport: how to send a text, how to load the media of a message, the
outbox of a run, and the binding key of a conversation.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import re
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Final, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.coding.commands import SwitchNotAllowedError, parse_command, set_direct
from sama.db.models import Approval, ChannelIdentity
from sama.gate.service import ApprovalError, GateService
from sama.gateway.handoff import moved_away
from sama.gateway.pairing import Lockout, normalize, redeem
from sama.gateway.sessions import (
    BG_EMPTY,
    JOINED,
    STEER_EMPTY,
    STEERED,
    Bound,
    FinalReply,
    FollowSettings,
    KeyedLocks,
    Target,
    busy_text,
    decide,
    find_session,
    follow_run,
    new_session,
    send_ending,
    send_media,
    stop_reply,
    wait_for_tasks,
)
from sama.gateway.transcribe import Transcriber
from sama.media.generate import StoredMedia
from sama.media.images import NewAttachment, with_markers
from sama.orchestrator.queue import QueueError, RunQueue
from sama.review.apply import ReviewApplier
from sama.skills.service import SkillLibrary
from sama.tenancy.access import Grants, load_grants

log: Final = logging.getLogger("sama.gateway.direct")

MAX_INPUT_CHARS: Final = 32_000
"""The same cap as ``POST /sessions/{id}/messages``."""
MAX_STORED_ANSWER_CHARS: Final = 200_000
"""The most text of a stored answer that a direct channel sends when the run sent no deltas."""
BUSY: Final = "sama is busy now. Send the message again in a few minutes."
NO_ACCESS: Final = "You do not have access to this workspace now."
WRONG_CODE: Final = "This code is wrong, used, or expired. Make a new code on the sama web page."
LOCKED: Final = "Too many wrong codes. Try again later."
CODE_CHARS: Final = 6
"""The approval reply code: the first characters of the approval id (Section 11.9)."""

OWN_COMMANDS: Final = frozenset(
    {
        "/new",
        "/stop",
        "/steer",
        "/bg",
        "/help",
        "/pair",
        "/start",
        "/skills",
        "/claude",
        "/codex",
        "/sama",
    }
)
"""Commands of the gateway. Any other command can be a skill (Section 9.5)."""

_COMMAND: Final = re.compile(r"/([A-Za-z0-9_]{1,32})(?:\s+(.*))?", re.S)
_DECISION: Final = re.compile(
    rf"(yes|approve|no|deny|always)\s+([0-9a-f]{{{CODE_CHARS}}})[.!]?", re.IGNORECASE
)
_CHOICES: Final = {"yes": "y", "approve": "y", "no": "n", "deny": "n", "always": "a"}


def command_of(text: str) -> tuple[str | None, str]:
    """``("/new", "rest")`` for a command, else ``(None, text)``."""
    match = _COMMAND.fullmatch(text)
    if match is None:
        return None, text
    name, rest = match.groups()
    return f"/{name.lower()}", (rest or "").strip()


def approval_code(approval_id: uuid.UUID) -> str:
    """The code that a reply names to decide an approval."""
    return approval_id.hex[:CODE_CHARS].upper()


class Unlinked(Exception):
    """The sender lost the link that it had when the run started."""


class RunOutbox(Protocol):
    """Sends the output of one run to one conversation."""

    async def send_message(self, text: str, *, reply_to: Any = None) -> Any: ...

    async def edit_message(self, message_id: Any, text: str) -> None: ...

    async def send_media(self, media: StoredMedia, *, reply_to: Any = None) -> None: ...

    async def send_card(self, row: Any) -> None: ...


@dataclass(frozen=True, slots=True)
class Conversation:
    """One direct conversation: the sender and the binding key of its session."""

    sender: str
    """The external id of the sender: a phone number or an email address."""
    key: str
    """The ``session_bindings.external_key`` of the conversation."""


class DirectGateway[C: Conversation](abc.ABC):
    channel: ClassVar[str]
    label: ClassVar[str]
    """The name of the channel in texts for the user."""
    help_text: ClassVar[str]
    reply_codes: ClassVar[bool] = True
    """True when a reply such as ``yes AB12CD`` decides an approval."""
    errors: ClassVar[tuple[type[Exception], ...]] = ()
    """The errors of the transport. A follower stops at one of them."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        integration_id: uuid.UUID,
        org_id: uuid.UUID,
        settings: FollowSettings | None = None,
        queue: RunQueue | None = None,
        gate: GateService | None = None,
        skills: SkillLibrary | None = None,
        reviews: ReviewApplier | None = None,
        transcriber: Transcriber | None = None,
        lockout: Lockout | None = None,
        followers: set[object] | None = None,
    ) -> None:
        self._engine = engine
        self.integration_id = integration_id
        self.org_id = org_id
        self._settings = settings or FollowSettings()
        self._queue = queue or RunQueue(engine)
        self._gate = gate or GateService(engine)
        self._skills = skills
        self._reviews = reviews
        self._transcriber = transcriber
        self._lockout = lockout or Lockout()
        self._followers: set[object] = set() if followers is None else followers
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        self._locks = KeyedLocks()

    # -- transport ------------------------------------------------------------------------

    @abc.abstractmethod
    async def _say(self, chat: C, text: str) -> None:
        """Sends a short answer. Errors are logged, not raised."""

    @abc.abstractmethod
    def _outbox(self, chat: C, target: Target, allowed: Callable[[], Awaitable[bool]]) -> RunOutbox:
        """The outbox of one run. Each send first asks ``allowed`` and raises ``Unlinked``."""

    def _framed(self, chat: C, text: str) -> str:
        """The text of the run message. A subclass can add context, such as a subject."""
        return text

    def _pair_done(self) -> str:
        return f"This {self.label} is linked to sama. Send a message to start."

    def _pair_prompt(self) -> str:
        return (
            f"This {self.label} is not linked to sama. Open the sama web page, make a"
            f" {self.channel_name} pairing code, and send the code here."
        )

    @property
    def channel_name(self) -> str:
        return "WhatsApp" if self.channel == "whatsapp" else "email"

    # -- background work ------------------------------------------------------------------

    def seen(self, key: str) -> bool:
        """True when ``key`` came before. Each new key is kept (the last 2,000)."""
        if not key:
            return False
        if key in self._seen:
            return True
        self._seen[key] = None
        while len(self._seen) > 2000:
            self._seen.popitem(last=False)
        return False

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _one_at_a_time(self, chat: C, work: Awaitable[None]) -> None:
        """Runs ``work`` while it holds the lock of the sender. Errors are logged and never
        reach the provider."""
        try:
            async with self._locks.hold(chat.sender):
                await work
        except Exception:
            log.exception("a %s message failed", self.channel)

    async def drain(self) -> None:
        """Waits for the background work (for tests)."""
        await wait_for_tasks(self._tasks)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # -- inbound --------------------------------------------------------------------------

    async def _identity(self, sender: str) -> Any:
        async with self._engine.connect() as connection:
            return (
                await connection.execute(
                    select(ChannelIdentity).where(
                        ChannelIdentity.integration_id == self.integration_id,
                        ChannelIdentity.external_id == sender,
                    )
                )
            ).one_or_none()

    async def _message(
        self,
        chat: C,
        text: str,
        *,
        codes: Sequence[str] = (),
        media: Callable[[], Awaitable[tuple[str, list[NewAttachment]] | None]] | None = None,
    ) -> None:
        """Handles one message of ``chat``. ``codes`` are the texts that can hold a pairing
        code (for example the subject of an email). ``media`` loads the voice text and the
        images of the message; it gives ``None`` after it told the sender why it cannot."""
        command, rest = command_of(text)
        identity = await self._identity(chat.sender)
        if command in ("/start", "/pair") and rest:
            await self._pair(chat, rest)
            return
        if identity is None:
            found = next(
                (code for code in (normalize(t) for t in (text, *codes)) if code is not None), None
            )
            if found is not None:
                await self._pair(chat, found)
            else:
                await self._prompt(chat)
            return
        target = Target.private(identity)
        async with self._engine.connect() as connection:
            grants = await load_grants(connection, target.org_id, target.user_id)
        if not grants.sees_workspace(target.workspace_id):
            await self._say(chat, NO_ACCESS)
            return
        if self.reply_codes and await self._decision(chat, text, target, grants):
            return
        if command == "/steer":
            if not rest:
                await self._say(chat, STEER_EMPTY.format(command="/steer"))
                return
            if await self._steer(chat, rest, target, grants):
                return
            command, text = None, rest
        coding = parse_command(text) if command in ("/claude", "/codex", "/sama") else None
        if coding is not None and coding.switch:
            # The direct mode of the session (Section 15.4).
            bound = await self._ensure(chat, target, grants)
            try:
                answer = await set_direct(
                    self._engine,
                    target.org_id,
                    bound.session_id,
                    coding.agent,
                    user_id=target.user_id,
                )
            except SwitchNotAllowedError as error:
                answer = str(error)
            await self._say(chat, answer)
            return
        if coding is not None and coding.agent is not None:
            # ``/claude <task>``: one message to the coding agent, with no skill expansion.
            if len(self._followers) >= self._settings.max_followers:
                await self._say(chat, BUSY)
                return
            await self._enqueue(
                chat, target, grants, coding.task, [], coding_agent=coding.agent.value
            )
            return
        parallel = False
        if command == "/bg":
            if not rest:
                await self._say(chat, BG_EMPTY.format(command="/bg"))
                return
            command, text, parallel = None, rest, True
        skill_command = (
            command is not None and command not in OWN_COMMANDS and self._skills is not None
        )
        if command is not None and not skill_command:
            await self._command(chat, command, target, grants, rest)
            return
        images: list[NewAttachment] = []
        if media is not None:
            loaded = await media()
            if loaded is None:
                return
            voice, images = loaded
            text = text or voice
        if not text and not images:
            return
        if len(text) > MAX_INPUT_CHARS:
            await self._say(chat, "The message is too long.")
            return
        if len(self._followers) >= self._settings.max_followers:
            log.warning("too many runs to follow; %s must try again", self.channel)
            await self._say(chat, BUSY)
            return
        if text.startswith("/") and self._skills is not None:
            expanded = await self._expand(chat, target, grants, text)
            if expanded is not None:
                text = expanded
            elif skill_command:
                await self._say(chat, self.help_text)
                return
        if (
            not parallel
            and not images
            and not skill_command
            and not text.startswith("/")
            and await self._join(chat, self._framed(chat, text), target, grants)
        ):
            return
        await self._enqueue(
            chat,
            target,
            grants,
            with_markers(self._framed(chat, text), images),
            images,
            parallel=parallel,
        )

    async def _join(self, chat: C, text: str, target: Target, grants: Grants) -> bool:
        """Busy mode ``steer`` (M7.1): adds a plain message to the running run of the session.
        False when no run takes it: then the message starts or queues a run."""
        current = await self._session(chat, target, grants, create=False)
        if current is None or not text.strip():
            return False
        try:
            steered = await self._queue.steer(
                org_id=target.org_id,
                session_id=current.session_id,
                text=text,
                user_id=target.user_id,
                busy_mode=True,
            )
        except QueueError as error:
            await self._say(chat, str(error))
            return True
        if steered is None:
            return False
        await self._say(chat, JOINED)
        return True

    async def _enqueue(
        self,
        chat: C,
        target: Target,
        grants: Grants,
        text: str,
        images: list[NewAttachment],
        *,
        coding_agent: str | None = None,
        parallel: bool = False,
    ) -> None:
        place = object()
        self._followers.add(place)
        try:
            bound = await self._ensure(chat, target, grants)
            run_id = await self._queue.enqueue(
                org_id=target.org_id,
                session_id=bound.session_id,
                text=text,
                user_id=target.user_id,
                attachments=images,
                coding_agent=coding_agent,
                parallel=parallel,
            )
        except QueueError as error:
            self._followers.discard(place)
            await self._say(chat, str(error))
            return
        except BaseException:
            self._followers.discard(place)
            raise
        self._spawn(self._follow(chat, target, run_id, place))
        notice = await busy_text(
            self._queue, target.org_id, bound.session_id, run_id, steer="/steer", stop="/stop"
        )
        if notice is not None:
            await self._say(chat, notice)

    async def _prompt(self, chat: C) -> None:
        await self._say(chat, self._pair_prompt())

    async def _pair(self, chat: C, text: str) -> None:
        key = f"{self.channel}:{self.integration_id}:{chat.sender}"
        # ``attempt`` counts the try before the database work, so tries at the same time
        # cannot pass the limit.
        if not self._lockout.attempt(key):
            await self._say(chat, LOCKED)
            return
        code = normalize(text)
        linked = None
        if code is not None:
            async with self._engine.begin() as connection:
                linked = await redeem(
                    connection,
                    integration_id=self.integration_id,
                    code=code,
                    kind="user",
                    external_id=chat.sender,
                )
        if linked is None:
            await self._say(chat, WRONG_CODE)
            return
        self._lockout.succeeded(key)
        await self._say(chat, self._pair_done())

    async def _decision(self, chat: C, text: str, target: Target, grants: Grants) -> bool:
        """Decides an approval of the session of ``chat`` from a reply such as
        ``yes AB12CD``. False when ``text`` is not such a reply."""
        match = _DECISION.fullmatch(text.strip())
        if match is None:
            return False
        choice, code = _CHOICES[match.group(1).lower()], match.group(2).upper()
        current = await self._session(chat, target, grants, create=False)
        ids: list[uuid.UUID] = []
        if current is not None:
            async with self._engine.connect() as connection:
                ids = list(
                    (
                        await connection.scalars(
                            select(Approval.id).where(
                                Approval.org_id == target.org_id,
                                Approval.session_id == current.session_id,
                                Approval.status == "pending",
                            )
                        )
                    ).all()
                )
        chosen = [approval_id for approval_id in ids if approval_code(approval_id) == code]
        if len(chosen) != 1:
            await self._say(chat, f"No open approval of this chat has the code {code}.")
            return True
        try:
            decided = await decide(
                self._engine,
                self._gate,
                self._queue,
                org_id=target.org_id,
                user_id=target.user_id,
                approval_id=chosen[0],
                approve=choice in "ya",
                always_allow=choice == "a",
                reviews=self._reviews,
            )
        except ApprovalError as error:
            await self._say(chat, str(error))
            return True
        await self._say(chat, f"{decided.outcome}: {decided.tool_name}.")
        return True

    async def _steer(self, chat: C, note: str, target: Target, grants: Grants) -> bool:
        current = await self._session(chat, target, grants, create=False)
        if current is None:
            return False
        try:
            steered = await self._queue.steer(
                org_id=target.org_id,
                session_id=current.session_id,
                text=note,
                user_id=target.user_id,
            )
        except QueueError as error:
            await self._say(chat, str(error))
            return True
        if steered is None:
            return False
        await self._say(chat, STEERED)
        return True

    async def _command(
        self, chat: C, command: str, target: Target, grants: Grants, rest: str = ""
    ) -> None:
        if command == "/new":
            current = await self._session(chat, target, grants, create=False)
            profile = target.agent_profile_id if current is None else current.agent_profile_id
            await new_session(self._engine, self.channel, chat.key, target, profile)
            await self._say(chat, "A new session started.")
        elif command == "/stop":
            current = await self._session(chat, target, grants, create=False)
            answer = await stop_reply(
                self._engine,
                self._queue,
                target.org_id,
                None if current is None else current.session_id,
                rest,
            )
            await self._say(chat, answer)
        elif command == "/skills":
            await self._skill_list(chat, target, grants)
        else:
            await self._say(chat, self.help_text)

    async def _actor(self, skills: SkillLibrary, chat: C, target: Target, grants: Grants) -> Any:
        current = await self._session(chat, target, grants, create=False)
        profile = target.agent_profile_id if current is None else current.agent_profile_id
        async with self._engine.connect() as connection:
            return await skills.actor(
                connection,
                org_id=target.org_id,
                workspace_id=target.workspace_id,
                profile_id=profile,
                user_id=target.user_id,
                private=True,
            )

    async def _expand(self, chat: C, target: Target, grants: Grants, text: str) -> str | None:
        if self._skills is None:
            return None
        actor = await self._actor(self._skills, chat, target, grants)
        return await self._skills.expand(actor, text)

    async def _skill_list(self, chat: C, target: Target, grants: Grants) -> None:
        if self._skills is None:
            await self._say(chat, self.help_text)
            return
        actor = await self._actor(self._skills, chat, target, grants)
        async with self._engine.connect() as connection:
            skills = await self._skills.index(connection, actor)
        if not skills:
            await self._say(chat, "There are no skills.")
            return
        lines = [f"/{s.name.replace('-', '_')} - {s.description}" for s in skills]
        await self._say(chat, "Skills (send /<skill> and your request):\n" + "\n".join(lines))

    async def _session(
        self, chat: C, target: Target, grants: Grants, *, create: bool
    ) -> Bound | None:
        """The session of the conversation. A session that the user cannot use now is not
        reused."""
        bound = await find_session(self._engine, self.channel, chat.key, target, grants)
        if bound is not None or not create:
            return bound
        return await self._ensure(chat, target, grants)

    async def _ensure(self, chat: C, target: Target, grants: Grants) -> Bound:
        """The session of the conversation, or a new one."""
        bound = await find_session(self._engine, self.channel, chat.key, target, grants)
        if bound is not None:
            return bound
        return await new_session(
            self._engine, self.channel, chat.key, target, target.agent_profile_id
        )

    # -- outbound -------------------------------------------------------------------------

    async def _may_deliver(self, chat: C, target: Target) -> bool:
        """True when the sender is still linked to the user of the run and the user still
        sees the workspace (Section 11.3)."""
        async with self._engine.connect() as connection:
            linked = await connection.scalar(
                select(ChannelIdentity.id).where(
                    ChannelIdentity.integration_id == self.integration_id,
                    ChannelIdentity.external_id == chat.sender,
                    ChannelIdentity.org_id == target.org_id,
                    ChannelIdentity.user_id == target.user_id,
                )
            )
            if linked is None:
                return False
            grants = await load_grants(connection, target.org_id, target.user_id)
        return grants.sees_workspace(target.workspace_id)

    async def _follow(self, chat: C, target: Target, run_id: uuid.UUID, place: object) -> None:
        """Follows the run, then the run of its late ``/steer`` notes. The output stops when
        the sender loses its link or a handoff gives the run to another chat."""
        org_id = target.org_id

        async def allowed() -> bool:
            if not await self._may_deliver(chat, target):
                return False
            async with self._engine.connect() as connection:
                return not await moved_away(connection, org_id, run_id, None)

        outbox = self._outbox(chat, target, allowed)
        try:
            while True:
                reply = FinalReply(outbox, None, 0)
                status = await follow_run(
                    self._engine, org_id, run_id, reply, self._settings, outbox.send_card
                )
                # The outbox splits by the limits of the channel (one email, or WhatsApp
                # messages), so the stored answer goes to it whole.
                await send_ending(
                    self._engine,
                    outbox,
                    org_id,
                    run_id,
                    status,
                    reply,
                    max_chars=MAX_STORED_ANSWER_CHARS,
                    split_at=None,
                )
                await send_media(self._engine, outbox, org_id, run_id)
                after = await self._queue.requeued_run(org_id=org_id, run_id=run_id)
                if after is None:
                    break
                async with self._engine.connect() as connection:
                    if await moved_away(connection, org_id, run_id, None):
                        break
                run_id = after
        except Unlinked:
            log.warning("the %s sender lost its link; run=%s is lost", self.channel, run_id)
        except self.errors as error:
            log.warning("a %s reply failed run=%s error=%s", self.channel, run_id, error)
        finally:
            self._followers.discard(place)
