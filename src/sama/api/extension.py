"""The Chrome extension card of the web app (docs/architecture.md, Section 14.2.4, M8.3).

``GET /me/chrome`` tells the web app if the sama Chrome extension of the user has an open
connection to this process. ``GET /extension/download`` gives the build of the extension as a
zip for "Load unpacked". The image has one build (``SAMA_EXTENSION_ZIP``) with a placeholder API
address (``PLACEHOLDER_API_URL``), so one image serves staging and production: the download puts
the public address of this API in its place.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Final

PLACEHOLDER_API_URL: Final = "https://sama-api.invalid"
"""The API address of the build in the image (``extension/build.mjs`` with ``SAMA_API_URL``)."""
REWRITTEN: Final = (".js", ".json", ".html")
"""The files of the build that can hold the address."""
MAX_ZIP_BYTES: Final = 20_000_000
MAX_UNZIPPED_BYTES: Final = 50_000_000
DOWNLOAD_NAME: Final = "sama-extension.zip"


class ExtensionError(Exception):
    """The build is not in the image or is not valid."""


def extension_ready(path: str | None) -> bool:
    """True when the image has the build of the extension."""
    return bool(path) and Path(str(path)).is_file()


def build_for(path: str | None, api_url: str | None) -> bytes:
    """The zip of the extension with ``api_url`` as its API address. Raises ``ExtensionError``
    when the build is missing, too large, or ``api_url`` is not ``https``."""
    if not path or not Path(path).is_file():
        raise ExtensionError("the extension build is not in this image")
    if not api_url or not api_url.startswith("https://"):
        raise ExtensionError("the public address of the API is not set")
    data = Path(path).read_bytes()
    if len(data) > MAX_ZIP_BYTES:
        raise ExtensionError("the extension build is too large")
    origin = api_url.rstrip("/")
    try:
        return _rewrite(data, origin)
    except (zipfile.BadZipFile, ValueError, KeyError) as error:
        raise ExtensionError("the extension build is not valid") from error


def _rewrite(data: bytes, origin: str) -> bytes:
    out = io.BytesIO()
    total = 0
    with (
        zipfile.ZipFile(io.BytesIO(data)) as source,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            name = info.filename
            parts = Path(name.replace("\\", "/")).parts
            if name.startswith(("/", "\\")) or ".." in parts or ":" in name:
                raise ExtensionError("the extension build is not valid")
            total += info.file_size
            if total > MAX_UNZIPPED_BYTES:
                raise ExtensionError("the extension build is too large")
            content = source.read(info)
            if name.endswith(REWRITTEN):
                content = content.replace(PLACEHOLDER_API_URL.encode(), origin.encode())
            if name == "manifest.json":
                content = _unique_hosts(content)
            target.writestr(info, content)
    return out.getvalue()


def _unique_hosts(content: bytes) -> bytes:
    """The manifest with each host permission once (the address can be in the list already)."""
    manifest = json.loads(content)
    hosts = manifest.get("host_permissions")
    if isinstance(hosts, list):
        manifest["host_permissions"] = list(dict.fromkeys(hosts))
    return (json.dumps(manifest, indent=2) + "\n").encode()
