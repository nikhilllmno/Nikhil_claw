"""The WhatsApp channel (docs/architecture.md, Section 11.9).

Each org connects one WhatsApp business number through Meta, Twilio, or Gupshup. The
``WhatsAppHub`` (``sama.integrations.whatsapp_hub``) checks the webhook token and the signature
of the provider and gives each message to ``accept``. A WhatsApp chat is a ``private`` session of
the linked user. WhatsApp has no groups for business numbers, so there are no shared sessions.

- Pairing: the user sends the code of the web page to the number.
- Output: WhatsApp cannot edit a message, so the reply of a run goes out when the run ends or
  pauses (``FinalReply``), cut at the text limit of the provider.
- Approvals: a card with the buttons Approve and Deny (Meta and Gupshup), or with reply codes
  (Twilio): ``yes AB12CD`` or ``no AB12CD``. A button press sends the same text.
- Media: images go to ``vision_analyze`` (Section 11.7). A voice note is transcribed. Images and
  voice notes that a run makes go out through a signed link that the provider reads.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

from sama.gateway.direct import Conversation, DirectGateway, RunOutbox, Unlinked, approval_code
from sama.gateway.sessions import ALWAYS_LABEL, Target, approval_text, offers_always, split_point
from sama.gateway.transcribe import TranscribeError
from sama.gateway.whatsapp_providers import (
    Button,
    Inbound,
    MediaRef,
    WhatsAppError,
    WhatsAppProvider,
)
from sama.media.generate import StoredMedia
from sama.media.images import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_MESSAGE,
    TOO_LARGE,
    TOO_MANY,
    UNREADABLE,
    ImageRejected,
    NewAttachment,
    check_image,
    is_image,
    normalize_mime,
)

log: Final = logging.getLogger("sama.gateway.whatsapp")

CHANNEL: Final = "whatsapp"
MAX_VOICE_BYTES: Final = 10 * 1024 * 1024
UNSUPPORTED_MEDIA: Final = "sama reads only images and voice notes here. Send text."
HELP: Final = (
    "Send a message to talk to sama.\n"
    "/new - start a new session\n"
    "/stop - stop the current run\n"
    "/steer <text> - add a note to the current run\n"
    "/skills - list the skills. /<skill> <request> uses a skill\n"
    "/pair <code> - link this number with a code from the sama web page\n"
    "yes <code> or no <code> - decide an approval"
)

MediaLink = Callable[[StoredMedia], str]
"""Makes the signed link that the provider reads to send a file of a run."""


@dataclass(frozen=True, slots=True)
class WhatsAppChat(Conversation):
    pass


def card_buttons(row: Any) -> list[Button]:
    code = approval_code(row.id)
    buttons = [Button("Approve", f"yes {code}"), Button("Deny", f"no {code}")]
    if offers_always(row):
        buttons.append(Button(ALWAYS_LABEL, f"always {code}"))
    return buttons


def card_text(row: Any, *, with_codes: bool) -> str:
    text = approval_text(row)
    if not with_codes:
        return text
    code = approval_code(row.id)
    lines = [f"Reply yes {code} to approve or no {code} to deny."]
    if offers_always(row):
        lines.append(f"Reply always {code} to approve and always allow this tool for you.")
    return text + "\n\n" + " ".join(lines)


class _Outbox(RunOutbox):
    def __init__(
        self,
        provider: WhatsAppProvider,
        to: str,
        allowed: Callable[[], Awaitable[bool]],
        media_link: MediaLink | None,
    ) -> None:
        self._provider = provider
        self._to = to
        self._allowed = allowed
        self._media_link = media_link

    async def _check(self) -> None:
        if not await self._allowed():
            raise Unlinked

    async def send_message(self, text: str, *, reply_to: Any = None) -> Any:
        sent = ""
        limit = self._provider.max_text
        while text.strip():
            cut = split_point(text, limit) if len(text) > limit else len(text)
            await self._check()
            sent = await self._provider.send_text(self._to, text[:cut])
            text = text[cut:]
        return sent

    async def edit_message(self, message_id: Any, text: str) -> None:
        """WhatsApp cannot edit a message. ``FinalReply`` never asks for an edit."""
        await self.send_message(text)

    async def send_media(self, media: StoredMedia, *, reply_to: Any = None) -> None:
        if self._media_link is None:
            return
        await self._check()
        await self._provider.send_media(self._to, media, self._media_link(media))

    async def send_card(self, row: Any) -> None:
        await self._check()
        if self._provider.buttons:
            await self._provider.send_buttons(
                self._to, card_text(row, with_codes=False), card_buttons(row)
            )
        else:
            await self._provider.send_text(self._to, card_text(row, with_codes=True))


class WhatsAppGateway(DirectGateway[WhatsAppChat]):
    channel = CHANNEL
    label = "WhatsApp number"
    help_text = HELP
    errors = (WhatsAppError,)

    def __init__(
        self,
        engine: Any,
        provider: WhatsAppProvider,
        *,
        integration_id: uuid.UUID,
        org_id: uuid.UUID,
        media_link: MediaLink | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(engine, integration_id=integration_id, org_id=org_id, **kwargs)
        self.provider = provider
        self._media_link = media_link

    def chat(self, sender: str) -> WhatsAppChat:
        return WhatsAppChat(sender, f"{self.integration_id}:chat:{sender}")

    def accept(self, message: Inbound) -> None:
        """Takes one message from the webhook. A repeated message is dropped; the others are
        handled in the background, so the provider gets its answer at once."""
        if self.seen(message.id):
            return
        self._spawn(self.handle(message))

    async def handle(self, message: Inbound) -> None:
        chat = self.chat(message.sender)

        async def media() -> tuple[str, list[NewAttachment]] | None:
            return await self._load(chat, message.media, has_text=bool(message.text))

        await self._one_at_a_time(
            chat, self._message(chat, message.text, media=media if message.media else None)
        )

    async def _load(
        self, chat: WhatsAppChat, refs: tuple[MediaRef, ...], *, has_text: bool
    ) -> tuple[str, list[NewAttachment]] | None:
        """The voice text and the images of a message, or ``None`` after the sender heard why
        sama cannot use them."""
        images = [r for r in refs if is_image(r.mime_type)]
        voices = [r for r in refs if normalize_mime(r.mime_type).startswith("audio/")]
        if not images and not voices:
            if has_text:
                return "", []
            await self._say(chat, UNSUPPORTED_MEDIA)
            return None
        if len(images) > MAX_IMAGES_PER_MESSAGE:
            await self._say(chat, TOO_MANY)
            return None
        loaded: list[NewAttachment] = []
        for ref in images:
            try:
                try:
                    data = await self.provider.download(ref, max_bytes=MAX_IMAGE_BYTES)
                except WhatsAppError as error:
                    log.warning("a WhatsApp image download failed error=%s", error)
                    raise ImageRejected(
                        TOO_LARGE if "too large" in str(error) else UNREADABLE
                    ) from None
                loaded.append(check_image(data, name=ref.name, claimed_mime=ref.mime_type))
            except ImageRejected as error:
                await self._say(chat, str(error))
                return None
        voice = ""
        if voices and not has_text:
            voice = await self._transcribe(chat, voices[0])
            if not voice and not loaded:
                return None
        return voice, loaded

    async def _transcribe(self, chat: WhatsAppChat, ref: MediaRef) -> str:
        if self._transcriber is None:
            await self._say(chat, "Voice notes are off. Send text.")
            return ""
        try:
            audio = await self.provider.download(ref, max_bytes=MAX_VOICE_BYTES)
            text = await self._transcriber.transcribe(
                audio, filename="voice.ogg", mime_type=normalize_mime(ref.mime_type) or "audio/ogg"
            )
        except (WhatsAppError, TranscribeError) as error:
            log.warning("a WhatsApp voice note failed error=%s", error)
            await self._say(chat, "sama cannot read this voice note.")
            return ""
        if not text:
            await self._say(chat, "The voice note has no words.")
        return text

    async def _say(self, chat: WhatsAppChat, text: str) -> None:
        try:
            await self.provider.send_text(chat.sender, text)
        except WhatsAppError as error:
            log.warning("a WhatsApp send failed error=%s", error)

    def _outbox(
        self, chat: WhatsAppChat, target: Target, allowed: Callable[[], Awaitable[bool]]
    ) -> RunOutbox:
        return _Outbox(self.provider, chat.sender, allowed, self._media_link)
