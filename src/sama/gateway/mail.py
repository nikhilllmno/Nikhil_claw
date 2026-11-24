"""The email channel (docs/architecture.md, Section 11.9).

Each org connects one inbound address, for example ``sama@mail.example.com``. SendGrid Inbound
Parse posts each email to the address to ``POST /email/<integration id>/inbound``, and the
``EmailHub`` (``sama.integrations.email_hub``) checks the signature. The gateway:

- drops an email that did not pass DKIM or SPF for the domain of its ``From`` address, and an
  email that did not go to the inbound address;
- maps an email thread to a ``private`` session of the linked sender. A new email starts a new
  thread. A reply goes to ``<local>+t<token>@<domain>``, and the ``Message-ID`` of each email of
  sama holds the token too, so a reply finds its thread. The binding key holds the sender, so
  one sender cannot reach the thread of another;
- sends the reply of a run as one email when the run ends or pauses, in the same thread;
- sends an approval card as an email with signed links (``sama.gateway.links``). A link opens
  a page of sama with a button that decides: a mail scanner that opens the link decides
  nothing;
- stores the images of an email for ``vision_analyze`` (Section 11.7) and attaches the images
  and the audio that a run makes.
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

from sama.gateway.direct import Conversation, DirectGateway, RunOutbox, Unlinked
from sama.gateway.links import approval_token
from sama.gateway.mail_inbound import MailIn, addressed_to, authenticated, reply_text, thread_of
from sama.gateway.sendgrid_client import Attachment, Mail, SendGridClient, SendGridError
from sama.gateway.sessions import ALWAYS_LABEL, Target, approval_text, offers_always
from sama.media.generate import StoredMedia
from sama.media.images import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_MESSAGE,
    TOO_LARGE,
    TOO_MANY,
    ImageRejected,
    NewAttachment,
    check_image,
    is_image,
)

log: Final = logging.getLogger("sama.gateway.mail")

CHANNEL: Final = "email"
MAX_MAIL_CHARS: Final = 100_000
"""The longest text of one email of sama. A longer reply goes on in a new email."""
PROMPT_EVERY_S: Final = 3600.0
"""An unknown sender gets the pairing prompt at most once in this time."""
HELP: Final = (
    "Write to this address to talk to sama. Each new email starts a new session, and a reply\n"
    "goes on in the same session.\n"
    "Write one of these commands as the whole text of an email:\n"
    "An email while sama works on the thread joins the current run at its next step.\n"
    "/bg <task> - start a run at the same time as the current run of this thread\n"
    "/stop - stop all runs of this thread. /stop <number> stops one run\n"
    "/steer <text> - add a note to the current run of this thread\n"
    "/skills - list the skills. /<skill> <request> uses a skill\n"
    "/claude <task> or /codex <task> - send a coding task to Claude Code or Codex. /claude or\n"
    "/codex alone sends each message to it until /sama\n"
    "/pair <code> - link this address with a code from the sama web page"
)


@dataclass(frozen=True, slots=True)
class EmailAddress:
    """The inbound address of one email integration."""

    integration_id: uuid.UUID
    org_id: uuid.UUID
    address: str
    name: str = "sama"

    @property
    def local(self) -> str:
        return self.address.partition("@")[0]

    @property
    def domain(self) -> str:
        return self.address.partition("@")[2]

    def reply_address(self, token: str) -> str:
        return f"{self.local}+t{token}@{self.domain}"


@dataclass(frozen=True, slots=True)
class EmailChat(Conversation):
    token: str
    subject: str
    message_id: str
    references: tuple[str, ...]
    new: bool
    """True when this email starts a new thread."""


def reply_subject(subject: str) -> str:
    subject = " ".join(subject.split()) or "sama"
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


class _Outbox(RunOutbox):
    def __init__(
        self,
        gateway: EmailGateway,
        chat: EmailChat,
        target: Target,
        allowed: Callable[[], Awaitable[bool]],
    ) -> None:
        self._gateway = gateway
        self._chat = chat
        self._target = target
        self._allowed = allowed

    async def _send(self, text: str, files: tuple[Attachment, ...] = ()) -> str:
        if not await self._allowed():
            raise Unlinked
        return await self._gateway.mail(self._chat, text, files)

    async def send_message(self, text: str, *, reply_to: Any = None) -> Any:
        sent = ""
        for start in range(0, len(text), MAX_MAIL_CHARS):
            sent = await self._send(text[start : start + MAX_MAIL_CHARS])
        return sent

    async def edit_message(self, message_id: Any, text: str) -> None:
        """An email cannot change. ``FinalReply`` never asks for an edit."""
        await self.send_message(text)

    async def send_media(self, media: StoredMedia, *, reply_to: Any = None) -> None:
        extension = {"image/png": "png", "audio/ogg": "ogg"}.get(media.mime_type, "bin")
        name = media.name or f"{media.kind}-{media.id.hex[:8]}.{extension}"
        text = media.caption or f"sama made this {media.kind}."
        await self._send(text, (Attachment(name, media.mime_type, media.data),))

    async def send_card(self, row: Any) -> None:
        await self._send(self._gateway.card(row, self._target.user_id))


class EmailGateway(DirectGateway[EmailChat]):
    channel = CHANNEL
    label = "email address"
    help_text = HELP
    reply_codes = False
    errors = (SendGridError,)

    def __init__(
        self,
        engine: Any,
        client: SendGridClient,
        *,
        address: EmailAddress,
        public_url: str,
        link_secret: str,
        clock: Callable[[], float] = time.time,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            engine, integration_id=address.integration_id, org_id=address.org_id, **kwargs
        )
        self._client = client
        self.address = address
        self._public_url = public_url.rstrip("/")
        self._link_secret = link_secret
        self._clock = clock
        self._prompted: OrderedDict[str, float] = OrderedDict()

    # -- inbound --------------------------------------------------------------------------

    def accept(self, mail: MailIn) -> None:
        """Takes one email from the webhook. A repeated email is dropped."""
        if self.seen(mail.message_id or f"{mail.sender}:{hash(mail.text)}"):
            return
        self._spawn(self.handle(mail))

    def chat_of(self, mail: MailIn) -> EmailChat | None:
        """The conversation of ``mail``, or ``None`` when sama must drop it."""
        tag = addressed_to(mail, self.address.address)
        if tag is None or mail.sender == self.address.address:
            return None
        if not authenticated(mail):
            log.warning("an email without DKIM or SPF for its sender domain is dropped")
            return None
        found = thread_of(mail, tag)
        token = found or secrets.token_hex(8)
        return EmailChat(
            mail.sender,
            f"{self.integration_id}:thread:{mail.sender}:{token}",
            token=token,
            subject=mail.subject,
            message_id=mail.message_id,
            references=mail.references,
            new=found is None,
        )

    async def handle(self, mail: MailIn) -> None:
        chat = self.chat_of(mail)
        if chat is None:
            return
        text = reply_text(mail.text) or ("" if mail.files else mail.subject)
        lines = [line.strip() for line in mail.text.splitlines() if line.strip()][:3]

        async def media() -> tuple[str, list[NewAttachment]] | None:
            return await self._images(chat, mail)

        await self._one_at_a_time(
            chat,
            self._message(
                chat,
                text,
                codes=(mail.subject, *lines),
                media=media if mail.files else None,
            ),
        )

    async def _images(
        self, chat: EmailChat, mail: MailIn
    ) -> tuple[str, list[NewAttachment]] | None:
        files = [f for f in mail.files if is_image(f.mime_type)]
        if len(files) > MAX_IMAGES_PER_MESSAGE:
            await self._say(chat, TOO_MANY)
            return None
        images: list[NewAttachment] = []
        for file in files:
            try:
                if len(file.data) > MAX_IMAGE_BYTES:
                    raise ImageRejected(TOO_LARGE)
                images.append(check_image(file.data, name=file.name, claimed_mime=file.mime_type))
            except ImageRejected as error:
                await self._say(chat, str(error))
                return None
        return "", images

    def _framed(self, chat: EmailChat, text: str) -> str:
        if chat.new and chat.subject and chat.subject not in text:
            return f"Subject: {chat.subject}\n\n{text}"
        return text

    async def _prompt(self, chat: EmailChat) -> None:
        """An unknown sender gets the prompt at most once an hour, so sama does not send many
        emails to an address that it does not know."""
        now = self._clock()
        last = self._prompted.get(chat.sender)
        if last is not None and now - last < PROMPT_EVERY_S:
            return
        self._prompted[chat.sender] = now
        while len(self._prompted) > 2000:
            self._prompted.popitem(last=False)
        await self._say(chat, self._pair_prompt())

    # -- outbound -------------------------------------------------------------------------

    async def mail(self, chat: EmailChat, text: str, files: tuple[Attachment, ...] = ()) -> str:
        """Sends one email in the thread of ``chat``."""
        references = [*chat.references, chat.message_id] if chat.message_id else [*chat.references]
        headers = {
            "Message-ID": f"<sama.{chat.token}.{secrets.token_hex(6)}@{self.address.domain}>"
        }
        if chat.message_id:
            headers["In-Reply-To"] = chat.message_id
        if references:
            headers["References"] = " ".join(references[-20:])
        return await self._client.send(
            Mail(
                sender=self.address.address,
                sender_name=self.address.name,
                to=chat.sender,
                subject=reply_subject(chat.subject),
                text=text,
                reply_to=self.address.reply_address(chat.token),
                headers=headers,
                attachments=files,
            )
        )

    def approval_link(self, approval_id: uuid.UUID, choice: Any, user_id: uuid.UUID) -> str:
        token = approval_token(self._link_secret, approval_id, choice, user_id, now=self._clock())
        return f"{self._public_url}/email/{self.integration_id}/approvals/{token}"

    def card(self, row: Any, user_id: uuid.UUID) -> str:
        lines = [
            approval_text(row),
            "",
            f"Approve: {self.approval_link(row.id, 'y', user_id)}",
            f"Deny: {self.approval_link(row.id, 'n', user_id)}",
        ]
        if offers_always(row):
            lines.append(f"{ALWAYS_LABEL}: {self.approval_link(row.id, 'a', user_id)}")
        lines += [
            "",
            "Each link opens a page of sama with a button. The links work for 24 hours."
            " Do not forward this email: the links decide as you.",
        ]
        return "\n".join(lines)

    async def _say(self, chat: EmailChat, text: str) -> None:
        try:
            await self.mail(chat, text)
        except SendGridError as error:
            log.warning("an email send failed error=%s", error)

    def _outbox(
        self, chat: EmailChat, target: Target, allowed: Callable[[], Awaitable[bool]]
    ) -> RunOutbox:
        return _Outbox(self, chat, target, allowed)
