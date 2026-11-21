"""Billing with Stripe (docs/architecture.md, Section 17.5).

An org admin buys a plan with Stripe Checkout, and changes the payment method, sees invoices,
or cancels in the Stripe customer portal. Stripe sends signed webhook events. For each event
of a subscription, sama gets the subscription again from Stripe and copies its state, so the
order of the events does not matter. The plan of the subscription sets ``orgs.plan``, the plan
keys of ``orgs.limits``, and the run budget of the default agent profile.

A subscription has two prices: the base price of the plan, and one metered price for the model
usage over the amount that the plan includes. A loop of the control plane sends the usage of
each period to a Stripe billing meter. The usage comes from the ledger of the model proxy
(``model_calls.cost_usd``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.billing import signature
from sama.billing.stripe import StripeClient, StripeError
from sama.db.models import (
    AgentProfile,
    AuditEntry,
    BillingEvent,
    Membership,
    ModelCall,
    Org,
    OrgBilling,
    OrgInvite,
    User,
)
from sama.orchestrator.limits import ModelQuotas, QueueLimits
from sama.orgs.plans import (
    FREE,
    PAID_PLANS,
    PLANS,
    QUOTA_KEYS,
    TEAM,
    USAGE_LOOKUP_KEY,
    Plan,
    limits_for,
    plan_for_lookup_key,
    plan_of,
)
from sama.orgs.service import OrgError, _admin, _lock_org

log = logging.getLogger(__name__)

METER_EVENT_NAME: Final = "sama_model_usage"
"""The event name of the Stripe billing meter. One unit is one US cent of model usage."""
PORTAL_METADATA_KEY: Final = "sama"
PORTAL_METADATA_VALUE: Final = "billing_portal"
"""The metadata of the customer portal configuration that the setup script makes."""
PAYING: Final = frozenset({"active", "trialing", "past_due"})
"""The Stripe statuses that keep the paid plan. ``past_due``: Stripe tries the card again."""
SUBSCRIPTION_EVENTS: Final = frozenset(
    {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "customer.subscription.paused",
        "customer.subscription.resumed",
    }
)
CHECKOUT_COMPLETED: Final = "checkout.session.completed"
REPORT_INTERVAL_S: Final = 300.0
CHECKOUT_LIFETIME: Final = timedelta(hours=1)
"""How long a Checkout session stays open (Stripe accepts 30 minutes to 24 hours)."""
PENDING_MAX_AGE: Final = timedelta(hours=12)
"""After this time, the usage report does not send a pending meter event again (Stripe keeps
the identifiers for at least 24 hours)."""


def is_sama_portal(configuration: Mapping[str, Any]) -> bool:
    """True for the customer portal configuration that the setup script makes."""
    metadata = configuration.get("metadata") or {}
    return bool(metadata.get(PORTAL_METADATA_KEY) == PORTAL_METADATA_VALUE)


MAX_EVENT_BYTES: Final = 512 * 1024


class BillingError(OrgError):
    """A billing request that sama refuses. ``status_code`` is the HTTP answer."""


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _time(value: Any) -> datetime | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC)
    return None


def _usd(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


def billable_cents(total_usd: Decimal, included_usd: Decimal) -> int:
    """The usage over the included amount, in whole US cents (rounded down)."""
    return max(0, math.floor((total_usd - included_usd) * 100))


def _items(subscription: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = subscription.get("items")
    data = items.get("data") if isinstance(items, Mapping) else None
    return [item for item in data or [] if isinstance(item, dict)]


def _lookup_key(item: Mapping[str, Any]) -> str | None:
    price = item.get("price")
    key = price.get("lookup_key") if isinstance(price, Mapping) else None
    return key if isinstance(key, str) else None


def _item_plan(item: Mapping[str, Any]) -> Plan | None:
    """The paid plan of a subscription item: by the lookup key of its price, else by the
    ``plan`` metadata of the price. A price that the setup script replaced has no lookup key
    any more, but keeps its metadata, so its subscribers keep their plan."""
    plan = plan_for_lookup_key(_lookup_key(item))
    if plan is not None:
        return plan
    price = item.get("price")
    metadata = price.get("metadata") if isinstance(price, Mapping) else None
    name = metadata.get("plan") if isinstance(metadata, Mapping) else None
    return next((p for p in PAID_PLANS if p.name == name), None)


def subscription_plan(subscription: Mapping[str, Any]) -> Plan:
    """The plan that a subscription gives: the plan of its base price while Stripe bills it,
    else ``free``."""
    if subscription.get("status") not in PAYING:
        return FREE
    for item in _items(subscription):
        plan = _item_plan(item)
        if plan is not None:
            return plan
    return FREE


def subscription_period(
    subscription: Mapping[str, Any],
) -> tuple[datetime | None, datetime | None]:
    """The current billing period. In API version ``basil`` it is on the items."""
    for item in _items(subscription):
        start, end = _time(item.get("current_period_start")), _time(item.get("current_period_end"))
        if start is not None and end is not None:
            return start, end
    return (
        _time(subscription.get("current_period_start")),
        _time(subscription.get("current_period_end")),
    )


@dataclass(frozen=True, slots=True)
class WebhookResult:
    applied: bool
    reason: str


async def _audit(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    action: str,
    details: dict[str, Any],
) -> None:
    await connection.execute(
        insert(AuditEntry).values(
            org_id=org_id,
            actor_kind="user" if actor_user_id is not None else "system",
            actor_user_id=actor_user_id,
            action=action,
            target_type="org",
            target_id=str(org_id),
            details=details,
        )
    )


async def apply_plan(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    plan: Plan,
    *,
    actor_user_id: uuid.UUID | None = None,
    via: str,
) -> bool:
    """Sets the plan of the org: ``orgs.plan``, the plan keys of ``orgs.limits``, and the run
    budget of the org-wide agent profiles that still have the budget of the old plan (a budget
    that an admin changed stays). Call it with the ``orgs`` row locked. Returns ``False`` when
    the org already has the plan."""
    row = (await connection.execute(select(Org.plan, Org.limits).where(Org.id == org_id))).one()
    if row.plan == plan.name:
        return False
    old = plan_of(row.plan)
    await connection.execute(
        update(Org)
        .where(Org.id == org_id)
        .values(plan=plan.name, limits=limits_for(plan, row.limits))
    )
    await connection.execute(
        update(AgentProfile)
        .where(
            AgentProfile.org_id == org_id,
            AgentProfile.workspace_id.is_(None),
            AgentProfile.budget == dict(old.run_budget),
        )
        .values(budget=dict(plan.run_budget))
    )
    await _audit(
        connection,
        org_id,
        actor_user_id,
        "org.plan_changed",
        {"from": row.plan, "to": plan.name, "via": via},
    )
    return True


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


class BillingService:
    """The billing rules. Without a Stripe client, billing is off: the read routes work and
    the write routes answer 409."""

    def __init__(
        self,
        engine: AsyncEngine,
        stripe: StripeClient | None = None,
        *,
        webhook_secret: str | None = None,
        web_origins: Sequence[str] = (),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._engine = engine
        self._stripe = stripe if webhook_secret else None
        self._webhook_secret = webhook_secret or ""
        self._origins = tuple(origin.rstrip("/") for origin in web_origins)
        self._clock = clock
        self._portal_configuration: str | None = None

    @property
    def enabled(self) -> bool:
        return self._stripe is not None

    def _client(self) -> StripeClient:
        if self._stripe is None:
            raise BillingError(409, "billing is not set up on this server")
        return self._stripe

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), UTC)

    def _base_url(self, origin: str | None) -> str:
        origin = (origin or "").rstrip("/")
        if origin and origin in self._origins:
            return origin
        if not self._origins:
            raise BillingError(409, "the web app address is not set (SAMA_WEB_ORIGINS)")
        return self._origins[0]

    async def aclose(self) -> None:
        if self._stripe is not None:
            await self._stripe.aclose()

    async def start(self) -> None:
        """Checks the Stripe key once at start. Stripe refuses a key that is not valid (401) or
        that has no access (403): billing is then off, as without a key. A network error keeps
        billing on, because the key can be good.

        With billing on, each free org gets the model quotas of the free plan that it does not
        have yet. Without billing, sama adds no plan quota: an org admin cannot buy more."""
        if self._stripe is None:
            log.info("billing is off: no Stripe key and webhook secret")
            return
        try:
            await self._stripe.prices([USAGE_LOOKUP_KEY])
        except StripeError as error:
            if error.status_code in (401, 403):
                log.error("billing is off: Stripe refused the key (%s)", error.status_code)
                stripe, self._stripe = self._stripe, None
                await stripe.aclose()
                return
            log.warning("the Stripe key check failed, billing stays on: %s", error)
        quotas = {k: FREE.limits[k] for k in QUOTA_KEYS if k in FREE.limits}
        async with self._engine.begin() as connection:
            # A key that is set wins: an operator can give one org a larger quota.
            added = await connection.execute(
                text(
                    "UPDATE orgs SET limits = CAST(:quotas AS jsonb) || limits"
                    " WHERE plan = :plan AND NOT (limits ?& CAST(:keys AS text[]))"
                ),
                {"quotas": json.dumps(quotas), "plan": FREE.name, "keys": list(quotas)},
            )
        if added.rowcount:
            log.info("billing is on: %d free orgs got the free model quotas", added.rowcount)

    # Read.

    def plans(self) -> dict[str, Any]:
        return {
            "billing_enabled": self.enabled,
            "plans": [_plan_out(plan) for plan in (FREE, *PAID_PLANS)],
        }

    async def overview(self, auth_user_id: uuid.UUID, org_id: uuid.UUID) -> dict[str, Any]:
        now = self._now()
        async with self._engine.connect() as connection:
            await _admin(connection, org_id, auth_user_id)
            org = (
                await connection.execute(select(Org.plan, Org.limits).where(Org.id == org_id))
            ).one()
            billing = (
                await connection.execute(select(OrgBilling).where(OrgBilling.org_id == org_id))
            ).one_or_none()
            month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            month_usd = await _spend(connection, org_id, month, now)
            paying = billing is not None and billing.status in PAYING
            start = billing.current_period_start if paying and billing else None
            end = billing.current_period_end if paying and billing else None
            period_usd = (
                await _spend(connection, org_id, start, now, billable=True) if start else month_usd
            )
            members = await _seats(connection, org_id)
        plan = plan_of(org.plan)
        quotas = ModelQuotas.from_json(org.limits)
        queue = QueueLimits.from_json(org.limits)
        return {
            "org_id": str(org_id),
            "billing_enabled": self.enabled,
            "plan": _plan_out(plan) | {"name": org.plan},
            "subscription": None
            if billing is None
            else {
                "status": billing.status,
                "plan": billing.plan,
                "current_period_start": _iso(billing.current_period_start),
                "current_period_end": _iso(billing.current_period_end),
                "cancel_at_period_end": billing.cancel_at_period_end,
                "has_subscription": billing.stripe_subscription_id is not None,
            },
            "usage": {
                "period_start": _iso(start or month),
                "period_end": _iso(end),
                "model_usd": _usd(period_usd),
                "included_usd": _usd(plan.included_usd) if paying else None,
                "billable_usd": _usd(Decimal(billable_cents(period_usd, plan.included_usd)) / 100)
                if paying
                else None,
                "reported_usd": _usd(Decimal(billing.usage_reported_cents) / 100)
                if paying and billing
                else None,
            },
            "quotas": {
                "model_usd_per_month": _usd(quotas.model_usd_per_month),
                "model_usd_this_month": _usd(month_usd),
                "model_usd_per_user_day": _usd(quotas.model_usd_per_user_day),
                "max_members": plan.max_members,
                "members": members,
                "max_running_runs": queue.max_running_runs,
                "user_messages_per_minute": queue.user_messages_per_minute,
                "org_messages_per_minute": queue.org_messages_per_minute,
            },
            "can_buy": self.enabled and org.plan != TEAM.name and not paying,
            "can_change": self.enabled and paying,
            "can_open_portal": self.enabled and billing is not None,
        }

    # Checkout, plan change, and portal.

    async def _prices(self, *lookup_keys: str) -> dict[str, dict[str, Any]]:
        try:
            prices = await self._client().prices(lookup_keys)
        except StripeError as error:
            log.warning("the Stripe prices did not load: %s", error)
            raise BillingError(502, "Stripe did not answer. Try again later.") from None
        missing = [key for key in lookup_keys if key not in prices]
        if missing:
            log.error("the Stripe prices %s do not exist: run sama.billing.setup", missing)
            raise BillingError(503, "the Stripe prices are not set up on this server")
        return prices

    async def checkout(
        self, auth_user_id: uuid.UUID, org_id: uuid.UUID, plan_name: str, origin: str | None
    ) -> dict[str, Any]:
        """Starts Stripe Checkout for a paid plan, and returns the URL of the Checkout page.

        An org has at most one open Checkout session. The org row stays locked during the
        Stripe calls, so two admins cannot start two sessions (and two subscriptions) at the
        same time. A second request for the same plan gets the open session again. A request
        for another plan ends the open session first."""
        stripe = self._client()
        plan = PLANS.get(plan_name)
        if plan is None or plan.lookup_key is None:
            raise BillingError(422, "choose a paid plan")
        base = self._base_url(origin)
        now = self._now()
        prices = await self._prices(plan.lookup_key, USAGE_LOOKUP_KEY)
        async with self._engine.begin() as connection:
            grants = await _admin(connection, org_id, auth_user_id)
            org = await _lock_org(connection, org_id)
            if org.plan == TEAM.name:
                raise BillingError(409, "the team plan of the operator has no billing")
            billing = (
                await connection.execute(select(OrgBilling).where(OrgBilling.org_id == org_id))
            ).one_or_none()
            if billing is not None and billing.status in PAYING:
                raise BillingError(409, "the org already has a plan: change the plan instead")
            try:
                if billing is not None and billing.checkout_session_id is not None:
                    reused = await self._open_checkout(stripe, billing, plan, now)
                    if reused is not None:
                        return {"url": reused}
                if billing is None:
                    email = await connection.scalar(
                        select(User.email).where(User.id == grants.user_id)
                    )
                    customer = await stripe.create_customer(
                        name=org.name,
                        email=email,
                        org_id=str(org_id),
                        idempotency_key=f"sama-customer-{org_id}",
                    )
                    customer_id = str(customer["id"])
                    await connection.execute(
                        insert(OrgBilling).values(org_id=org_id, stripe_customer_id=customer_id)
                    )
                else:
                    customer_id = billing.stripe_customer_id
                back = f"{base}/billing?org={org_id}"
                session = await stripe.create_checkout_session(
                    {
                        "mode": "subscription",
                        "customer": customer_id,
                        "client_reference_id": str(org_id),
                        "line_items": [
                            {"price": prices[plan.lookup_key]["id"], "quantity": 1},
                            {"price": prices[USAGE_LOOKUP_KEY]["id"]},
                        ],
                        "subscription_data": {"metadata": {"org_id": str(org_id)}},
                        "metadata": {"org_id": str(org_id), "plan": plan.name},
                        "success_url": f"{back}&checkout=success",
                        "cancel_url": f"{back}&checkout=cancel",
                        "expires_at": int((now + CHECKOUT_LIFETIME).timestamp()),
                    }
                )
            except StripeError as error:
                log.warning("the Stripe checkout failed org=%s: %s", org_id, error)
                raise BillingError(
                    502, "Stripe did not start the checkout. Try again later."
                ) from None
            await connection.execute(
                update(OrgBilling)
                .where(OrgBilling.org_id == org_id)
                .values(
                    checkout_session_id=str(session["id"]),
                    checkout_url=str(session["url"]),
                    checkout_plan=plan.name,
                    checkout_expires_at=now + CHECKOUT_LIFETIME,
                )
            )
            await _audit(
                connection, org_id, grants.user_id, "billing.checkout_started", {"plan": plan.name}
            )
        return {"url": session["url"]}

    async def _open_checkout(
        self, stripe: StripeClient, billing: Any, plan: Plan, now: datetime
    ) -> str | None:
        """The URL of the open Checkout session of the org when it is for ``plan``. Ends an
        open session for another plan. Raises ``BillingError`` (409) when a session is paid
        and the webhook did not come yet."""
        session = await stripe.request(
            "GET", f"/v1/checkout/sessions/{billing.checkout_session_id}"
        )
        status = session.get("status")
        if status == "complete":
            raise BillingError(
                409, "a payment for this org is in progress: wait a minute and reload the page"
            )
        if status != "open":
            return None
        expires = billing.checkout_expires_at
        if (
            billing.checkout_plan == plan.name
            and expires is not None
            and expires > now + timedelta(minutes=5)
        ):
            return str(billing.checkout_url)
        await stripe.request("POST", f"/v1/checkout/sessions/{billing.checkout_session_id}/expire")
        return None

    async def change_plan(
        self, auth_user_id: uuid.UUID, org_id: uuid.UUID, plan_name: str
    ) -> dict[str, Any]:
        """Moves a paid subscription to another paid plan. Stripe prorates the base price."""
        stripe = self._client()
        plan = PLANS.get(plan_name)
        if plan is None or plan.lookup_key is None:
            raise BillingError(422, "choose a paid plan. To stop paying, cancel in the portal")
        async with self._engine.connect() as connection:
            grants = await _admin(connection, org_id, auth_user_id)
            billing = (
                await connection.execute(select(OrgBilling).where(OrgBilling.org_id == org_id))
            ).one_or_none()
        if (
            billing is None
            or billing.stripe_subscription_id is None
            or billing.status not in PAYING
        ):
            raise BillingError(409, "the org has no paid plan: buy a plan first")
        prices = await self._prices(plan.lookup_key)
        try:
            subscription = await stripe.subscription(billing.stripe_subscription_id)
            base = next((item for item in _items(subscription) if _item_plan(item)), None)
            if base is None:
                raise BillingError(409, "the subscription has no plan price")
            if _item_plan(base) is plan:
                raise BillingError(409, f"the org already has the {plan.title} plan")
            await stripe.update_subscription(
                billing.stripe_subscription_id,
                {
                    "items": [{"id": base["id"], "price": prices[plan.lookup_key]["id"]}],
                    "proration_behavior": "create_prorations",
                },
                idempotency_key=f"sama-plan-{uuid.uuid4()}",
            )
        except StripeError as error:
            log.warning("the Stripe plan change failed org=%s: %s", org_id, error)
            raise BillingError(502, "Stripe did not change the plan. Try again later.") from None
        await self._sync(
            org_id,
            billing.stripe_subscription_id,
            actor_user_id=grants.user_id,
            via="billing_page",
        )
        return await self.overview(auth_user_id, org_id)

    async def portal(
        self, auth_user_id: uuid.UUID, org_id: uuid.UUID, origin: str | None
    ) -> dict[str, Any]:
        """A session of the Stripe customer portal: payment method, invoices, and cancel."""
        stripe = self._client()
        base = self._base_url(origin)
        async with self._engine.connect() as connection:
            await _admin(connection, org_id, auth_user_id)
            customer_id = await connection.scalar(
                select(OrgBilling.stripe_customer_id).where(OrgBilling.org_id == org_id)
            )
        if customer_id is None:
            raise BillingError(409, "the org has no Stripe customer: buy a plan first")
        try:
            configuration = await self._portal_configuration_id(stripe)
            session = await stripe.create_portal_session(
                {
                    "customer": customer_id,
                    "return_url": f"{base}/billing?org={org_id}",
                    "configuration": configuration,
                }
            )
        except StripeError as error:
            log.warning("the Stripe portal failed org=%s: %s", org_id, error)
            raise BillingError(502, "Stripe did not open the portal. Try again later.") from None
        return {"url": session["url"]}

    async def close_org(self, org_id: uuid.UUID) -> bool:
        """For an org delete (M6.8): sends the last usage, then ends the subscription at once
        with a final invoice (``invoice_now``), so Stripe bills the last usage and nothing
        more. The Stripe customer stays, so Stripe can collect the final invoice. Call it
        before the rows of the org go. Returns ``False`` when the org has no Stripe customer.

        Raises ``BillingError``, so the delete stops, when billing is off (503), when the last
        usage report fails or stays pending (409), or when Stripe does not end the
        subscription (502)."""
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(select(OrgBilling).where(OrgBilling.org_id == org_id))
            ).one_or_none()
        if row is None:
            return False
        if self._stripe is None:
            raise BillingError(503, "billing is off: end the Stripe subscription of the org first")
        stripe = self._stripe
        subscription_id = row.stripe_subscription_id
        if subscription_id is not None and row.status in PAYING:
            try:
                for _ in range(3):
                    if not await self._report_org(stripe, org_id):
                        break
            except StripeError as error:
                log.warning("the last usage report failed org=%s: %s", org_id, error)
                raise BillingError(409, "the last usage report failed: try again later") from None
            async with self._engine.connect() as connection:
                pending = await connection.scalar(
                    select(OrgBilling.usage_pending_cents).where(OrgBilling.org_id == org_id)
                )
            if pending is not None:
                raise BillingError(409, "a usage report of the org is pending: try again later")
            try:
                await stripe.request(
                    "DELETE",
                    f"/v1/subscriptions/{subscription_id}",
                    {"invoice_now": True, "prorate": False},
                )
            except StripeError as error:
                if error.status_code != 404:
                    log.warning("the Stripe cancel failed org=%s: %s", org_id, error)
                    raise BillingError(502, "Stripe did not end the subscription") from None
        async with self._engine.begin() as connection:
            await connection.execute(
                update(OrgBilling)
                .where(OrgBilling.org_id == org_id)
                .values(status="canceled", plan=FREE.name, updated_at=func.now())
            )
            await _audit(
                connection,
                org_id,
                None,
                "billing.subscription_ended",
                {"subscription": subscription_id, "reason": "org_delete"},
            )
        return True

    async def _portal_configuration_id(self, stripe: StripeClient) -> str | None:
        if self._portal_configuration is None:
            for configuration in await stripe.portal_configurations():
                if is_sama_portal(configuration):
                    self._portal_configuration = str(configuration["id"])
                    break
        return self._portal_configuration

    # Webhook.

    async def webhook(self, body: bytes, signature_header: str | None) -> WebhookResult:
        """Verifies and applies one Stripe event. Raises ``signature.SignatureError`` for a
        bad signature (400), and ``BillingError`` when Stripe should send the event again."""
        stripe = self._client()
        signature.verify(self._webhook_secret, signature_header, body, clock=self._clock)
        try:
            event = json.loads(body)
        except ValueError:
            raise BillingError(400, "the event is not JSON") from None
        if not isinstance(event, dict) or not isinstance(event.get("id"), str):
            raise BillingError(400, "the event has no id")
        event_id, kind = event["id"], str(event.get("type", ""))
        data = event.get("data")
        obj = data.get("object") if isinstance(data, dict) else None
        if not isinstance(obj, dict):
            return WebhookResult(False, "no object")
        if bool(event.get("livemode")) != stripe.live:
            log.warning("a Stripe event of the other mode came in: %s", event_id)
            return WebhookResult(False, "other mode")
        if kind == CHECKOUT_COMPLETED:
            if obj.get("mode") != "subscription":
                return WebhookResult(False, "not a subscription")
            subscription_id, customer_id = obj.get("subscription"), obj.get("customer")
        elif kind in SUBSCRIPTION_EVENTS:
            subscription_id, customer_id = obj.get("id"), obj.get("customer")
        else:
            return WebhookResult(False, "ignored type")
        if not isinstance(subscription_id, str) or not isinstance(customer_id, str):
            return WebhookResult(False, "no subscription")
        async with self._engine.connect() as connection:
            org_id = await connection.scalar(
                select(OrgBilling.org_id).where(OrgBilling.stripe_customer_id == customer_id)
            )
        if org_id is None:
            log.warning("a Stripe event for an unknown customer: %s", event_id)
            return WebhookResult(False, "unknown customer")
        reason = await self._sync(
            org_id, subscription_id, customer_id=customer_id, event=(event_id, kind), via="webhook"
        )
        return WebhookResult(reason == "applied", reason)

    async def _sync(
        self,
        org_id: uuid.UUID,
        subscription_id: str,
        *,
        customer_id: str | None = None,
        event: tuple[str, str] | None = None,
        actor_user_id: uuid.UUID | None = None,
        via: str,
    ) -> str:
        """Gets the subscription from Stripe and copies its state to ``org_billing`` and the
        plan of the org. The org row is locked before the Stripe call, so a slow handler
        cannot write an older state over a newer one. Returns ``applied``, ``duplicate``
        (the event was applied before), or ``other customer``. Raises ``BillingError`` (503)
        when Stripe does not answer: the transaction rolls back, and Stripe sends the event
        again."""
        stripe = self._client()
        async with self._engine.begin() as connection:
            await _lock_org(connection, org_id)
            if event is not None:
                recorded = await connection.scalar(
                    pg_insert(BillingEvent)
                    .values(org_id=org_id, stripe_event_id=event[0], type=event[1])
                    .on_conflict_do_nothing(index_elements=[BillingEvent.stripe_event_id])
                    .returning(BillingEvent.id)
                )
                if recorded is None:
                    return "duplicate"
            try:
                subscription = await stripe.subscription(subscription_id)
            except StripeError as error:
                log.warning("the Stripe subscription did not load org=%s: %s", org_id, error)
                raise BillingError(503, "Stripe did not answer") from None
            if customer_id is not None and subscription.get("customer") != customer_id:
                return "other customer"
            plan = subscription_plan(subscription)
            start, end = subscription_period(subscription)
            status = str(subscription.get("status"))
            billing = (
                await connection.execute(
                    select(OrgBilling).where(OrgBilling.org_id == org_id).with_for_update()
                )
            ).one()
            current = billing.stripe_subscription_id
            if current not in (None, subscription_id) and billing.status in PAYING:
                if status not in PAYING:
                    # The end of an old subscription does not end the current one.
                    return "applied"
                # Two paid subscriptions: Stripe bills both. sama follows the newer one (by the
                # Stripe ``created`` time), and an operator refunds and cancels the other.
                try:
                    other = await stripe.subscription(current)
                except StripeError as error:
                    log.warning("the Stripe subscription did not load org=%s: %s", org_id, error)
                    raise BillingError(503, "Stripe did not answer") from None
                newer = int(subscription.get("created") or 0) >= int(other.get("created") or 0)
                if other.get("status") in PAYING and not newer:
                    log.error(
                        "org=%s has two paid Stripe subscriptions: kept %s, not the older %s",
                        org_id,
                        current,
                        subscription_id,
                    )
                    return "applied"
                log.error(
                    "org=%s has two paid Stripe subscriptions: %s and %s",
                    org_id,
                    current,
                    subscription_id,
                )
                await _audit(
                    connection,
                    org_id,
                    None,
                    "billing.duplicate_subscription",
                    {"kept": subscription_id, "other": current},
                )
            values: dict[str, Any] = {
                "stripe_subscription_id": subscription_id,
                "status": status,
                "plan": plan.name,
                "current_period_start": start,
                "current_period_end": end,
                "cancel_at_period_end": bool(subscription.get("cancel_at_period_end")),
                "updated_at": func.now(),
            }
            if status in PAYING:
                values |= {
                    "checkout_session_id": None,
                    "checkout_url": None,
                    "checkout_plan": None,
                    "checkout_expires_at": None,
                }
            await connection.execute(
                update(OrgBilling).where(OrgBilling.org_id == org_id).values(**values)
            )
            await apply_plan(connection, org_id, plan, actor_user_id=actor_user_id, via=via)
        return "applied"

    # Usage report.

    async def report_usage(self) -> int:
        """Sends the new billable usage of each paying org to the Stripe meter. Returns the
        number of outbox steps (meter events and period changes)."""
        if self._stripe is None:
            return 0
        async with self._engine.connect() as connection:
            org_ids = (
                await connection.scalars(
                    select(OrgBilling.org_id).where(
                        OrgBilling.status.in_(PAYING),
                        OrgBilling.current_period_start.is_not(None),
                    )
                )
            ).all()
        sent = 0
        for org_id in org_ids:
            # A period change can need two events: the end of the old period, then the new.
            for _ in range(3):
                try:
                    if not await self._report_org(self._stripe, org_id):
                        break
                except StripeError as error:
                    log.warning("the usage report failed org=%s: %s", org_id, error)
                    break
                sent += 1
        return sent

    async def _report_org(self, stripe: StripeClient, org_id: uuid.UUID) -> bool:
        """One step of the usage outbox for one org: a meter event or the start of a new
        period. Returns ``False`` when there is nothing to do."""
        now = self._now()
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    select(OrgBilling)
                    .where(OrgBilling.org_id == org_id, OrgBilling.status.in_(PAYING))
                    .with_for_update(skip_locked=True)
                )
            ).one_or_none()
            if row is None or row.current_period_start is None:
                return False
            period = row.usage_period_start or row.current_period_start
            closed = bool(period != row.current_period_start)
            # Before the renewal webhook comes, usage after the period end waits for the new
            # period: it does not go into the old one.
            end = row.current_period_start if closed else min(now, row.current_period_end or now)
            pending = row.usage_pending_cents
            pending_at = row.usage_pending_at
            stuck = pending_at is not None and now - pending_at > PENDING_MAX_AGE
            if pending is not None and stuck:
                # Stripe drops a second event with the same identifier only for about 24
                # hours. After that, a retry can bill two times: an operator checks the meter
                # events in Stripe and clears the row.
                log.error(
                    "the usage report of org=%s is stuck since %s: check the Stripe meter"
                    " events, then set usage_reported_cents or clear usage_pending_cents",
                    org_id,
                    pending_at,
                )
                return False
            if pending is None:
                total = await _spend(connection, org_id, period, end, billable=True)
                target = billable_cents(total, plan_of(row.plan).included_usd)
                if target > row.usage_reported_cents:
                    pending = target
                    await connection.execute(
                        update(OrgBilling)
                        .where(OrgBilling.org_id == org_id)
                        .values(
                            usage_pending_cents=pending,
                            usage_pending_at=now,
                            usage_period_start=period,
                        )
                    )
                elif closed or row.usage_period_start is None:
                    # Nothing more for the old period: start the new one.
                    await connection.execute(
                        update(OrgBilling)
                        .where(OrgBilling.org_id == org_id)
                        .values(
                            usage_period_start=row.current_period_start,
                            usage_reported_cents=0,
                        )
                    )
                    return closed
                else:
                    return False
            reported = row.usage_reported_cents
            customer_id = row.stripe_customer_id
        # The event is in the period that it bills: an old period gets its last second.
        stamp = end - timedelta(seconds=1) if end < now else now
        await stripe.meter_event(
            event_name=METER_EVENT_NAME,
            customer_id=customer_id,
            value=pending - reported,
            identifier=f"sama-{org_id}-{int(period.timestamp())}-{pending}",
            timestamp=int(stamp.timestamp()),
        )
        async with self._engine.begin() as connection:
            await connection.execute(
                update(OrgBilling)
                .where(OrgBilling.org_id == org_id, OrgBilling.usage_pending_cents == pending)
                .values(
                    usage_reported_cents=pending,
                    usage_pending_cents=None,
                    usage_pending_at=None,
                    usage_reported_at=func.now(),
                )
            )
        return True

    async def serve(self, stop: asyncio.Event, *, interval_s: float = REPORT_INTERVAL_S) -> None:
        """Reports the usage each ``interval_s``. Two control planes can run it: the row lock
        and the meter event identifier stop a second bill."""
        while not stop.is_set():
            try:
                sent = await self.report_usage()
                if sent:
                    log.info("the usage report made %d steps", sent)
            except Exception:
                log.exception("the usage report failed")
            await _sleep(stop, interval_s)


def _plan_out(plan: Plan) -> dict[str, Any]:
    quotas = ModelQuotas.from_json(plan.limits)
    queue = QueueLimits.from_json(plan.limits)
    return {
        "name": plan.name,
        "title": plan.title,
        "price_usd": _usd(plan.price_usd),
        "included_usd": _usd(plan.included_usd),
        "paid": plan.paid,
        "max_members": plan.max_members,
        "model_usd_per_month": _usd(quotas.model_usd_per_month),
        "model_usd_per_user_day": _usd(quotas.model_usd_per_user_day),
        "max_running_runs": queue.max_running_runs,
        "run_budget": dict(plan.run_budget),
    }


async def _spend(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    start: datetime,
    end: datetime,
    *,
    billable: bool = False,
) -> Decimal:
    """The model cost of the org from ``start`` to ``end``. With ``billable``, only the calls
    on the keys of sama: the org pays the provider itself for a call on its own key (M6.7,
    ``key_source = 'org'``). The quotas count both sources, as the model proxy does."""
    query = select(func.coalesce(func.sum(ModelCall.cost_usd), 0)).where(
        ModelCall.org_id == org_id, ModelCall.created_at >= start, ModelCall.created_at < end
    )
    if billable:
        query = query.where(ModelCall.key_source == "sama")
    total = await connection.scalar(query)
    return Decimal(total or 0)


async def _seats(connection: AsyncConnection, org_id: uuid.UUID) -> int:
    members = await connection.scalar(
        select(func.count()).where(Membership.org_id == org_id, Membership.workspace_id.is_(None))
    )
    invites = await connection.scalar(
        select(func.count()).where(
            OrgInvite.org_id == org_id,
            OrgInvite.accepted_at.is_(None),
            OrgInvite.revoked_at.is_(None),
            OrgInvite.expires_at > func.now(),
        )
    )
    return int(members or 0) + int(invites or 0)
