"""The plans of an org (docs/architecture.md, Sections 17.2 and 17.5).

A plan is a name in ``orgs.plan``. This module maps the name to the caps of the org: the
member cap, the invites per day, the queue limits and the model quotas (``orgs.limits``), and
the run budget of the default agent profile. An unknown name gets the caps of ``free``, so a
bad value never gives more than the smallest plan.

The paid plans (``pro`` and ``business``) have a Stripe price. The Stripe price has a lookup
key, so the setup script and the webhook find the price by its key, not by an id that changes
between test mode and live mode.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final

MANAGED_LIMITS: Final = (
    "max_running_runs",
    "user_messages_per_minute",
    "org_messages_per_minute",
    "model_usd_per_month",
    "model_usd_per_user_day",
)
"""The keys of ``orgs.limits`` that a plan sets. A change of plan writes these keys again, and
keeps the other keys (an operator can add more)."""

QUOTA_KEYS: Final = ("model_usd_per_month", "model_usd_per_user_day")
"""The model quotas of a plan. They apply only while billing is on (Section 17.5): without
billing, an org admin has no way to buy more."""

USAGE_LOOKUP_KEY: Final = "sama_model_usage"
"""The lookup key of the metered Stripe price for model usage over the included amount. One
unit is one US cent."""


@dataclass(frozen=True, slots=True)
class Plan:
    name: str
    max_members: int | None
    """The members of the org plus its open invites. ``None``: no cap."""
    invites_per_day: int | None
    """The invites that the admins of the org can make in 24 hours. ``None``: no cap."""
    limits: Mapping[str, Any] = field(default_factory=dict)
    """The queue limits (``QueueLimits``) and model quotas (``OrgLimits``) for ``orgs.limits``."""
    run_budget: Mapping[str, Any] = field(default_factory=dict)
    """The run budget for ``agent_profiles.budget`` of the default profile (``Budget``)."""
    title: str = ""
    """The name of the plan in the web app and on the Stripe product."""
    price_usd: Decimal | None = None
    """The monthly base price. ``None``: the plan is not for sale."""
    included_usd: Decimal = Decimal(0)
    """The model usage that the base price includes each billing period."""
    lookup_key: str | None = None
    """The lookup key of the Stripe base price. ``None``: the plan is not for sale."""

    @property
    def paid(self) -> bool:
        return self.lookup_key is not None


TEAM: Final = Plan("team", max_members=None, invites_per_day=None, title="Team")
"""The plan of an org of ``bootstrap``: the team of the operator. No caps and no billing."""

FREE: Final = Plan(
    "free",
    max_members=5,
    invites_per_day=20,
    limits=MappingProxyType(
        {
            "max_running_runs": 2,
            "user_messages_per_minute": 10,
            "org_messages_per_minute": 30,
            "model_usd_per_month": "5.00",
            "model_usd_per_user_day": "2.00",
        }
    ),
    run_budget=MappingProxyType(
        {"max_turns": 40, "max_cost_usd": "1.00", "max_wall_time_s": 900.0}
    ),
    title="Free",
    price_usd=Decimal(0),
)
"""The plan of an org from self-serve sign-up. The caps keep the cost of an unpaid org small."""

PRO: Final = Plan(
    "pro",
    max_members=10,
    invites_per_day=50,
    limits=MappingProxyType(
        {
            "max_running_runs": 5,
            "user_messages_per_minute": 20,
            "org_messages_per_minute": 120,
            "model_usd_per_month": "200.00",
            "model_usd_per_user_day": "50.00",
        }
    ),
    run_budget=MappingProxyType(
        {"max_turns": 100, "max_cost_usd": "5.00", "max_wall_time_s": 1800.0}
    ),
    title="Pro",
    price_usd=Decimal(20),
    included_usd=Decimal(10),
    lookup_key="sama_pro_monthly",
)

BUSINESS: Final = Plan(
    "business",
    max_members=50,
    invites_per_day=200,
    limits=MappingProxyType(
        {
            "max_running_runs": 10,
            "user_messages_per_minute": 30,
            "org_messages_per_minute": 300,
            "model_usd_per_month": "1000.00",
            "model_usd_per_user_day": "200.00",
        }
    ),
    run_budget=MappingProxyType(
        {"max_turns": 200, "max_cost_usd": "20.00", "max_wall_time_s": 3600.0}
    ),
    title="Business",
    price_usd=Decimal(99),
    included_usd=Decimal(60),
    lookup_key="sama_business_monthly",
)

SIGNUP_PLAN: Final = FREE
"""The plan of a new org from self-serve sign-up."""

PLANS: Final[Mapping[str, Plan]] = MappingProxyType(
    {plan.name: plan for plan in (TEAM, FREE, PRO, BUSINESS)}
)

PAID_PLANS: Final = (PRO, BUSINESS)
"""The plans that an org admin can buy, in the order of the web app."""


def plan_of(name: str | None) -> Plan:
    """The plan with this name. An unknown name gets ``FREE``."""
    return PLANS.get(name or "", FREE)


def plan_for_lookup_key(lookup_key: str | None) -> Plan | None:
    """The paid plan with this Stripe lookup key, or ``None``."""
    return next((plan for plan in PAID_PLANS if plan.lookup_key == lookup_key), None)


def limits_for(
    plan: Plan, current: Mapping[str, Any] | None, *, quotas: bool = True
) -> dict[str, Any]:
    """``orgs.limits`` for ``plan``: the managed keys of the plan, and the other keys of
    ``current`` as they are. A plan without a managed key removes the key, so the default of
    the code applies (``team`` has no caps).

    With ``quotas=False`` (billing is off), the model quotas (``QUOTA_KEYS``) of ``current``
    stay as they are and the plan adds none: an org that cannot buy a plan gets no plan quota.
    """
    managed = MANAGED_LIMITS if quotas else tuple(k for k in MANAGED_LIMITS if k not in QUOTA_KEYS)
    kept = {k: v for k, v in (current or {}).items() if k not in managed}
    return kept | {k: v for k, v in plan.limits.items() if k in managed}
