"""Per-org limits of the run queue (docs/architecture.md, Section 4.3).

The limits are in ``orgs.limits`` (JSON). A missing or invalid value uses the default, so a bad
value never stops the queue.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any


def _positive(value: Any) -> bool:
    # bool is a subclass of int, and true is not a count.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


@dataclass(frozen=True, slots=True)
class QueueLimits:
    """The field names are the keys in ``orgs.limits``."""

    max_running_runs: int = 5
    """At most this many runs of the org are ``running`` at the same time."""
    user_messages_per_minute: int = 20
    """At most this many messages of one user in 60 seconds."""
    org_messages_per_minute: int = 120
    """At most this many messages of the org in 60 seconds, also from jobs."""

    @classmethod
    def from_json(cls, limits: Mapping[str, Any] | None) -> QueueLimits:
        limits = limits if isinstance(limits, Mapping) else {}
        # An invalid value is left out, so the field keeps its default.
        return cls(**{f.name: limits[f.name] for f in fields(cls) if _positive(limits.get(f.name))})
