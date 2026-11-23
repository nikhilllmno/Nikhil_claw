"""Images that the web composer sends with a message (docs/architecture.md, Section 11.7).

The body is ``multipart/form-data``. The size limit applies before the parser keeps a byte, and
each image goes through the same check as an image from Telegram or Slack.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from sama.api.events import MAX_MESSAGE_CHARS
from sama.media.images import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_MESSAGE,
    TOO_LARGE,
    TOO_MANY,
    ImageRejected,
    NewAttachment,
    check_image,
    normalize_mime,
    sniff,
)

MAX_PART_HEADER_BYTES: Final = 16_384
"""The room for the boundaries and the headers of the parts."""
MAX_UPLOAD_BYTES: Final = (
    MAX_IMAGES_PER_MESSAGE * (MAX_IMAGE_BYTES + 1_024)
    + MAX_MESSAGE_CHARS * 4
    + MAX_PART_HEADER_BYTES
)
"""The size limit of the whole body: the images, the text (4 bytes for a character), and the
parts. A larger body gets ``413`` before the parser reads it."""
EMPTY: Final = "Write a message or add an image."

_TEXT_MIME_FALLBACK: Final = frozenset({"", "application/octet-stream"})
_TRUE: Final = frozenset({"true", "1", "on"})
_FALSE: Final = frozenset({"false", "0", "off", ""})


@dataclass(frozen=True, slots=True)
class WebMessage:
    text: str
    parallel: bool
    images: tuple[NewAttachment, ...]


def too_large() -> HTTPException:
    return HTTPException(status_code=413, detail="the body is too large")


async def read_message(request: Request) -> WebMessage:
    """Reads and checks a message with images. Raises ``HTTPException`` with ``413`` for a body
    over the limit and ``422`` for text or an image that sama refuses."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise too_large()
    seen = 0
    receive = request.receive

    async def bounded() -> object:
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            seen += len(message.get("body", b""))
            if seen > MAX_UPLOAD_BYTES:
                raise too_large()
        return message

    counted = Request(request.scope, bounded)  # type: ignore[arg-type]
    try:
        form = await counted.form(
            max_files=MAX_IMAGES_PER_MESSAGE + 1,
            max_fields=8,
            max_part_size=MAX_MESSAGE_CHARS * 4 + 1_024,
        )
    except StarletteHTTPException as error:
        if error.status_code == 400 and "Too many files" in str(error.detail):
            raise HTTPException(status_code=422, detail=TOO_MANY) from None
        raise
    try:
        text = form.get("text", "")
        if not isinstance(text, str):
            raise HTTPException(status_code=422, detail="text must be a text field")
        parallel_text = form.get("parallel", "")
        if not isinstance(parallel_text, str) or parallel_text.lower() not in _TRUE | _FALSE:
            raise HTTPException(status_code=422, detail="parallel must be true or false")
        if len(text) > MAX_MESSAGE_CHARS:
            raise HTTPException(status_code=422, detail="the message is too long")
        files = form.getlist("images")
        if len(files) > MAX_IMAGES_PER_MESSAGE:
            raise HTTPException(status_code=422, detail=TOO_MANY)
        images: list[NewAttachment] = []
        for item in files:
            if not isinstance(item, UploadFile):
                raise HTTPException(status_code=422, detail="images must be files")
            # One byte more than the limit is enough to see that the file is too large.
            data = await item.read(MAX_IMAGE_BYTES + 1)
            try:
                images.append(
                    check_image(data, name=item.filename, claimed_mime=_claimed(item, data))
                )
            except ImageRejected as error:
                status = 413 if str(error) == TOO_LARGE else 422
                raise HTTPException(status_code=status, detail=str(error)) from None
        if not text.strip() and not images:
            raise HTTPException(status_code=422, detail=EMPTY)
        return WebMessage(text=text, parallel=parallel_text.lower() in _TRUE, images=tuple(images))
    finally:
        await form.close()


def _claimed(file: UploadFile, data: bytes) -> str:
    """The type that the browser gave. A browser can give none for a rare file name: then the
    first bytes name the type. ``check_image`` still reads the first bytes and they must agree."""
    claimed = normalize_mime(file.content_type)
    if claimed in _TEXT_MIME_FALLBACK:
        return sniff(data) or ""
    return claimed
