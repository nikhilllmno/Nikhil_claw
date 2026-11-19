"""Per-org limits of the run queue (docs/architecture.md, Section 4.3).

The limits are in ``orgs.limits`` (JSON). A missing or invalid value uses the default, so a bad
value never stops the queue.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

DEFAULT_MAX_RUNNING_RUNS: Final = 5
DEFAULT_USER_MESSAGES_PER_MINUTE: Final = 20
DEFAULT_ORG_MESSAGES_PER_MINUTE: Final = 120


def _positive(value: Any, default: int) -> int:
    # bool is a subclass of int, and true is not a count.
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return default


@dataclass(frozen=True, slots=True)
class QueueLimits:
    max_running_runs: int = DEFAULT_MAX_RUNNING_RUNS
    """At most this many runs of the org are ``running`` at the same time."""
    user_messages_per_minute: int = DEFAULT_USER_MESSAGES_PER_MINUTE
    """At most this many messages of one user in 60 seconds."""
    org_messages_per_minute: int = DEFAULT_ORG_MESSAGES_PER_MINUTE
    """At most this many messages of the org in 60 seconds, also from jobs."""

    @classmethod
    def from_json(cls, limits: Mapping[str, Any] | None) -> QueueLimits:
        limits = limits if isinstance(limits, Mapping) else {}
        return cls(
            max_running_runs=_positive(limits.get("max_running_runs"), DEFAULT_MAX_RUNNING_RUNS),
            user_messages_per_minute=_positive(
                limits.get("user_messages_per_minute"), DEFAULT_USER_MESSAGES_PER_MINUTE
            ),
            org_messages_per_minute=_positive(
                limits.get("org_messages_per_minute"), DEFAULT_ORG_MESSAGES_PER_MINUTE
            ),
        )
