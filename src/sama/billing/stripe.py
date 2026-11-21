"""A small Stripe API client (docs/architecture.md, Section 17.5).

Sama calls the Stripe REST API with httpx and does not use the Stripe SDK. The client pins the
API version, so a change of the account default in the Stripe dashboard does not change the
shape of the answers. The secret key is only in the ``Authorization`` header: no error message
and no log line has it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

import httpx

API_BASE: Final = "https://api.stripe.com"
API_VERSION: Final = "2025-03-31.basil"
"""The Stripe API version of every request and of the webhook endpoint. In this version the
billing period of a subscription is on its items."""
LIVE_PREFIXES: Final = ("sk_live_", "rk_live_")
TIMEOUT_S: Final = 20.0


class StripeError(Exception):
    """A failed Stripe call. The message is safe to log and has no secret."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def is_live_key(key: str) -> bool:
    return key.startswith(LIVE_PREFIXES)


def encode(params: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The form fields of nested Stripe parameters: ``{"a": {"b": [1]}}`` gives ``a[b][0]=1``.
    ``None`` values are left out. A bool is ``true`` or ``false``."""
    fields: list[tuple[str, str]] = []

    def add(key: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, Mapping):
            for name, item in value.items():
                add(f"{key}[{name}]", item)
        elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
            for index, item in enumerate(value):
                add(f"{key}[{index}]", item)
        elif isinstance(value, bool):
            fields.append((key, "true" if value else "false"))
        else:
            fields.append((key, str(value)))

    for name, value in params.items():
        add(name, value)
    return fields


class StripeClient:
    """The Stripe calls of sama. ``http`` is for tests (an ``httpx.MockTransport``)."""

    def __init__(
        self,
        secret_key: str,
        *,
        http: httpx.AsyncClient | None = None,
        base_url: str = API_BASE,
    ) -> None:
        self._key = secret_key
        self._http = http or httpx.AsyncClient(timeout=TIMEOUT_S, trust_env=False)
        self._base = base_url.rstrip("/")

    @property
    def live(self) -> bool:
        return is_live_key(self._key)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._key}", "Stripe-Version": API_VERSION}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        fields = encode(params or {})
        url = f"{self._base}{path}"
        try:
            # Stripe reads the parameters of GET and DELETE from the query string.
            if method in ("GET", "DELETE"):
                response = await self._http.request(
                    method, url, params=tuple(fields), headers=headers
                )
            else:
                response = await self._http.request(method, url, data=dict(fields), headers=headers)
        except httpx.HTTPError as error:
            raise StripeError(f"the Stripe API did not answer: {type(error).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if response.status_code >= 400:
            raw = body.get("error")
            detail: dict[str, Any] = raw if isinstance(raw, dict) else {}
            message = str(detail.get("message") or f"HTTP {response.status_code}")
            # Stripe can echo a part of the key ("Invalid API Key provided: sk_test_...").
            message = message.replace(self._key, "[redacted]")
            raise StripeError(
                f"the Stripe API refused {method} {path}: {message}",
                status_code=response.status_code,
            )
        return body

    # The calls of the billing service.

    async def create_customer(
        self, *, name: str, email: str | None, org_id: str, idempotency_key: str
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/v1/customers",
            {"name": name, "email": email, "metadata": {"org_id": org_id}},
            idempotency_key=idempotency_key,
        )

    async def prices(self, lookup_keys: Sequence[str]) -> dict[str, dict[str, Any]]:
        """The active prices with these lookup keys, by lookup key."""
        body = await self.request(
            "GET",
            "/v1/prices",
            {"lookup_keys": list(lookup_keys), "active": True, "limit": 100},
        )
        return {
            str(price["lookup_key"]): price
            for price in body.get("data", [])
            if isinstance(price, dict) and price.get("lookup_key")
        }

    async def create_checkout_session(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return await self.request("POST", "/v1/checkout/sessions", params)

    async def create_portal_session(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return await self.request("POST", "/v1/billing_portal/sessions", params)

    async def portal_configurations(self) -> list[dict[str, Any]]:
        body = await self.request(
            "GET", "/v1/billing_portal/configurations", {"active": True, "limit": 100}
        )
        return [item for item in body.get("data", []) if isinstance(item, dict)]

    async def subscription(self, subscription_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/v1/subscriptions/{subscription_id}")

    async def update_subscription(
        self, subscription_id: str, params: Mapping[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            f"/v1/subscriptions/{subscription_id}",
            params,
            idempotency_key=idempotency_key,
        )

    async def meter_event(
        self, *, event_name: str, customer_id: str, value: int, identifier: str, timestamp: int
    ) -> dict[str, Any]:
        """One usage record. Stripe drops a second event with the same ``identifier`` (24
        hours), so a retry after a crash does not bill two times."""
        return await self.request(
            "POST",
            "/v1/billing/meter_events",
            {
                "event_name": event_name,
                "identifier": identifier,
                "timestamp": timestamp,
                "payload": {"stripe_customer_id": customer_id, "value": value},
            },
        )
