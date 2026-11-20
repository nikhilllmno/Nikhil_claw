"""The error of the connection service. It has its own module, so that the tool server can
import it without the service."""

from __future__ import annotations

from typing import Final

from sama.vault.redact import redact

MAX_ERROR_CHARS: Final = 300


class ConnectorError(Exception):
    """A connection change or a connector call that cannot happen. The message is safe to
    show to a user and to the agent. ``status_code`` is the HTTP answer of the API."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def short_error(text: str) -> str:
    """A provider error message for a row, a log, or the agent: redacted, on one line, and at
    most ``MAX_ERROR_CHARS`` characters."""
    text = redact(" ".join(text.split()))
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 3] + "..."
