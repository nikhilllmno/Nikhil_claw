"""A fake Stripe API for the billing tests. It keeps the objects in memory and answers the
calls of ``sama.billing.stripe.StripeClient`` and of the setup script."""

from __future__ import annotations

import itertools
import json
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl

import httpx

from sama.billing import signature
from sama.billing.stripe import StripeClient

WEBHOOK_SECRET = "whsec_fake_secret_for_tests"
PERIOD_START = 1_767_225_600  # 2026-01-01T00:00:00Z
PERIOD_END = 1_769_904_000  # 2026-02-01T00:00:00Z


def _nest(fields: list[tuple[str, str]]) -> dict[str, Any]:
    """The nested parameters of form fields: ``a[b][0]=1`` gives ``{"a": {"b": {"0": "1"}}}``."""
    out: dict[str, Any] = {}
    for key, value in fields:
        parts = re.findall(r"[^\[\]]+", key)
        node = out
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return out


def _list(value: Any) -> list[Any]:
    """A nested ``{"0": .., "1": ..}`` as a list."""
    if isinstance(value, dict):
        return [value[key] for key in sorted(value, key=int)]
    return list(value or [])


class FakeStripe:
    def __init__(self) -> None:
        self.ids = itertools.count(1)
        self.requests: list[tuple[str, str, dict[str, Any], dict[str, str]]] = []
        self.customers: dict[str, dict[str, Any]] = {}
        self.products: dict[str, dict[str, Any]] = {}
        self.prices: dict[str, dict[str, Any]] = {}
        self.meters: dict[str, dict[str, Any]] = {}
        self.subscriptions: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.portal_configurations: dict[str, dict[str, Any]] = {}
        self.endpoints: dict[str, dict[str, Any]] = {}
        self.meter_events: list[dict[str, Any]] = []
        self.idempotent: dict[str, dict[str, Any]] = {}
        self.clock: Callable[[], float] = time.time
        """The time of the signature of a webhook delivery."""
        self.fail: dict[str, int] = {}
        """Path prefix -> HTTP status of the next call."""

    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{next(self.ids)}"

    def client(self, key: str = "sk_test_fake") -> StripeClient:
        transport = httpx.MockTransport(self.handle)
        return StripeClient(key, http=httpx.AsyncClient(transport=transport))

    # Helpers for tests.

    def add_price(self, lookup_key: str, **extra: Any) -> dict[str, Any]:
        price = {
            "id": self.new_id("price"),
            "object": "price",
            "lookup_key": lookup_key,
            "active": True,
            "currency": "usd",
            **extra,
        }
        self.prices[price["id"]] = price
        return price

    def standard_prices(self) -> None:
        self.add_price("sama_pro_monthly", unit_amount=2000, product="sama_pro")
        self.add_price("sama_business_monthly", unit_amount=9900, product="sama_business")
        self.add_price("sama_model_usage", unit_amount=1, product="sama_model_usage")

    def price_by_key(self, lookup_key: str) -> dict[str, Any]:
        return next(p for p in self.prices.values() if p.get("lookup_key") == lookup_key)

    def subscribe(
        self, customer_id: str, lookup_key: str = "sama_pro_monthly", status: str = "active"
    ) -> dict[str, Any]:
        """A subscription as Checkout makes it."""
        items = [
            {
                "id": self.new_id("si"),
                "price": self.price_by_key(lookup_key),
                "quantity": 1,
                "current_period_start": PERIOD_START,
                "current_period_end": PERIOD_END,
            },
            {
                "id": self.new_id("si"),
                "price": self.price_by_key("sama_model_usage"),
                "current_period_start": PERIOD_START,
                "current_period_end": PERIOD_END,
            },
        ]
        subscription: dict[str, Any] = {
            "id": self.new_id("sub"),
            "object": "subscription",
            "customer": customer_id,
            "status": status,
            "created": PERIOD_START + next(self.ids),
            "cancel_at_period_end": False,
            "items": {"object": "list", "data": items},
        }
        self.subscriptions[subscription["id"]] = subscription
        return subscription

    def event(
        self, kind: str, obj: dict[str, Any], *, event_id: str | None = None, live: bool = False
    ) -> tuple[bytes, str]:
        """The body and the ``Stripe-Signature`` header of a webhook delivery."""
        body = json.dumps(
            {
                "id": event_id or self.new_id("evt"),
                "object": "event",
                "type": kind,
                "livemode": live,
                "data": {"object": obj},
            }
        ).encode()
        return body, signature.sign(WEBHOOK_SECRET, int(self.clock()), body)

    def calls(self, method: str, path: str) -> list[dict[str, Any]]:
        return [p for m, u, p, _ in self.requests if m == method and u == path]

    # The API.

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method in ("GET", "DELETE"):
            fields = list(request.url.params.multi_items())
        else:
            fields = parse_qsl(request.content.decode(), keep_blank_values=True)
        params = _nest(fields)
        headers = dict(request.headers)
        self.requests.append((request.method, path, params, headers))
        assert headers["authorization"].startswith("Bearer sk_")
        for prefix, status in list(self.fail.items()):
            if path.startswith(prefix):
                del self.fail[prefix]
                return httpx.Response(status, json={"error": {"message": "fake failure"}})
        key = headers.get("idempotency-key")
        if key is not None and key in self.idempotent:
            return httpx.Response(200, json=self.idempotent[key])
        status, body = self.route(request.method, path, params)
        if key is not None and status == 200:
            self.idempotent[key] = body
        return httpx.Response(status, json=body)

    def route(self, method: str, path: str, p: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        parts = path.strip("/").split("/")[1:]
        match method, parts:
            case "POST", ["customers"]:
                customer = {"id": self.new_id("cus"), "object": "customer", **p}
                self.customers[customer["id"]] = customer
                return 200, customer
            case "GET", ["prices"]:
                keys = set(_list(p.get("lookup_keys")))
                data = [
                    x for x in self.prices.values() if x.get("lookup_key") in keys and x["active"]
                ]
                return 200, {"object": "list", "data": data}
            case "POST", ["prices"]:
                if p.pop("transfer_lookup_key", None) == "true":
                    for old in self.prices.values():
                        if old.get("lookup_key") == p["lookup_key"]:
                            old["lookup_key"] = None
                price = self.add_price(p.pop("lookup_key"), **p)
                if "unit_amount" in price:
                    price["unit_amount"] = int(price["unit_amount"])
                return 200, price
            case "GET", ["products", product_id]:
                if product_id not in self.products:
                    return 404, {"error": {"message": "No such product", "code": "missing"}}
                return 200, self.products[product_id]
            case "POST", ["products"]:
                product = {"object": "product", "active": True, **p}
                self.products[product["id"]] = product
                return 200, product
            case "POST", ["products", product_id]:
                product = self.products[product_id]
                product.update(p)
                product["active"] = p.get("active", "true") == "true"
                return 200, product
            case "GET", ["billing", "meters"]:
                return 200, {"object": "list", "data": list(self.meters.values())}
            case "POST", ["billing", "meters"]:
                meter = {"id": self.new_id("mtr"), "object": "billing.meter", **p}
                self.meters[meter["id"]] = meter
                return 200, meter
            case "POST", ["billing", "meter_events"]:
                self.meter_events.append(p)
                return 200, {"object": "billing.meter_event", **p}
            case "POST", ["checkout", "sessions"]:
                session = {
                    "id": self.new_id("cs"),
                    "object": "checkout.session",
                    "url": f"https://checkout.stripe.test/{next(self.ids)}",
                    "status": "open",
                    **p,
                }
                self.sessions[session["id"]] = session
                return 200, session
            case "GET", ["checkout", "sessions", session_id]:
                return 200, self.sessions[session_id]
            case "POST", ["checkout", "sessions", session_id, "expire"]:
                self.sessions[session_id]["status"] = "expired"
                return 200, self.sessions[session_id]
            case "DELETE", ["subscriptions", subscription_id]:
                if subscription_id not in self.subscriptions:
                    return 404, {"error": {"message": "No such subscription"}}
                self.subscriptions[subscription_id]["status"] = "canceled"
                return 200, self.subscriptions[subscription_id]
            case "DELETE", ["customers", customer_id]:
                if self.customers.pop(customer_id, None) is None:
                    return 404, {"error": {"message": "No such customer"}}
                for subscription in self.subscriptions.values():
                    if subscription["customer"] == customer_id:
                        subscription["status"] = "canceled"
                return 200, {"id": customer_id, "deleted": True}
            case "GET", ["billing_portal", "configurations"]:
                return 200, {"object": "list", "data": list(self.portal_configurations.values())}
            case "POST", ["billing_portal", "configurations"]:
                configuration = {"id": self.new_id("bpc"), "active": True, **p}
                self.portal_configurations[configuration["id"]] = configuration
                return 200, configuration
            case "POST", ["billing_portal", "configurations", configuration_id]:
                self.portal_configurations[configuration_id].update(p)
                return 200, self.portal_configurations[configuration_id]
            case "POST", ["billing_portal", "sessions"]:
                return 200, {"id": self.new_id("bps"), "url": "https://portal.stripe.test/s", **p}
            case "GET", ["subscriptions", subscription_id]:
                if subscription_id not in self.subscriptions:
                    return 404, {"error": {"message": "No such subscription"}}
                return 200, self.subscriptions[subscription_id]
            case "POST", ["subscriptions", subscription_id]:
                subscription = self.subscriptions[subscription_id]
                for change in _list(p.get("items") or {}):
                    item = next(i for i in subscription["items"]["data"] if i["id"] == change["id"])
                    item["price"] = self.prices[change["price"]]
                return 200, subscription
            case "GET", ["webhook_endpoints"]:
                return 200, {"object": "list", "data": list(self.endpoints.values())}
            case "POST", ["webhook_endpoints"]:
                endpoint = {
                    "id": self.new_id("we"),
                    "object": "webhook_endpoint",
                    "secret": f"whsec_new_{next(self.ids)}",
                    **p,
                }
                self.endpoints[endpoint["id"]] = endpoint
                return 200, endpoint
            case "POST", ["webhook_endpoints", endpoint_id]:
                self.endpoints[endpoint_id].update(p)
                return 200, {k: v for k, v in self.endpoints[endpoint_id].items() if k != "secret"}
            case "DELETE", ["webhook_endpoints", endpoint_id]:
                self.endpoints.pop(endpoint_id)
                return 200, {"id": endpoint_id, "deleted": True}
        return 404, {"error": {"message": f"the fake has no route {method} {path}"}}
