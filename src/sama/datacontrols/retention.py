"""Retention per plan (docs/architecture.md, Section 17.4).

Each org has a retention setting, ``orgs.retention_days``. Null means the default of the plan.
The plan can also set an upper limit. The plans of M6.6 fill ``PLAN_RETENTION``. Until then,
every plan keeps everything, and only an org setting turns the sweep on.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from sama.db.models import MAX_RETENTION_DAYS


@dataclass(frozen=True, slots=True)
class PlanRetention:
    """The retention rule of one plan."""

    default_days: int | None = None
    """The retention of an org without its own setting. None keeps everything."""
    max_days: int | None = None
    """The longest retention that an org can set. None has no limit."""


KEEP_EVERYTHING: Final = PlanRetention()
PLAN_RETENTION: Final[Mapping[str, PlanRetention]] = {}
"""The rule of each plan. The plans of M6.6 add their rules here. A plan that is not in the
map keeps everything."""


class RetentionError(ValueError):
    """The retention setting is not allowed. The message is safe to show."""


def plan_retention(plan: str) -> PlanRetention:
    """The retention rule of ``plan``: the seam for the plans of M6.6."""
    return PLAN_RETENTION.get(plan, KEEP_EVERYTHING)


def effective_days(plan: str, org_days: int | None) -> int | None:
    """The retention in days that the sweep uses, or None to keep everything."""
    rule = plan_retention(plan)
    days = org_days if org_days is not None else rule.default_days
    if rule.max_days is None:
        return days
    return rule.max_days if days is None else min(days, rule.max_days)


def check_days(plan: str, days: int | None) -> None:
    """Raises ``RetentionError`` when the org cannot set ``days``."""
    if days is None:
        return
    if not 1 <= days <= MAX_RETENTION_DAYS:
        raise RetentionError(f"the retention must be from 1 to {MAX_RETENTION_DAYS} days")
    rule = plan_retention(plan)
    if rule.max_days is not None and days > rule.max_days:
        raise RetentionError(f"the plan keeps data for at most {rule.max_days} days")
