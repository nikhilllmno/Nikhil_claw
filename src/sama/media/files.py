"""``file_send``: gives a file of the session sandbox to the user (docs/architecture.md,
Section 11.10; roadmap M8.6).

The agent writes files in its sandbox. The user cannot open a sandbox path, and a relative link
in the answer opens a page of the web app that does not exist. ``file_send`` copies the file
into ``generated_media`` (kind ``file``). The web app shows it as a download under the reply of
the run, the API gives it at ``GET /v1/sessions/{id}/media/{media_id}``, and the gateway of a
chat channel sends it as a document when the run ends.

Rules:

- The path is relative to the work folder, or absolute in the work folder or in ``/tmp``. A
  symbolic link counts only when its target is in one of these folders too.
- A file has at least 1 byte and at most ``MAX_GENERATED_MEDIA_BYTES``.
- The type comes from the file extension (``FILE_TYPES``). A file whose type has a known start
  (PDF, PNG, JPEG, GIF, WebP, zip and the Office formats) must start with it. A file with an
  unknown extension goes as ``application/octet-stream``.
- A run has at most ``MAX_FILES_PER_RUN`` files of any kind (the gateway limit). The count
  and the insert are one locked step.
"""

from __future__ import annotations

import logging
import posixpath
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from sama.db.models import MAX_GENERATED_MEDIA_BYTES
from sama.media.generate import (
    MAX_SENT_PER_RUN,
    PNG_MAGIC,
    GeneratedMediaStore,
    MediaResult,
    MediaStatus,
)
from sama.media.images import clean_name
from sama.media.meter import MediaError
from sama.sandbox.base import Sandbox, SandboxError, SandboxFileTooLargeError
from sama.tenancy.run_token import RunTokenClaims

if TYPE_CHECKING:
    # ``sama.tools`` imports the tool server, which imports this module.
    from sama.tools.output import SessionSandbox

log = logging.getLogger("sama.media")

KIND: Final = "file"
MAX_FILES_PER_RUN: Final = MAX_SENT_PER_RUN
"""A run has at most this many files of any kind, the number that a gateway sends."""
MAX_PATH_CHARS: Final = 1_000
MAX_NAME_CHARS: Final = 120
"""The longest ``name`` argument."""
MAX_STORED_NAME_CHARS: Final = 80
SCRATCH_DIR: Final = "/tmp"  # noqa: S108 - a folder of the sandbox, not of this host
"""The scratch folder of the sandbox. Code often writes charts and exports there."""
OCTET_STREAM: Final = "application/octet-stream"

_ZIP: Final = (b"PK\x03\x04",)
FILE_TYPES: Final[Mapping[str, str]] = {
    ".txt": "text/plain",
    ".log": "text/plain",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".tsv": "text/tab-separated-values",
    ".ics": "text/calendar",
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
    ".xml": "application/xml",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".gz": "application/gzip",
    ".tgz": "application/gzip",
    ".tar": "application/x-tar",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".mp4": "video/mp4",
}
"""The type of a file for each extension. Each type is in ``GENERATED_FILE_MIME_TYPES``."""

MAGIC: Final[Mapping[str, tuple[bytes, ...]]] = {
    "application/pdf": (b"%PDF",),
    "application/zip": _ZIP,
    "application/gzip": (b"\x1f\x8b",),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": _ZIP,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": _ZIP,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": _ZIP,
    "image/png": (PNG_MAGIC,),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
}
"""The bytes that a file of a type starts with. A file of another type is not checked."""

REAL_PATH: Final = 'p=$(realpath -e -- "$1") || exit 2; [ -d "$p" ] && exit 3; printf "%s" "$p"'
"""Prints the target of the path. Exit 2: no file. Exit 3: a folder."""
FOLDER_EXIT: Final = 3

NO_SANDBOX: Final = "This session has no sandbox, so there is no file to send."
OUTSIDE: Final = (
    "The path must be a file in the work folder ({workdir}) or in /tmp. Write the file there first."
)


def file_type(name: str) -> str:
    """The type of a file from its extension, else ``application/octet-stream``."""
    _, extension = posixpath.splitext(name.lower())
    return FILE_TYPES.get(extension, OCTET_STREAM)


def resolve(path: str, workdir: str) -> str:
    """The absolute, normalised path of ``path``. Raises ``MediaError`` for a path outside the
    work folder and ``/tmp``."""
    if not path.strip() or "\x00" in path or "\\" in path or len(path) > MAX_PATH_CHARS:
        raise MediaError("Give the path of a file in the work folder.")
    joined = posixpath.normpath(posixpath.join(workdir, path.strip()))
    if not allowed(joined, workdir):
        raise MediaError(OUTSIDE.format(workdir=workdir))
    return joined


def allowed(path: str, workdir: str) -> bool:
    """True if ``path`` is below the work folder or below ``/tmp`` (not the folder itself)."""
    for root in (posixpath.normpath(workdir), SCRATCH_DIR):
        if path.startswith(root.rstrip("/") + "/"):
            return True
    return False


def download_name(name: str | None, path: str) -> str:
    """A safe download name (letters, digits, ``.``, ``_``, and ``-``, at most
    ``MAX_STORED_NAME_CHARS``): the given name, else the base name of the path. It keeps the
    extension of the path when the given name has none."""
    base = posixpath.basename(path)
    wanted = (name or "").strip() or base
    stem, extension = posixpath.splitext(wanted)
    if not extension:
        extension = posixpath.splitext(base)[1]
    extension = clean_name(extension.lstrip("."), fallback="")[:10]
    suffix = f".{extension}" if extension else ""
    stem = clean_name(stem, fallback="file")[: MAX_STORED_NAME_CHARS - len(suffix)]
    return stem + suffix


def media_headers(kind: str, filename: str) -> dict[str, str]:
    """The headers of a download of ``generated_media`` (the API and the WhatsApp media link).
    A file of ``file_send`` is a download that the browser does not run: ``attachment``,
    ``nosniff``, and a sandbox CSP, so an HTML or SVG file cannot run script on the API origin.
    An image or a voice note of the media tools stays ``inline``."""
    headers = {"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"}
    if kind == KIND:
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        headers["Content-Security-Policy"] = "sandbox"
    else:
        headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return headers


def size_text(size: int) -> str:
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


class FileSender:
    """Copies a file of the session sandbox into ``generated_media`` for the user."""

    def __init__(self, session_sandbox: SessionSandbox, *, store: GeneratedMediaStore) -> None:
        self._session_sandbox = session_sandbox
        self._store = store

    async def send(
        self, claims: RunTokenClaims, path: str, name: str | None = None, caption: str = ""
    ) -> MediaResult:
        """Gives the file at ``path`` to the user. Raises ``MediaError`` with a message for
        the model when the file cannot go."""
        sandbox = await self._session_sandbox(claims.org_id, claims.session_id)
        if sandbox is None:
            raise MediaError(NO_SANDBOX)
        full = resolve(path, sandbox.workdir)
        real = await self._real_path(sandbox, full)
        if not allowed(real, sandbox.workdir):
            raise MediaError(OUTSIDE.format(workdir=sandbox.workdir))
        try:
            data = await sandbox.read_file(real, max_bytes=MAX_GENERATED_MEDIA_BYTES)
        except FileNotFoundError:
            raise MediaError(f"There is no file at {full}. Write the file first.") from None
        except SandboxFileTooLargeError:
            limit = size_text(MAX_GENERATED_MEDIA_BYTES)
            raise MediaError(
                f"The file is larger than {limit}. Make it smaller or split it."
            ) from None
        except SandboxError:
            log.warning("a sandbox read for file_send failed run=%s", claims.run_id)
            raise MediaError("sama could not read the file from the sandbox.") from None
        if not data:
            raise MediaError("The file is empty. Write its content first.")
        filename = download_name(name, real)
        mime = file_type(filename)
        if mime == OCTET_STREAM:
            mime = file_type(real)
        starts = MAGIC.get(mime)
        if starts and not data.startswith(starts):
            raise MediaError(
                f"{filename} is not a valid {mime} file: its bytes do not start like one."
                " Make the file again with a tool that writes this format."
            )
        saved = await self._store.save(
            claims,
            kind=KIND,
            mime_type=mime,
            data=data,
            caption=caption,
            name=filename,
            run_limit=MAX_SENT_PER_RUN,
        )
        if saved is None:
            raise MediaError(
                f"This run already has {MAX_SENT_PER_RUN} files for the user. Put more files"
                " in one zip."
            )
        return MediaResult(MediaStatus.OK, sent_text(filename, mime, len(data)))

    async def _real_path(self, sandbox: Sandbox, path: str) -> str:
        """The target of ``path`` with every symbolic link followed, from the sandbox. Raises
        ``MediaError`` when there is no file or the path is a folder."""
        try:
            result = await sandbox.exec(["sh", "-c", REAL_PATH, "sh", path], timeout_s=15)
        except SandboxError:
            log.warning("realpath for file_send failed")
            raise MediaError("sama could not read the file from the sandbox.") from None
        if result.exit_code == FOLDER_EXIT:
            raise MediaError(f"{path} is a folder. Zip it, then send the zip.")
        if result.exit_code != 0 or not result.stdout.strip():
            raise MediaError(f"There is no file at {path}. Write the file first.")
        return posixpath.normpath(result.stdout.strip())


def sent_text(name: str, mime_type: str, size: int) -> str:
    """The tool result: the file went to the user, and how to talk about it."""
    return (
        f"Sent {name} ({mime_type}, {size_text(size)}) to the user. It"
        " shows as a download with your reply (in a chat app, as a file). In your answer, name"
        f" the file ({name}) and say what is in it. Do not give a sandbox path or a link"
        " as the way to get it: the user cannot open those. To send it again later, call"
        " file_send again."
    )
