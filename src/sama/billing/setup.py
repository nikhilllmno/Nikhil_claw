"""Makes the Stripe objects of sama billing (docs/architecture.md, Section 17.5).

Run it once for each Stripe account and mode. It is idempotent: a second run finds the
objects and changes only what is different.

    SAMA_STRIPE_SECRET_KEY=sk_test_... uv run python -m sama.billing.setup \\
        --webhook-url https://sama-staging.fly.dev/stripe/webhook \\
        --secret-file /var/tmp/stripe-webhook-secret

It makes:

- one product and one monthly base price for each paid plan (lookup keys ``sama_pro_monthly``
  and ``sama_business_monthly``);
- the billing meter ``sama_model_usage`` and its metered price (lookup key
  ``sama_model_usage``, one US cent for each unit);
- a customer portal configuration (payment method, invoices, cancel at the period end);
- with ``--webhook-url``, the webhook endpoint. Stripe shows the signing secret of an endpoint
  only when it makes the endpoint. The script writes the secret to ``--secret-file`` (mode
  0600) and never prints it. ``--rotate-webhook`` makes the endpoint again, for a new secret.

A live key (``sk_live_``) needs ``--allow-live``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from sama.billing.service import (
    CHECKOUT_COMPLETED,
    METER_EVENT_NAME,
    PORTAL_METADATA_KEY,
    PORTAL_METADATA_VALUE,
    SUBSCRIPTION_EVENTS,
    is_sama_portal,
)
from sama.billing.stripe import API_VERSION, StripeClient, StripeError, is_live_key
from sama.orgs.plans import PAID_PLANS, USAGE_LOOKUP_KEY

USAGE_PRODUCT: Final = "sama_model_usage"
USAGE_UNIT_CENTS: Final = 1
WEBHOOK_EVENTS: Final = (CHECKOUT_COMPLETED, *sorted(SUBSCRIPTION_EVENTS))


def _cents(usd: Decimal) -> int:
    return int(usd * 100)


async def _product(stripe: StripeClient, product_id: str, name: str, report: list[str]) -> None:
    try:
        product = await stripe.request("GET", f"/v1/products/{product_id}")
    except StripeError as error:
        if error.status_code != 404:
            raise
        await stripe.request(
            "POST",
            "/v1/products",
            {"id": product_id, "name": name, "metadata": {"sama": "plan"}},
            idempotency_key=f"sama-setup-product-{product_id}",
        )
        report.append(f"product {product_id}: created")
        return
    if product.get("name") != name or not product.get("active"):
        await stripe.request("POST", f"/v1/products/{product_id}", {"name": name, "active": True})
        report.append(f"product {product_id}: updated")
    else:
        report.append(f"product {product_id}: ok")


async def _price(
    stripe: StripeClient,
    lookup_key: str,
    wanted: dict[str, Any],
    same: Any,
    report: list[str],
) -> str:
    """The price with this lookup key. A new price takes the lookup key from an old price
    that is different (``transfer_lookup_key``): Stripe prices cannot change."""
    current = (await stripe.prices([lookup_key])).get(lookup_key)
    if current is not None and same(current):
        report.append(f"price {lookup_key}: ok ({current['id']})")
        return str(current["id"])
    price = await stripe.request(
        "POST",
        "/v1/prices",
        {**wanted, "lookup_key": lookup_key, "transfer_lookup_key": True},
    )
    report.append(f"price {lookup_key}: {'replaced' if current else 'created'} ({price['id']})")
    return str(price["id"])


async def _meter(stripe: StripeClient, report: list[str]) -> str:
    meters = await stripe.request("GET", "/v1/billing/meters", {"status": "active", "limit": 100})
    for meter in meters.get("data", []):
        if meter.get("event_name") == METER_EVENT_NAME:
            report.append(f"meter {METER_EVENT_NAME}: ok ({meter['id']})")
            return str(meter["id"])
    meter = await stripe.request(
        "POST",
        "/v1/billing/meters",
        {
            "display_name": "Sama model usage (US cents)",
            "event_name": METER_EVENT_NAME,
            "default_aggregation": {"formula": "sum"},
            "customer_mapping": {"type": "by_id", "event_payload_key": "stripe_customer_id"},
            "value_settings": {"event_payload_key": "value"},
        },
        idempotency_key=f"sama-setup-meter-{METER_EVENT_NAME}",
    )
    report.append(f"meter {METER_EVENT_NAME}: created ({meter['id']})")
    return str(meter["id"])


PORTAL_FEATURES: Final[dict[str, Any]] = {
    "customer_update": {"enabled": True, "allowed_updates": ["email", "name", "address"]},
    "invoice_history": {"enabled": True},
    "payment_method_update": {"enabled": True},
    "subscription_cancel": {"enabled": True, "mode": "at_period_end"},
    # A plan change is on the sama Billing page: the subscription has a metered price.
    "subscription_update": {"enabled": False},
}


async def _portal(stripe: StripeClient, return_url: str | None, report: list[str]) -> str:
    params: dict[str, Any] = {
        "features": PORTAL_FEATURES,
        "business_profile": {"headline": "Sama billing"},
        "default_return_url": return_url,
    }
    for configuration in await stripe.portal_configurations():
        if is_sama_portal(configuration):
            await stripe.request(
                "POST", f"/v1/billing_portal/configurations/{configuration['id']}", params
            )
            report.append(f"portal configuration: updated ({configuration['id']})")
            return str(configuration["id"])
    configuration = await stripe.request(
        "POST",
        "/v1/billing_portal/configurations",
        {**params, "metadata": {PORTAL_METADATA_KEY: PORTAL_METADATA_VALUE}},
    )
    report.append(f"portal configuration: created ({configuration['id']})")
    return str(configuration["id"])


async def _webhook(
    stripe: StripeClient, url: str, secret_file: Path | None, rotate: bool, report: list[str]
) -> None:
    endpoints = await stripe.request("GET", "/v1/webhook_endpoints", {"limit": 100})
    found = [e for e in endpoints.get("data", []) if e.get("url") == url]
    if found and not rotate:
        endpoint = found[0]
        await stripe.request(
            "POST",
            f"/v1/webhook_endpoints/{endpoint['id']}",
            {"enabled_events": list(WEBHOOK_EVENTS), "disabled": False},
        )
        report.append(
            f"webhook {url}: ok ({endpoint['id']}). Stripe shows the secret only at creation:"
            " use --rotate-webhook for a new secret"
        )
        return
    if secret_file is None:
        raise SystemExit("--secret-file is necessary to make a webhook endpoint")
    endpoint = await stripe.request(
        "POST",
        "/v1/webhook_endpoints",
        {
            "url": url,
            "enabled_events": list(WEBHOOK_EVENTS),
            "api_version": API_VERSION,
            "description": "sama billing",
        },
    )
    secret = str(endpoint.get("secret") or "")
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(secret)
    os.chmod(secret_file, 0o600)
    for old in found:
        await stripe.request("DELETE", f"/v1/webhook_endpoints/{old['id']}")
    report.append(f"webhook {url}: created ({endpoint['id']}); secret in {secret_file}")


async def setup(
    stripe: StripeClient,
    *,
    webhook_url: str | None = None,
    secret_file: Path | None = None,
    rotate_webhook: bool = False,
    return_url: str | None = None,
) -> list[str]:
    """Makes or checks every object. Returns one report line for each object."""
    report: list[str] = []
    for plan in PAID_PLANS:
        if plan.lookup_key is None or plan.price_usd is None:
            raise ValueError(f"the paid plan {plan.name} has no price")
        product_id = f"sama_{plan.name}"
        await _product(stripe, product_id, f"Sama {plan.title}", report)
        amount = _cents(plan.price_usd)
        await _price(
            stripe,
            plan.lookup_key,
            {
                "product": product_id,
                "currency": "usd",
                "unit_amount": amount,
                "recurring": {"interval": "month"},
                "nickname": f"Sama {plan.title} monthly",
                "metadata": {"plan": plan.name},
            },
            lambda price, amount=amount, product_id=product_id: (
                price.get("unit_amount") == amount
                and price.get("currency") == "usd"
                and price.get("product") == product_id
                and (price.get("recurring") or {}).get("interval") == "month"
            ),
            report,
        )
    await _product(stripe, USAGE_PRODUCT, "Sama model usage", report)
    meter_id = await _meter(stripe, report)
    await _price(
        stripe,
        USAGE_LOOKUP_KEY,
        {
            "product": USAGE_PRODUCT,
            "currency": "usd",
            "unit_amount": USAGE_UNIT_CENTS,
            "recurring": {"interval": "month", "usage_type": "metered", "meter": meter_id},
            "nickname": "Sama model usage over the included amount (per US cent)",
        },
        lambda price: (
            price.get("unit_amount") == USAGE_UNIT_CENTS
            and (price.get("recurring") or {}).get("meter") == meter_id
        ),
        report,
    )
    await _portal(stripe, return_url, report)
    if webhook_url:
        await _webhook(stripe, webhook_url, secret_file, rotate_webhook, report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sama.billing.setup", description=__doc__)
    parser.add_argument("--webhook-url", help="the public URL of POST /stripe/webhook")
    parser.add_argument("--secret-file", type=Path, help="where to write the webhook secret")
    parser.add_argument("--rotate-webhook", action="store_true")
    parser.add_argument("--return-url", help="the default return URL of the portal")
    parser.add_argument("--allow-live", action="store_true", help="accept a live key")
    args = parser.parse_args(argv)
    key = os.environ.get("SAMA_STRIPE_SECRET_KEY") or os.environ.get("STRIPE_SECRET_KEY") or ""
    key = key.strip()
    if not key:
        print("set SAMA_STRIPE_SECRET_KEY", file=sys.stderr)
        return 2
    if is_live_key(key) and not args.allow_live:
        print("this is a live key: add --allow-live to use it", file=sys.stderr)
        return 2

    async def run() -> list[str]:
        stripe = StripeClient(key)
        try:
            return await setup(
                stripe,
                webhook_url=args.webhook_url,
                secret_file=args.secret_file,
                rotate_webhook=args.rotate_webhook,
                return_url=args.return_url,
            )
        finally:
            await stripe.aclose()

    try:
        report = asyncio.run(run())
    except StripeError as error:
        print(f"the setup failed: {error}", file=sys.stderr)
        return 1
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
