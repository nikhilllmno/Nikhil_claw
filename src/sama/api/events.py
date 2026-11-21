"""The shared parts of the message and event routes of the API: the HTTP routes and the
Chrome extension channel (``sama.api.chrome``) send the same events and the same errors."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel

MAX_MESSAGE_CHARS: Final = 32_000


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One event of the event stream of a session. The SSE route and the extension channel
    send the same events (``ping`` is the heartbeat)."""

    event: Literal["runs", "run_event", "run", "end", "ping"]
    data: dict[str, Any]
    event_id: int | None = None


class Sent(BaseModel):
    run_id: uuid.UUID
    queued: bool


class SendError(Exception):
    """A message was not queued. ``status`` is the HTTP status of the answer."""

    def __init__(
        self,
        status: int,
        detail: str,
        *,
        retry_after_s: int | None = None,
        quota: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.retry_after_s = retry_after_s
        self.quota = quota
        """The model quota of a ``402`` (Section 17.5)."""
