"""Images from channels (docs/architecture.md, Section 11.7).

A gateway checks each image here before it stores it. The check does not trust the type that
the channel gives: it reads the first bytes of the file and the two must agree.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from sama.db.models import ATTACHMENT_MIME_TYPES, MAX_ATTACHMENT_BYTES

MAX_IMAGE_BYTES: Final = MAX_ATTACHMENT_BYTES
"""The size limit of one image."""
MAX_IMAGES_PER_MESSAGE: Final = 4
"""A message with more images gets only this number of them."""
MAX_NAME_CHARS: Final = 80

IMAGE_MIME_TYPES: Final = frozenset(ATTACHMENT_MIME_TYPES)

TOO_LARGE: Final = "That image is too large. The limit is 10 MB."
UNSUPPORTED: Final = "sama reads only PNG, JPEG, GIF, and WebP images."
UNREADABLE: Final = "sama could not read that image. Send it again."
TOO_MANY: Final = (
    f"sama reads only the first {MAX_IMAGES_PER_MESSAGE} images of a message."
    " Send the other images in a new message."
)

_ALIASES: Final = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}
_UNSAFE: Final = re.compile(r"[^A-Za-z0-9._-]+")


class ImageRejected(Exception):
    """The image cannot be stored. The text is for the sender: it has no detail that the
    sender does not already know."""


def normalize_mime(value: object) -> str:
    """The media type without parameters, in lower case, with the common aliases mapped."""
    text = str(value or "").split(";", 1)[0].strip().lower()
    return _ALIASES.get(text, text)


def is_image(mime_type: object) -> bool:
    """True for a type that sama accepts."""
    return normalize_mime(mime_type) in IMAGE_MIME_TYPES


def sniff(data: bytes) -> str | None:
    """The type of the bytes from their first bytes, or ``None`` for an unknown type."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def clean_name(name: object, *, fallback: str = "image") -> str:
    """A file name that is safe in a marker, in a log, and in a prompt."""
    cleaned = _UNSAFE.sub("_", str(name or "")).strip("._")
    return cleaned[:MAX_NAME_CHARS] or fallback


@dataclass(frozen=True, slots=True)
class NewAttachment:
    """An image to store with a message. The gateway makes the id first, so that the text of
    the message can name the image before the database has the message."""

    name: str
    mime_type: str
    data: bytes = field(repr=False)
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def check_image(data: bytes, *, name: object, claimed_mime: object) -> NewAttachment:
    """Checks the bytes of a downloaded image. Raises ``ImageRejected``."""
    claimed = normalize_mime(claimed_mime)
    if claimed not in IMAGE_MIME_TYPES:
        raise ImageRejected(UNSUPPORTED)
    if not data:
        raise ImageRejected(UNREADABLE)
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageRejected(TOO_LARGE)
    if sniff(data) != claimed:
        raise ImageRejected(UNREADABLE)
    return NewAttachment(name=clean_name(name), mime_type=claimed, data=data)


def marker(attachment: NewAttachment) -> str:
    """The line in the message text that tells the agent about the image."""
    return f"[image attached: {attachment.id} {attachment.name}; use vision_analyze to look at it]"


def with_markers(text: str, attachments: Sequence[NewAttachment]) -> str:
    """The message text and one marker line for each image."""
    lines = [marker(attachment) for attachment in attachments]
    return "\n".join(part for part in (text, *lines) if part)
