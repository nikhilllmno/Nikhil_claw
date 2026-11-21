"""The origins of the web app that may call the API from a browser (``SAMA_WEB_ORIGINS``), and
the ids of the Chrome extensions that may open the extension channel (``SAMA_EXTENSION_IDS``).

An origin is ``http://host`` or ``https://host``, with an optional port. It has no path, no
query, and no wildcard. The check at start-up refuses anything else, so a typo cannot open the
API to every site.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

_ORIGIN: Final = re.compile(
    r"^https?://(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
    r"(?::[0-9]{1,5})?$"
)


def check_origins(origins: Iterable[str]) -> tuple[str, ...]:
    """The origins in lower case, without duplicates.

    Raises ``ValueError`` for a value that is not an exact origin (for example ``*``, a URL with
    a path, or a trailing slash).
    """
    checked: dict[str, None] = {}
    for origin in origins:
        value = origin.strip().lower()
        if not _ORIGIN.fullmatch(value):
            raise ValueError(
                f"{origin!r} is not an origin: use http(s)://host[:port] with no path and no *"
            )
        checked[value] = None
    return tuple(checked)


def parse_origins(text: str) -> tuple[str, ...]:
    """The origins in a comma-separated list. An empty value, or empty items, are skipped."""
    return check_origins(part for part in text.split(",") if part.strip())


_EXTENSION_ID: Final = re.compile(r"^[a-p]{32}$")


def check_extension_ids(ids: Iterable[str]) -> tuple[str, ...]:
    """The Chrome extension ids, without duplicates. An id is 32 letters from ``a`` to ``p``.
    Raises ``ValueError`` for anything else."""
    checked: dict[str, None] = {}
    for extension_id in ids:
        value = extension_id.strip()
        if not _EXTENSION_ID.fullmatch(value):
            raise ValueError(f"{extension_id!r} is not a Chrome extension id: use 32 letters a-p")
        checked[value] = None
    return tuple(checked)


def parse_extension_ids(text: str) -> tuple[str, ...]:
    """The extension ids in a comma-separated list. Empty items are skipped."""
    return check_extension_ids(part for part in text.split(",") if part.strip())
