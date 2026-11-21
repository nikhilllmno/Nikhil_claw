"""The plans of an org (docs/architecture.md, Section 17.2).

A plan is a name in ``orgs.plan``. This module maps the name to the caps of the org: the
member cap, the invites per day, the queue limits (``orgs.limits``), and the run budget of the
default agent profile. M6.6 (billing) adds paid plans here. An unknown name gets the caps of
``free``, so a bad value never gives more than the smallest plan.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final


@dataclass(frozen=True, slots=True)
class Plan:
    name: str
    max_members: int | None
    """The members of the org plus its open invites. ``None``: no cap."""
    invites_per_day: int | None
    """The invites that the admins of the org can make in 24 hours. ``None``: no cap."""
    limits: Mapping[str, Any] = field(default_factory=dict)
    """The queue limits for ``orgs.limits`` of a new org (``QueueLimits``)."""
    run_budget: Mapping[str, Any] = field(default_factory=dict)
    """The run budget for ``agent_profiles.budget`` of the default profile (``Budget``)."""


TEAM: Final = Plan("team", max_members=None, invites_per_day=None)
"""The plan of an org of ``bootstrap``: the team of the operator. No caps."""

FREE: Final = Plan(
    "free",
    max_members=5,
    invites_per_day=20,
    limits=MappingProxyType(
        {"max_running_runs": 2, "user_messages_per_minute": 10, "org_messages_per_minute": 30}
    ),
    run_budget=MappingProxyType(
        {"max_turns": 40, "max_cost_usd": "1.00", "max_wall_time_s": 900.0}
    ),
)
"""The plan of an org from self-serve sign-up. The caps keep the cost of an unpaid org small
until M6.6 adds quotas and billing."""

SIGNUP_PLAN: Final = FREE
"""The plan of a new org from self-serve sign-up."""

PLANS: Final[Mapping[str, Plan]] = MappingProxyType({plan.name: plan for plan in (TEAM, FREE)})


def plan_of(name: str | None) -> Plan:
    """The plan with this name. An unknown name gets ``FREE``."""
    return PLANS.get(name or "", FREE)
