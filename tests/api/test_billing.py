"""Tests of billing over HTTP with a fake Stripe (docs/architecture.md, Section 17.5)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import ApiSettings, JwtVerifier, SamaApi
from sama.billing import BillingService
from sama.billing.service import BillingError
from sama.datacontrols.service import DataControls, ServiceCleanup
from sama.db.models import (
    AgentProfile,
    AuditEntry,
    BillingEvent,
    ModelCall,
    Org,
    OrgBilling,
    Session,
    User,
    Workspace,
)
from sama.orchestrator.queue import RunQueue
from sama.orgs import OrgService, SignupPolicy
from sama.orgs.plans import BUSINESS, FREE, PRO, QUOTA_KEYS
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.billing.fake_stripe import PERIOD_END, PERIOD_START, WEBHOOK_SECRET, FakeStripe

WEB = "https://app.sama.test"


class Emails:
    def __init__(self) -> None:
        self.emails: dict[uuid.UUID, str] = {}

    async def verified_email(self, auth_user_id: uuid.UUID) -> str | None:
        return self.emails.get(auth_user_id)


@dataclass
class World:
    engine: AsyncEngine
    stripe: FakeStripe
    service: BillingService
    web: httpx.AsyncClient
    emails: Emails
    users: list[uuid.UUID] = field(default_factory=list)

    def person(self) -> uuid.UUID:
        auth_user_id = uuid.uuid4()
        self.users.append(auth_user_id)
        self.emails.emails[auth_user_id] = f"{auth_user_id.hex[:8]}@example.test"
        return auth_user_id

    def set_clock(self, now: datetime | float) -> None:
        """Sets the time of the billing service and of the webhook signatures."""
        stamp = now.timestamp() if isinstance(now, datetime) else now
        self.service._clock = lambda: stamp
        self.stripe.clock = lambda: stamp

    async def member(self, owner: uuid.UUID, org_id: str) -> uuid.UUID:
        """A new member of the org with the role ``member``."""
        person = self.person()
        created = await self.web.post(
            f"/orgs/{org_id}/invites",
            json={"email": self.emails.emails[person]},
            headers=bearer(owner),
        )
        assert created.status_code == 201, created.text
        accepted = await self.web.post(
            "/invites/accept", json={"token": created.json()["token"]}, headers=bearer(person)
        )
        assert accepted.status_code == 200, accepted.text
        return person

    async def org(self) -> tuple[uuid.UUID, dict[str, Any]]:
        owner = self.person()
        response = await self.web.post("/orgs", json={"name": "Acme"}, headers=bearer(owner))
        assert response.status_code == 201, response.text
        return owner, response.json()


def make_world(
    engine: AsyncEngine, stripe: FakeStripe, emails: Emails, *, enabled: bool = True
) -> tuple[BillingService, SamaApi]:
    service = BillingService(
        engine,
        stripe.client() if enabled else None,
        webhook_secret=WEBHOOK_SECRET if enabled else None,
        web_origins=(WEB, "https://preview.sama.test"),
    )
    api = SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        settings=ApiSettings(supabase_url="https://project.example.test", web_origins=(WEB,)),
        orgs=OrgService(
            engine, emails, SignupPolicy(mode="open"), plan_quotas=lambda: service.enabled
        ),
        billing=service,
    )
    return service, api


@pytest.fixture
async def world(engine: AsyncEngine) -> AsyncIterator[World]:
    stripe = FakeStripe()
    stripe.standard_prices()
    emails = Emails()
    service, api = make_world(engine, stripe, emails)
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as web:
        world = World(engine, stripe, service, web, emails)
        yield world
    await service.aclose()
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL sama.audit_purge = 'on'"))
        await connection.execute(delete(Org).where(Org.created_by_auth_user_id.in_(world.users)))


async def checkout(world: World, owner: uuid.UUID, org_id: str, plan: str = "pro") -> Any:
    return await world.web.post(
        f"/orgs/{org_id}/billing/checkout",
        json={"plan": plan},
        headers={**bearer(owner), "Origin": "https://preview.sama.test"},
    )


async def deliver(world: World, body: bytes, header: str) -> httpx.Response:
    return await world.web.post(
        "/stripe/webhook", content=body, headers={"Stripe-Signature": header}
    )


async def buy(world: World, plan: str = "pro") -> tuple[uuid.UUID, str, dict[str, Any]]:
    """An org that bought ``plan`` through Checkout and the webhook."""
    owner, org = await world.org()
    started = await checkout(world, owner, org["id"], plan)
    assert started.status_code == 200, started.text
    session = list(world.stripe.sessions.values())[-1]
    subscription = world.stripe.subscribe(session["customer"], f"sama_{plan}_monthly")
    body, header = world.stripe.event(
        "checkout.session.completed",
        {
            "id": session["id"],
            "mode": "subscription",
            "customer": session["customer"],
            "subscription": subscription["id"],
        },
    )
    answer = await deliver(world, body, header)
    assert answer.status_code == 200, answer.text
    assert answer.json()["applied"] is True
    return owner, org["id"], subscription


async def org_row(world: World, org_id: str) -> Any:
    async with world.engine.connect() as connection:
        return (
            await connection.execute(select(Org.plan, Org.limits).where(Org.id == org_id))
        ).one()


async def profile_budget(world: World, org_id: str) -> dict[str, Any]:
    async with world.engine.connect() as connection:
        budget = await connection.scalar(
            select(AgentProfile.budget).where(
                AgentProfile.org_id == org_id, AgentProfile.workspace_id.is_(None)
            )
        )
    assert isinstance(budget, dict)
    return budget


# Plans and the overview.


async def test_the_plans_are_public_and_list_the_prices(world: World) -> None:
    response = await world.web.get("/billing/plans")
    assert response.status_code == 200
    body = response.json()
    assert body["billing_enabled"] is True
    assert [plan["name"] for plan in body["plans"]] == ["free", "pro", "business"]
    pro = body["plans"][1]
    assert pro["price_usd"] == "20.00"
    assert pro["included_usd"] == "10.00"
    assert pro["model_usd_per_month"] == "200.00"
    config = (await world.web.get("/config")).json()
    assert config["billing"] is True


async def test_only_an_org_admin_sees_the_billing_page(world: World) -> None:
    owner, org = await world.org()
    stranger = world.person()
    answer = await world.web.get(f"/orgs/{org['id']}/billing", headers=bearer(stranger))
    assert answer.status_code == 404
    member = await world.member(owner, org["id"])
    answer = await world.web.get(f"/orgs/{org['id']}/billing", headers=bearer(member))
    assert answer.status_code == 403
    assert (await world.web.get(f"/orgs/{org['id']}/billing")).status_code == 401
    overview = await world.web.get(f"/orgs/{org['id']}/billing", headers=bearer(owner))
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert body["plan"]["name"] == "free"
    assert body["subscription"] is None
    assert body["can_buy"] is True
    assert body["can_change"] is False
    assert body["can_open_portal"] is False
    assert body["quotas"]["model_usd_per_month"] == "5.00"
    assert body["quotas"]["model_usd_this_month"] == "0.00"
    assert body["quotas"]["members"] == 2
    assert body["quotas"]["max_members"] == 5


# Checkout.


async def test_checkout_makes_one_customer_and_a_session_with_two_prices(world: World) -> None:
    owner, org = await world.org()
    assert (await checkout(world, owner, org["id"], "free")).status_code == 422
    assert (await checkout(world, owner, org["id"], "team")).status_code == 422
    first = await checkout(world, owner, org["id"])
    assert first.status_code == 200, first.text
    assert first.json()["url"].startswith("https://checkout.stripe.test/")
    second = await checkout(world, owner, org["id"], "business")
    assert second.status_code == 200
    assert len(world.stripe.customers) == 1
    customer = next(iter(world.stripe.customers.values()))
    assert customer["metadata"] == {"org_id": org["id"]}
    pro, business = world.stripe.sessions.values()
    assert pro["customer"] == business["customer"] == customer["id"]
    assert pro["mode"] == "subscription"
    assert pro["line_items"] == {
        "0": {"price": world.stripe.price_by_key("sama_pro_monthly")["id"], "quantity": "1"},
        "1": {"price": world.stripe.price_by_key("sama_model_usage")["id"]},
    }
    assert pro["subscription_data"] == {"metadata": {"org_id": org["id"]}}
    # The allowed origin of the request is the return address.
    back = f"https://preview.sama.test/billing?org={org['id']}"
    assert pro["success_url"] == f"{back}&checkout=success"
    assert pro["cancel_url"] == f"{back}&checkout=cancel"
    # The second checkout, for another plan, ended the first session.
    assert pro["status"] == "expired"
    assert business["status"] == "open"
    async with world.engine.connect() as connection:
        query = select(AuditEntry.action).where(AuditEntry.org_id == org["id"])
        actions = (await connection.scalars(query)).all()
    assert actions.count("billing.checkout_started") == 2


async def test_checkout_uses_the_first_origin_for_an_unknown_origin(world: World) -> None:
    owner, org = await world.org()
    answer = await world.web.post(
        f"/orgs/{org['id']}/billing/checkout",
        json={"plan": "pro"},
        headers={**bearer(owner), "Origin": "https://evil.test"},
    )
    assert answer.status_code == 200
    session = next(iter(world.stripe.sessions.values()))
    assert session["success_url"] == f"{WEB}/billing?org={org['id']}&checkout=success"


async def test_checkout_needs_an_admin_and_the_prices(world: World) -> None:
    owner, org = await world.org()
    member = await world.member(owner, org["id"])
    assert (await checkout(world, member, org["id"])).status_code == 403
    world.stripe.prices.clear()
    answer = await checkout(world, owner, org["id"])
    assert answer.status_code == 503
    assert "not set up" in answer.json()["detail"]
    world.stripe.standard_prices()
    world.stripe.fail["/v1/checkout"] = 500
    answer = await checkout(world, owner, org["id"])
    assert answer.status_code == 502


async def test_a_team_org_and_a_paying_org_cannot_check_out(world: World) -> None:
    owner, org_id, _ = await buy(world)
    answer = await checkout(world, owner, org_id, "business")
    assert answer.status_code == 409
    assert "change the plan" in answer.json()["detail"]
    team_owner, team = await world.org()
    async with world.engine.begin() as connection:
        await connection.execute(update(Org).where(Org.id == team["id"]).values(plan="team"))
    answer = await checkout(world, team_owner, team["id"])
    assert answer.status_code == 409


# The webhook.


async def test_a_completed_checkout_sets_the_plan_limits_and_budget(world: World) -> None:
    owner, org_id, subscription = await buy(world)
    org = await org_row(world, org_id)
    assert org.plan == "pro"
    assert {key: org.limits[key] for key in PRO.limits} == dict(PRO.limits)
    assert await profile_budget(world, org_id) == dict(PRO.run_budget)
    async with world.engine.connect() as connection:
        billing = (
            await connection.execute(select(OrgBilling).where(OrgBilling.org_id == org_id))
        ).one()
        audit = (
            await connection.execute(
                select(AuditEntry.actor_kind, AuditEntry.details).where(
                    AuditEntry.org_id == org_id, AuditEntry.action == "org.plan_changed"
                )
            )
        ).one()
    assert billing.stripe_subscription_id == subscription["id"]
    assert billing.status == "active"
    assert billing.plan == "pro"
    assert billing.current_period_start == datetime.fromtimestamp(PERIOD_START, UTC)
    assert billing.current_period_end == datetime.fromtimestamp(PERIOD_END, UTC)
    assert audit.actor_kind == "system"
    assert audit.details == {"from": "free", "to": "pro", "via": "webhook"}
    overview = (await world.web.get(f"/orgs/{org_id}/billing", headers=bearer(owner))).json()
    assert overview["can_buy"] is False
    assert overview["can_change"] is True
    assert overview["can_open_portal"] is True
    assert overview["usage"]["included_usd"] == "10.00"


async def test_a_second_delivery_of_an_event_is_not_applied_again(world: World) -> None:
    _, org_id, subscription = await buy(world)
    body, header = world.stripe.event(
        "customer.subscription.updated", subscription, event_id="evt_same"
    )
    first = await deliver(world, body, header)
    assert first.json() == {"received": True, "applied": True, "reason": "applied"}
    again = await deliver(world, body, header)
    assert again.status_code == 200
    assert again.json()["reason"] == "duplicate"
    async with world.engine.connect() as connection:
        count = await connection.scalar(
            select(text("count(*)")).select_from(BillingEvent).where(BillingEvent.org_id == org_id)
        )
    assert count == 2


async def test_a_bad_or_missing_signature_is_refused(world: World) -> None:
    body, header = world.stripe.event("customer.subscription.updated", {"id": "sub_x"})
    assert (await deliver(world, body, header.replace("v1=", "v1=0"))).status_code == 400
    assert (await world.web.post("/stripe/webhook", content=body)).status_code == 400
    assert (await deliver(world, body + b" ", header)).status_code == 400


async def test_events_that_sama_does_not_use_are_accepted_and_ignored(world: World) -> None:
    cases = [
        (world.stripe.event("invoice.paid", {"id": "in_1"}), "ignored type"),
        (world.stripe.event("customer.subscription.updated", {"id": "s"}, live=True), "other mode"),
        (
            world.stripe.event(
                "customer.subscription.updated", {"id": "sub_1", "customer": "cus_unknown"}
            ),
            "unknown customer",
        ),
        (
            world.stripe.event("checkout.session.completed", {"mode": "payment", "customer": "c"}),
            "not a subscription",
        ),
    ]
    for (body, header), reason in cases:
        answer = await deliver(world, body, header)
        assert answer.status_code == 200, answer.text
        assert answer.json() == {"received": True, "applied": False, "reason": reason}


async def test_stripe_gets_a_retry_when_the_subscription_does_not_load(world: World) -> None:
    owner, org = await world.org()
    await checkout(world, owner, org["id"])
    customer_id = next(iter(world.stripe.customers))
    subscription = world.stripe.subscribe(customer_id)
    body, header = world.stripe.event("customer.subscription.created", subscription)
    world.stripe.fail["/v1/subscriptions"] = 500
    answer = await deliver(world, body, header)
    assert answer.status_code == 503
    assert (await org_row(world, org["id"])).plan == "free"
    retry = await deliver(world, body, header)
    assert retry.status_code == 200
    assert (await org_row(world, org["id"])).plan == "pro"


async def test_a_cancelled_subscription_moves_the_org_to_free(world: World) -> None:
    _, org_id, subscription = await buy(world)
    subscription["cancel_at_period_end"] = True
    body, header = world.stripe.event("customer.subscription.updated", subscription)
    await deliver(world, body, header)
    async with world.engine.connect() as connection:
        assert await connection.scalar(
            select(OrgBilling.cancel_at_period_end).where(OrgBilling.org_id == org_id)
        )
    assert (await org_row(world, org_id)).plan == "pro"
    subscription["status"] = "canceled"
    body, header = world.stripe.event("customer.subscription.deleted", subscription)
    await deliver(world, body, header)
    org = await org_row(world, org_id)
    assert org.plan == "free"
    assert {key: org.limits[key] for key in FREE.limits} == dict(FREE.limits)
    assert await profile_budget(world, org_id) == dict(FREE.run_budget)


async def test_the_end_of_an_old_subscription_keeps_the_new_one(world: World) -> None:
    _, org_id, old = await buy(world)
    customer_id = old["customer"]
    new = world.stripe.subscribe(customer_id, "sama_business_monthly")
    body, header = world.stripe.event("customer.subscription.created", new)
    await deliver(world, body, header)
    assert (await org_row(world, org_id)).plan == "business"
    old["status"] = "canceled"
    body, header = world.stripe.event("customer.subscription.deleted", old)
    assert (await deliver(world, body, header)).status_code == 200
    assert (await org_row(world, org_id)).plan == "business"


async def test_a_budget_that_an_admin_changed_stays(world: World) -> None:
    owner, org = await world.org()
    custom = {"max_turns": 7}
    async with world.engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.org_id == org["id"], AgentProfile.workspace_id.is_(None))
            .values(budget=custom)
        )
    await checkout(world, owner, org["id"])
    customer_id = next(iter(world.stripe.customers))
    body, header = world.stripe.event(
        "customer.subscription.created", world.stripe.subscribe(customer_id)
    )
    await deliver(world, body, header)
    assert (await org_row(world, org["id"])).plan == "pro"
    assert await profile_budget(world, org["id"]) == custom


# Plan change and portal.


async def test_an_admin_changes_the_plan_and_stripe_prorates(world: World) -> None:
    owner, org_id, subscription = await buy(world)
    base = subscription["items"]["data"][0]
    same = await world.web.put(
        f"/orgs/{org_id}/billing/plan", json={"plan": "pro"}, headers=bearer(owner)
    )
    assert same.status_code == 409
    assert (
        await world.web.put(
            f"/orgs/{org_id}/billing/plan", json={"plan": "free"}, headers=bearer(owner)
        )
    ).status_code == 422
    changed = await world.web.put(
        f"/orgs/{org_id}/billing/plan", json={"plan": "business"}, headers=bearer(owner)
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["plan"]["name"] == "business"
    (update_call,) = world.stripe.calls("POST", f"/v1/subscriptions/{subscription['id']}")
    assert update_call["items"] == {
        "0": {"id": base["id"], "price": world.stripe.price_by_key("sama_business_monthly")["id"]}
    }
    assert update_call["proration_behavior"] == "create_prorations"
    org = await org_row(world, org_id)
    assert org.plan == "business"
    assert org.limits["model_usd_per_month"] == BUSINESS.limits["model_usd_per_month"]


async def test_a_plan_change_needs_a_paid_plan(world: World) -> None:
    owner, org = await world.org()
    answer = await world.web.put(
        f"/orgs/{org['id']}/billing/plan", json={"plan": "business"}, headers=bearer(owner)
    )
    assert answer.status_code == 409


async def test_the_portal_uses_the_sama_configuration(world: World) -> None:
    owner, org = await world.org()
    portal = await world.web.post(f"/orgs/{org['id']}/billing/portal", headers=bearer(owner))
    assert portal.status_code == 409
    world.stripe.portal_configurations["bpc_other"] = {"id": "bpc_other", "metadata": {}}
    world.stripe.portal_configurations["bpc_sama"] = {
        "id": "bpc_sama",
        "metadata": {"sama": "billing_portal"},
    }
    await checkout(world, owner, org["id"])
    portal = await world.web.post(f"/orgs/{org['id']}/billing/portal", headers=bearer(owner))
    assert portal.status_code == 200, portal.text
    assert portal.json()["url"] == "https://portal.stripe.test/s"
    (call,) = world.stripe.calls("POST", "/v1/billing_portal/sessions")
    assert call["configuration"] == "bpc_sama"
    assert call["return_url"] == f"{WEB}/billing?org={org['id']}"
    assert call["customer"] == next(iter(world.stripe.customers))


# Billing off.


async def test_without_stripe_the_pages_work_and_purchases_answer_409(
    engine: AsyncEngine,
) -> None:
    stripe, emails = FakeStripe(), Emails()
    _, api = make_world(engine, stripe, emails, enabled=False)
    owner = uuid.uuid4()
    emails.emails[owner] = "off@example.test"
    transport = httpx.ASGITransport(app=api.app())
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as web:
            org = (await web.post("/orgs", json={"name": "Off"}, headers=bearer(owner))).json()
            overview = await web.get(f"/orgs/{org['id']}/billing", headers=bearer(owner))
            assert overview.status_code == 200
            assert overview.json()["can_buy"] is False
            assert overview.json()["billing_enabled"] is False
            answer = await web.post(
                f"/orgs/{org['id']}/billing/checkout", json={"plan": "pro"}, headers=bearer(owner)
            )
            assert answer.status_code == 409
            assert (await web.post("/stripe/webhook", content=b"{}")).status_code == 404
            assert (await web.get("/config")).json()["billing"] is False
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL sama.audit_purge = 'on'"))
            await connection.execute(delete(Org).where(Org.created_by_auth_user_id == owner))
    assert stripe.requests == []


# The usage report.


async def add_spend(
    world: World, org_id: str, cost: str, at: datetime, *, key_source: str = "sama"
) -> None:
    """One model call of ``cost`` USD at ``at`` in a new cancelled run of the org."""
    org = uuid.UUID(org_id)
    async with world.engine.begin() as connection:
        profile = await connection.scalar(select(AgentProfile.id).where(AgentProfile.org_id == org))
        workspace = await connection.scalar(select(Workspace.id).where(Workspace.org_id == org))
        user = await connection.scalar(select(User.id).where(User.org_id == org))
        session = await connection.scalar(
            insert(Session)
            .values(org_id=org, workspace_id=workspace, agent_profile_id=profile, audience="shared")
            .returning(Session.id)
        )
    assert session is not None
    queue = RunQueue(world.engine)
    run_id = await queue.enqueue(org_id=org, session_id=session, text="x", unattended=True)
    await queue.cancel(org_id=org, run_id=run_id)
    async with world.engine.begin() as connection:
        await connection.execute(
            insert(ModelCall).values(
                call_id=uuid.uuid4(),
                org_id=org,
                workspace_id=workspace,
                session_id=session,
                run_id=run_id,
                user_id=user,
                provider="anthropic",
                model="claude-x",
                endpoint="messages",
                status_code=200,
                cost_usd=Decimal(cost),
                duration_ms=10,
                created_at=at,
                key_source=key_source,
            )
        )


def at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 1, day, hour, tzinfo=UTC)


async def test_the_usage_over_the_included_amount_goes_to_the_meter(world: World) -> None:
    world.set_clock(at(20))
    _, org_id, _ = await buy(world)
    customer_id = next(iter(world.stripe.customers))
    await add_spend(world, org_id, "6", at(2))
    assert await world.service.report_usage() == 0  # USD 6 is in the USD 10 included
    assert world.stripe.meter_events == []
    await add_spend(world, org_id, "6.506", at(3))
    assert await world.service.report_usage() == 1
    (event,) = world.stripe.meter_events
    assert event["event_name"] == "sama_model_usage"
    assert event["payload"] == {"stripe_customer_id": customer_id, "value": "250"}
    assert event["identifier"] == f"sama-{org_id}-{PERIOD_START}-250"
    assert int(event["timestamp"]) == int(at(20).timestamp())
    # Nothing new: no event.
    assert await world.service.report_usage() == 0
    await add_spend(world, org_id, "1", at(4))
    assert await world.service.report_usage() == 1
    assert world.stripe.meter_events[-1]["payload"]["value"] == "100"
    assert world.stripe.meter_events[-1]["identifier"] == f"sama-{org_id}-{PERIOD_START}-350"
    overview = await world.service.overview(world.users[0], uuid.UUID(org_id))
    assert overview["usage"]["model_usd"] == "13.51"
    assert overview["usage"]["billable_usd"] == "3.50"
    assert overview["usage"]["reported_usd"] == "3.50"


async def test_a_failed_meter_event_is_sent_again_with_the_same_identifier(
    world: World,
) -> None:
    world.set_clock(at(20))
    _, org_id, _ = await buy(world)
    await add_spend(world, org_id, "11", at(2))
    world.stripe.fail["/v1/billing/meter_events"] = 500
    assert await world.service.report_usage() == 0
    async with world.engine.connect() as connection:
        pending = await connection.scalar(
            select(OrgBilling.usage_pending_cents).where(OrgBilling.org_id == org_id)
        )
    assert pending == 100
    await add_spend(world, org_id, "5", at(3))
    assert await world.service.report_usage() == 2
    first, second = world.stripe.meter_events
    assert first["identifier"] == f"sama-{org_id}-{PERIOD_START}-100"
    assert first["payload"]["value"] == "100"
    assert second["payload"]["value"] == "500"


async def test_a_new_period_closes_the_old_one_first(world: World) -> None:
    world.set_clock(at(20))
    _, org_id, subscription = await buy(world)
    await add_spend(world, org_id, "10", at(2))
    assert await world.service.report_usage() == 0
    # Late usage of the old period, then the period changes.
    await add_spend(world, org_id, "2", at(31, 23))
    new_start, new_end = PERIOD_END, PERIOD_END + 28 * 86_400
    for item in subscription["items"]["data"]:
        item["current_period_start"], item["current_period_end"] = new_start, new_end
    body, header = world.stripe.event("customer.subscription.updated", subscription)
    await deliver(world, body, header)
    await add_spend(world, org_id, "15", datetime.fromtimestamp(new_start + 3600, UTC))
    world.set_clock(new_start + 7200)
    assert await world.service.report_usage() == 3
    old, new = world.stripe.meter_events
    assert old["payload"]["value"] == "200"
    assert int(old["timestamp"]) == PERIOD_END - 1
    assert old["identifier"] == f"sama-{org_id}-{PERIOD_START}-200"
    assert new["payload"]["value"] == "500"
    assert new["identifier"] == f"sama-{org_id}-{new_start}-500"
    async with world.engine.connect() as connection:
        row = (
            await connection.execute(select(OrgBilling).where(OrgBilling.org_id == org_id))
        ).one()
    assert row.usage_period_start == datetime.fromtimestamp(new_start, UTC)
    assert row.usage_reported_cents == 500
    assert row.usage_pending_cents is None


async def test_an_org_that_does_not_pay_has_no_usage_report(world: World) -> None:
    world.set_clock(at(20))
    _, org_id, subscription = await buy(world)
    subscription["status"] = "canceled"
    body, header = world.stripe.event("customer.subscription.deleted", subscription)
    await deliver(world, body, header)
    await add_spend(world, org_id, "50", at(2))
    assert await world.service.report_usage() == 0
    assert world.stripe.meter_events == []


# Billing on and off at start.


async def test_a_new_org_gets_the_free_quotas_only_while_billing_is_on(
    world: World, engine: AsyncEngine
) -> None:
    _, on = await world.org()
    assert (await org_row(world, on["id"])).limits == dict(FREE.limits)
    stripe, emails = FakeStripe(), Emails()
    _, api = make_world(engine, stripe, emails, enabled=False)
    owner = world.person()
    emails.emails[owner] = world.emails.emails[owner]
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as web:
        off = (await web.post("/orgs", json={"name": "Off"}, headers=bearer(owner))).json()
    limits = (await org_row(world, off["id"])).limits
    assert not set(QUOTA_KEYS) & set(limits)
    assert limits == {k: v for k, v in FREE.limits.items() if k not in QUOTA_KEYS}


async def test_start_turns_billing_off_when_stripe_refuses_the_key(engine: AsyncEngine) -> None:
    stripe = FakeStripe()
    service = BillingService(engine, stripe.client(), webhook_secret=WEBHOOK_SECRET)
    stripe.fail["/v1/prices"] = 401
    await service.start()
    assert service.enabled is False
    assert await service.report_usage() == 0
    with pytest.raises(BillingError) as error:
        await service.checkout(uuid.uuid4(), uuid.uuid4(), "pro", None)
    assert error.value.status_code == 409
    await service.aclose()


async def test_start_keeps_billing_on_after_a_network_error(world: World) -> None:
    world.stripe.fail["/v1/prices"] = 503
    await world.service.start()
    assert world.service.enabled is True


async def test_start_with_billing_on_adds_the_free_quotas_that_are_missing(
    world: World,
) -> None:
    _, bare = await world.org()
    _, custom = await world.org()
    _, team = await world.org()
    async with world.engine.begin() as connection:
        await connection.execute(
            update(Org).where(Org.id == bare["id"]).values(limits={"max_running_runs": 3})
        )
        await connection.execute(
            update(Org)
            .where(Org.id == custom["id"])
            .values(limits={"model_usd_per_month": "50.00"})
        )
        await connection.execute(
            update(Org).where(Org.id == team["id"]).values(plan="team", limits={})
        )
    await world.service.start()
    assert (await org_row(world, bare["id"])).limits == {
        "max_running_runs": 3,
        "model_usd_per_month": "5.00",
        "model_usd_per_user_day": "2.00",
    }
    assert (await org_row(world, custom["id"])).limits == {
        "model_usd_per_month": "50.00",
        "model_usd_per_user_day": "2.00",
    }
    assert (await org_row(world, team["id"])).limits == {}


async def test_start_with_billing_off_adds_no_quota(engine: AsyncEngine, world: World) -> None:
    _, org = await world.org()
    async with world.engine.begin() as connection:
        await connection.execute(update(Org).where(Org.id == org["id"]).values(limits={}))
    service = BillingService(engine)
    await service.start()
    assert (await org_row(world, org["id"])).limits == {}


# Review fixes: one open checkout, renewal, stuck delivery, replaced price, org delete.


async def test_a_second_checkout_for_the_same_plan_reuses_the_open_session(
    world: World,
) -> None:
    owner, org = await world.org()
    first = (await checkout(world, owner, org["id"])).json()["url"]
    second = await checkout(world, owner, org["id"])
    assert second.status_code == 200
    assert second.json()["url"] == first
    assert len(world.stripe.sessions) == 1


async def test_a_paid_checkout_without_its_webhook_stops_a_new_checkout(world: World) -> None:
    owner, org = await world.org()
    await checkout(world, owner, org["id"])
    session = next(iter(world.stripe.sessions.values()))
    session["status"] = "complete"
    answer = await checkout(world, owner, org["id"], "business")
    assert answer.status_code == 409
    assert "in progress" in answer.json()["detail"]
    assert len(world.stripe.sessions) == 1


async def test_the_webhook_clears_the_open_checkout(world: World) -> None:
    _, org_id, _ = await buy(world)
    async with world.engine.connect() as connection:
        row = (
            await connection.execute(select(OrgBilling).where(OrgBilling.org_id == org_id))
        ).one()
    assert row.checkout_session_id is None
    assert row.checkout_url is None


async def test_usage_after_the_period_end_waits_for_the_renewal(world: World) -> None:
    world.set_clock(at(20))
    _, org_id, subscription = await buy(world)
    await add_spend(world, org_id, "21", at(2))
    assert await world.service.report_usage() == 1
    assert world.stripe.meter_events[-1]["payload"]["value"] == "1100"
    # The period ended, and the renewal webhook did not come yet.
    new_start, new_end = PERIOD_END, PERIOD_END + 28 * 86_400
    await add_spend(world, org_id, "11", datetime.fromtimestamp(new_start + 3600, UTC))
    world.set_clock(new_start + 7200)
    assert await world.service.report_usage() == 0
    assert len(world.stripe.meter_events) == 1
    for item in subscription["items"]["data"]:
        item["current_period_start"], item["current_period_end"] = new_start, new_end
    body, header = world.stripe.event("customer.subscription.updated", subscription)
    await deliver(world, body, header)
    assert await world.service.report_usage() == 2
    new = world.stripe.meter_events[-1]
    # USD 11 in the new period, USD 10 included: USD 1, not USD 12.
    assert new["payload"]["value"] == "100"
    assert new["identifier"] == f"sama-{org_id}-{new_start}-100"


async def test_a_meter_event_that_stays_pending_too_long_is_not_sent_again(
    world: World,
) -> None:
    world.set_clock(at(10))
    _, org_id, _ = await buy(world)
    await add_spend(world, org_id, "11", at(2))
    world.stripe.fail["/v1/billing/meter_events"] = 500
    assert await world.service.report_usage() == 0
    world.set_clock(at(10, 13))
    assert await world.service.report_usage() == 0
    assert world.stripe.meter_events == []
    async with world.engine.connect() as connection:
        pending = await connection.scalar(
            select(OrgBilling.usage_pending_cents).where(OrgBilling.org_id == org_id)
        )
    assert pending == 100


async def test_subscribers_of_a_replaced_price_keep_their_plan(world: World) -> None:
    owner, org_id, subscription = await buy(world)
    old = world.stripe.price_by_key("sama_pro_monthly")
    old["metadata"] = {"plan": "pro"}
    old["lookup_key"] = None
    world.stripe.add_price("sama_pro_monthly", unit_amount=2500, metadata={"plan": "pro"})
    body, header = world.stripe.event("customer.subscription.updated", subscription)
    assert (await deliver(world, body, header)).json()["applied"] is True
    assert (await org_row(world, org_id)).plan == "pro"
    changed = await world.web.put(
        f"/orgs/{org_id}/billing/plan", json={"plan": "business"}, headers=bearer(owner)
    )
    assert changed.status_code == 200, changed.text
    assert (await org_row(world, org_id)).plan == "business"


async def test_an_org_delete_reports_the_last_usage_and_ends_the_subscription(
    world: World,
) -> None:
    world.set_clock(at(20))
    _, org_id, subscription = await buy(world)
    await add_spend(world, org_id, "12", at(2))
    assert await world.service.close_org(uuid.UUID(org_id)) is True
    assert world.stripe.meter_events[-1]["payload"]["value"] == "200"
    (cancel,) = world.stripe.calls("DELETE", f"/v1/subscriptions/{subscription['id']}")
    assert cancel == {"invoice_now": "true", "prorate": "false"}
    assert world.stripe.subscriptions[subscription["id"]]["status"] == "canceled"
    # The customer stays, so Stripe can collect the final invoice.
    assert subscription["customer"] in world.stripe.customers
    async with world.engine.connect() as connection:
        status = await connection.scalar(
            select(OrgBilling.status).where(OrgBilling.org_id == org_id)
        )
    assert status == "canceled"
    # A second call has nothing more to end.
    assert await world.service.close_org(uuid.UUID(org_id)) is True
    _, other = await world.org()
    assert await world.service.close_org(uuid.UUID(other["id"])) is False


async def test_an_org_delete_stops_when_the_last_usage_is_not_in_stripe(world: World) -> None:
    world.set_clock(at(20))
    _, org_id, subscription = await buy(world)
    await add_spend(world, org_id, "12", at(2))
    world.stripe.fail["/v1/billing/meter_events"] = 500
    with pytest.raises(BillingError) as error:
        await world.service.close_org(uuid.UUID(org_id))
    assert error.value.status_code == 409
    assert world.stripe.subscriptions[subscription["id"]]["status"] == "active"
    # The pending event is too old to send again: the delete still stops.
    world.set_clock(at(21))
    with pytest.raises(BillingError):
        await world.service.close_org(uuid.UUID(org_id))
    assert world.stripe.calls("DELETE", f"/v1/subscriptions/{subscription['id']}") == []


async def test_a_late_event_of_an_older_paid_subscription_does_not_win(world: World) -> None:
    _, org_id, old = await buy(world)
    new = world.stripe.subscribe(old["customer"], "sama_business_monthly")
    body, header = world.stripe.event("customer.subscription.created", new)
    await deliver(world, body, header)
    assert (await org_row(world, org_id)).plan == "business"
    body, header = world.stripe.event("customer.subscription.updated", old)
    assert (await deliver(world, body, header)).status_code == 200
    assert (await org_row(world, org_id)).plan == "business"
    async with world.engine.connect() as connection:
        tracked = await connection.scalar(
            select(OrgBilling.stripe_subscription_id).where(OrgBilling.org_id == org_id)
        )
    assert tracked == new["id"]


async def test_an_org_delete_with_billing_off_stops_when_the_org_has_a_customer(
    world: World, engine: AsyncEngine
) -> None:
    _, org_id, _ = await buy(world)
    with pytest.raises(BillingError) as error:
        await BillingService(engine).close_org(uuid.UUID(org_id))
    assert error.value.status_code == 503


async def test_calls_on_the_own_key_of_the_org_are_not_billed(world: World) -> None:
    world.set_clock(at(20))
    owner, org_id, _ = await buy(world)
    await add_spend(world, org_id, "30", at(2), key_source="org")
    await add_spend(world, org_id, "11", at(3))
    assert await world.service.report_usage() == 1
    assert world.stripe.meter_events[-1]["payload"]["value"] == "100"
    overview = await world.service.overview(owner, uuid.UUID(org_id))
    assert overview["usage"]["model_usd"] == "11.00"
    # The quota counts both sources, as the model proxy does.
    assert overview["quotas"]["model_usd_this_month"] == "41.00"


# The org delete of M6.8 ends the Stripe subscription first.


async def owner_user_id(world: World, org_id: str) -> uuid.UUID:
    async with world.engine.connect() as connection:
        user_id = await connection.scalar(select(User.id).where(User.org_id == uuid.UUID(org_id)))
    assert isinstance(user_id, uuid.UUID)
    return user_id


async def test_the_org_delete_ends_the_subscription_before_the_rows_go(world: World) -> None:
    world.set_clock(at(20))
    _, org_id, subscription = await buy(world)
    await add_spend(world, org_id, "12", at(2))
    actor = await owner_user_id(world, org_id)
    data = DataControls(world.engine, cleanup=ServiceCleanup(world.engine, billing=world.service))
    await data.delete_org(uuid.UUID(org_id), actor_user_id=actor)
    assert world.stripe.meter_events[-1]["payload"]["value"] == "200"
    assert world.stripe.subscriptions[subscription["id"]]["status"] == "canceled"
    async with world.engine.connect() as connection:
        assert await connection.scalar(select(Org.id).where(Org.id == uuid.UUID(org_id))) is None
        assert (
            await connection.scalar(
                select(OrgBilling.id).where(OrgBilling.org_id == uuid.UUID(org_id))
            )
            is None
        )


async def test_a_billing_failure_stops_the_org_delete(world: World) -> None:
    world.set_clock(at(20))
    _, org_id, subscription = await buy(world)
    await add_spend(world, org_id, "12", at(2))
    world.stripe.fail["/v1/billing/meter_events"] = 500
    actor = await owner_user_id(world, org_id)
    data = DataControls(world.engine, cleanup=ServiceCleanup(world.engine, billing=world.service))
    with pytest.raises(BillingError):
        await data.delete_org(uuid.UUID(org_id), actor_user_id=actor)
    assert world.stripe.subscriptions[subscription["id"]]["status"] == "active"
    assert (await org_row(world, org_id)).plan == "pro"
