"""Tests of the Stripe setup script with a fake Stripe (docs/architecture.md, Section 17.5)."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from sama.billing import setup as billing_setup
from sama.billing.setup import WEBHOOK_EVENTS, setup
from tests.billing.fake_stripe import FakeStripe

URL = "https://sama.test/stripe/webhook"


async def test_a_second_run_finds_every_object(tmp_path: Path) -> None:
    stripe = FakeStripe()
    client = stripe.client()
    secret_file = tmp_path / "secret"
    first = await setup(client, webhook_url=URL, secret_file=secret_file)
    assert sorted(stripe.products) == ["sama_business", "sama_model_usage", "sama_pro"]
    keys = sorted(p["lookup_key"] for p in stripe.prices.values())
    assert keys == ["sama_business_monthly", "sama_model_usage", "sama_pro_monthly"]
    assert stripe.price_by_key("sama_pro_monthly")["unit_amount"] == 2000
    assert stripe.price_by_key("sama_business_monthly")["unit_amount"] == 9900
    usage = stripe.price_by_key("sama_model_usage")
    (meter,) = stripe.meters.values()
    assert meter["event_name"] == "sama_model_usage"
    assert usage["recurring"] == {
        "interval": "month",
        "usage_type": "metered",
        "meter": meter["id"],
    }
    (portal,) = stripe.portal_configurations.values()
    assert portal["metadata"] == {"sama": "billing_portal"}
    assert portal["features"]["subscription_update"] == {"enabled": "false"}
    (endpoint,) = stripe.endpoints.values()
    assert sorted(endpoint["enabled_events"].values()) == sorted(WEBHOOK_EVENTS)
    assert any("created" in line for line in first)

    second = await setup(client, webhook_url=URL, secret_file=secret_file)
    assert len(stripe.prices) == 3
    assert len(stripe.meters) == 1
    assert len(stripe.portal_configurations) == 1
    assert len(stripe.endpoints) == 1
    assert not any("created" in line or "replaced" in line for line in second)
    await client.aclose()


async def test_a_changed_price_takes_the_lookup_key(tmp_path: Path) -> None:
    stripe = FakeStripe()
    client = stripe.client()
    await setup(client)
    old = stripe.price_by_key("sama_pro_monthly")
    old["unit_amount"] = 1500
    report = await setup(client)
    new = stripe.price_by_key("sama_pro_monthly")
    assert new["id"] != old["id"]
    assert new["unit_amount"] == 2000
    assert old["lookup_key"] is None
    assert any(line.startswith("price sama_pro_monthly: replaced") for line in report)
    await client.aclose()


async def test_the_webhook_secret_goes_to_a_private_file_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stripe = FakeStripe()
    client = stripe.client()
    secret_file = tmp_path / "dir" / "secret"
    report = await setup(client, webhook_url=URL, secret_file=secret_file)
    (endpoint,) = stripe.endpoints.values()
    secret = endpoint["secret"]
    assert secret_file.read_text() == secret
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    assert secret not in "\n".join(report)
    assert secret not in capsys.readouterr().out
    # Rotate: a new endpoint and secret, and the old endpoint is gone.
    await setup(client, webhook_url=URL, secret_file=secret_file, rotate_webhook=True)
    (rotated,) = stripe.endpoints.values()
    assert rotated["id"] != endpoint["id"]
    assert secret_file.read_text() == rotated["secret"] != secret
    await client.aclose()


async def test_a_new_endpoint_needs_a_secret_file() -> None:
    stripe = FakeStripe()
    client = stripe.client()
    with pytest.raises(SystemExit):
        await setup(client, webhook_url=URL)
    await client.aclose()


def test_main_needs_a_test_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SAMA_STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    assert billing_setup.main([]) == 2
    assert "SAMA_STRIPE_SECRET_KEY" in capsys.readouterr().err
    monkeypatch.setenv("SAMA_STRIPE_SECRET_KEY", "sk_live_secret_value")
    assert billing_setup.main([]) == 2
    err = capsys.readouterr().err
    assert "--allow-live" in err
    assert "sk_live_secret_value" not in err
