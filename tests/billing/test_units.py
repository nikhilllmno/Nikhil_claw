"""Unit tests of billing without a database (docs/architecture.md, Section 17.5)."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from sama.billing import signature
from sama.billing.service import billable_cents, subscription_period, subscription_plan
from sama.billing.stripe import API_VERSION, StripeClient, StripeError, encode, is_live_key
from sama.orchestrator.limits import ModelQuotas
from sama.orgs.plans import (
    BUSINESS,
    FREE,
    MANAGED_LIMITS,
    PAID_PLANS,
    PLANS,
    PRO,
    TEAM,
    limits_for,
    plan_for_lookup_key,
    plan_of,
)

SECRET = "whsec_test_secret"


# Plans.


def test_every_paid_plan_has_a_price_and_every_managed_limit() -> None:
    assert PAID_PLANS == (PRO, BUSINESS)
    for plan in PAID_PLANS:
        assert plan.paid
        assert plan.price_usd is not None
        assert plan.price_usd > 0
        assert plan.lookup_key is not None
        assert plan_for_lookup_key(plan.lookup_key) is plan
        assert set(plan.limits) == set(MANAGED_LIMITS)
    assert set(FREE.limits) == set(MANAGED_LIMITS)
    assert not FREE.paid
    assert not TEAM.paid
    assert plan_for_lookup_key("sama_model_usage") is None
    assert plan_for_lookup_key(None) is None


def test_a_bigger_plan_never_has_a_smaller_cap() -> None:
    order = [FREE, PRO, BUSINESS]
    for small, big in itertools.pairwise(order):
        assert big.max_members is not None
        assert small.max_members is not None
        assert big.max_members >= small.max_members
        for key in MANAGED_LIMITS:
            assert Decimal(str(big.limits[key])) >= Decimal(str(small.limits[key])), key


def test_an_unknown_plan_gets_the_free_caps() -> None:
    assert plan_of("gold") is FREE
    assert plan_of("team") is TEAM
    assert set(PLANS) == {"team", "free", "pro", "business"}


def test_limits_for_keeps_the_keys_that_a_plan_does_not_manage() -> None:
    current = {"max_running_runs": 99, "model_usd_per_month": "1", "custom": 7}
    limits = limits_for(PRO, current)
    assert limits["custom"] == 7
    assert {key: limits[key] for key in MANAGED_LIMITS} == dict(PRO.limits)
    # The team plan has no caps: it removes the managed keys.
    assert limits_for(TEAM, current) == {"custom": 7}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ModelQuotas()),
        ({"model_usd_per_month": "5.00"}, ModelQuotas(model_usd_per_month=Decimal("5.00"))),
        ({"model_usd_per_user_day": 2}, ModelQuotas(model_usd_per_user_day=Decimal(2))),
        ({"model_usd_per_month": True, "model_usd_per_user_day": "-1"}, ModelQuotas()),
        ({"model_usd_per_month": "NaN", "model_usd_per_user_day": "bad"}, ModelQuotas()),
    ],
)
def test_model_quotas_come_from_the_org_json(raw: Any, expected: ModelQuotas) -> None:
    assert ModelQuotas.from_json(raw) == expected


# Usage and subscriptions.


@pytest.mark.parametrize(
    ("total", "included", "cents"),
    [("0", "10", 0), ("10", "10", 0), ("10.019", "10", 1), ("12.5", "10", 250), ("3", "0", 300)],
)
def test_billable_cents_is_the_usage_over_the_included_amount(
    total: str, included: str, cents: int
) -> None:
    assert billable_cents(Decimal(total), Decimal(included)) == cents


def subscription(status: str = "active", key: str | None = "sama_pro_monthly") -> dict[str, Any]:
    items: list[dict[str, Any]] = [{"id": "si_usage", "price": {"lookup_key": "sama_model_usage"}}]
    if key is not None:
        items.append(
            {
                "id": "si_base",
                "price": {"lookup_key": key},
                "current_period_start": 1_700_000_000,
                "current_period_end": 1_702_592_000,
            }
        )
    return {"id": "sub_1", "status": status, "items": {"data": items}}


def test_the_plan_of_a_subscription() -> None:
    assert subscription_plan(subscription()) is PRO
    assert subscription_plan(subscription(key="sama_business_monthly")) is BUSINESS
    assert subscription_plan(subscription(status="past_due")) is PRO
    for status in ("canceled", "unpaid", "incomplete", "incomplete_expired", "paused"):
        assert subscription_plan(subscription(status=status)) is FREE
    assert subscription_plan(subscription(key=None)) is FREE


def test_the_period_of_a_subscription_is_on_the_items() -> None:
    start, end = subscription_period(subscription())
    assert start == datetime.fromtimestamp(1_700_000_000, UTC)
    assert end == datetime.fromtimestamp(1_702_592_000, UTC)
    old = {"current_period_start": 1, "current_period_end": 2, "items": {"data": []}}
    assert subscription_period(old) == (
        datetime.fromtimestamp(1, UTC),
        datetime.fromtimestamp(2, UTC),
    )
    assert subscription_period({}) == (None, None)


# The Stripe client.


def test_encode_flattens_nested_parameters() -> None:
    fields = encode(
        {
            "mode": "subscription",
            "line_items": [{"price": "price_1", "quantity": 1}, {"price": "price_2"}],
            "subscription_data": {"metadata": {"org_id": "o"}},
            "allow": True,
            "skip": None,
        }
    )
    assert fields == [
        ("mode", "subscription"),
        ("line_items[0][price]", "price_1"),
        ("line_items[0][quantity]", "1"),
        ("line_items[1][price]", "price_2"),
        ("subscription_data[metadata][org_id]", "o"),
        ("allow", "true"),
    ]


def test_a_live_key_is_known() -> None:
    assert is_live_key("sk_live_abc")
    assert is_live_key("rk_live_abc")
    assert not is_live_key("sk_test_abc")


async def test_the_client_sends_the_key_the_version_and_the_idempotency_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "cus_1"})

    client = StripeClient(
        "sk_test_x", http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    await client.create_customer(name="Acme", email=None, org_id="o", idempotency_key="k")
    await client.prices(["a", "b"])
    request, query = seen
    assert request.headers["authorization"] == "Bearer sk_test_x"
    assert request.headers["stripe-version"] == API_VERSION
    assert request.headers["idempotency-key"] == "k"
    assert request.content == b"name=Acme&metadata%5Borg_id%5D=o"
    assert query.method == "GET"
    assert query.url.params.get_list("lookup_keys[0]") == ["a"]
    assert query.url.params.get_list("lookup_keys[1]") == ["b"]
    assert not client.live
    await client.aclose()


async def test_a_stripe_error_has_no_secret() -> None:
    answers = iter(
        [
            httpx.Response(402, json={"error": {"message": "card declined", "code": "declined"}}),
            httpx.Response(500, text="oops"),
            httpx.Response(401, json={"error": {"message": "Invalid API Key: sk_test_secret"}}),
        ]
    )
    client = StripeClient(
        "sk_test_secret",
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(answers))),
    )
    with pytest.raises(StripeError) as declined:
        await client.subscription("sub_1")
    assert declined.value.status_code == 402
    assert "card declined" in str(declined.value)
    with pytest.raises(StripeError) as failed:
        await client.subscription("sub_1")
    assert "HTTP 500" in str(failed.value)
    with pytest.raises(StripeError) as echoed:
        await client.subscription("sub_1")
    assert "[redacted]" in str(echoed.value)

    def broken(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    offline = StripeClient(
        "sk_test_secret", http=httpx.AsyncClient(transport=httpx.MockTransport(broken))
    )
    with pytest.raises(StripeError) as error:
        await offline.subscription("sub_1")
    assert error.value.status_code is None
    for problem in (declined.value, failed.value, echoed.value, error.value):
        assert "sk_test_secret" not in str(problem)


# The webhook signature.


def test_a_signed_delivery_is_valid() -> None:
    body = json.dumps({"id": "evt_1"}).encode()
    header = signature.sign(SECRET, 1000, body)
    assert signature.verify(SECRET, header, body, clock=lambda: 1000.0) == 1000
    # Stripe sends more than one v1 value while a secret rolls.
    rolled = f"{header},v1={'0' * 64}"
    assert signature.verify(SECRET, rolled, body, clock=lambda: 1100.0) == 1000


@pytest.mark.parametrize(
    ("header", "now", "message"),
    [
        (None, 1000.0, "missing"),
        ("t=1000", 1000.0, "no v1"),
        ("v1=abc", 1000.0, "timestamp"),
        ("t=1000,t=1001,v1=abc", 1000.0, "timestamp"),
        ("t=abc,v1=abc", 1000.0, "timestamp"),
        ("t=1000," + ",".join(["v1=a"] * 11), 1000.0, "no v1"),
        ("t=1000,v1=" + "a" * 2000, 1000.0, "missing"),
        ("t=1000,v1=" + "0" * 64, 1000.0, "does not match"),
    ],
)
def test_a_bad_signature_is_refused(header: str | None, now: float, message: str) -> None:
    with pytest.raises(signature.SignatureError, match=message):
        signature.verify(SECRET, header, b"{}", clock=lambda: now)


def test_an_old_or_changed_delivery_is_refused() -> None:
    body = b'{"id":"evt_1"}'
    header = signature.sign(SECRET, 1000, body)
    with pytest.raises(signature.SignatureError, match="too old"):
        signature.verify(SECRET, header, body, clock=lambda: 1301.0)
    with pytest.raises(signature.SignatureError, match="does not match"):
        signature.verify(SECRET, header, body + b" ", clock=lambda: 1000.0)
    with pytest.raises(signature.SignatureError, match="does not match"):
        signature.verify("whsec_other", header, body, clock=lambda: 1000.0)
    with pytest.raises(signature.SignatureError, match="not set"):
        signature.verify("", header, body, clock=lambda: 1000.0)
