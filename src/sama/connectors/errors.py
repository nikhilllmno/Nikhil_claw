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


class ConnectorUnknownError(ConnectorError):
    """A connector call with an unknown result: the provider did not answer, gave a time limit,
    or answered 5xx, 408, or 429. The call can have had its effect. A workflow does not send a
    write that is not idempotent again after this error (Section 12.3)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=502)


def short_error(text: str) -> str:
    """A provider error message for a row, a log, or the agent: redacted, on one line, and at
    most ``MAX_ERROR_CHARS`` characters."""
    # Redact first: a secret with two spaces or a tab in it does not match after the join.
    text = " ".join(redact(text).split())
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 3] + "..."
