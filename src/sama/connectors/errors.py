"""The error of the connection service. It has its own module, so that the tool server can
import it without the service."""

from __future__ import annotations


class ConnectorError(Exception):
    """A connection change or a connector call that cannot happen. The message is safe to
    show to a user and to the agent. ``status_code`` is the HTTP answer of the API."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code
