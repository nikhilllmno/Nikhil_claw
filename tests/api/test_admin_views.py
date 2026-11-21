"""Tests of the admin views and of the agent profile create route (docs/architecture.md,
Section 17.5, M6.9)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api.admin import MAX_REPORT_CHARS, usage_window
from sama.db.models import AgentProfile, AuditEntry, Job, JobFire, ModelCall, Org
from sama.profiles import MAX_PROFILES_PER_ORG
from tests.api.conftest import bearer
from tests.api.test_app import add_user, mia
from tests.api.test_history import add_run
from tests.orchestrator.conftest import Tenant, add_tenant

pytestmark = pytest.mark.usefixtures("tenant")

# Built from parts, so the secret scanner does not see a key in the source.
FAKE_KEY = "sk-" + "a1b2c3d4" * 4


async def admin(engine: AsyncEngine, tenant: Tenant, role: str = "admin") -> dict[str, str]:
    return bearer(await add_user(engine, tenant, workspace_member=False, role=role))


async def add_call(
    engine: AsyncEngine,
    tenant: Tenant,
    run_id: uuid.UUID,
    *,
    session: str = "s1",
    cost: str = "0.10",
    model: str = "claude-x",
    key_source: str = "sama",
    user: bool = True,
    when: datetime | None = None,
) -> None:
    values: dict[str, Any] = {
        "call_id": uuid.uuid4(),
        "org_id": tenant.org,
        "workspace_id": tenant.ids["ws"],
        "session_id": tenant.ids[session],
        "run_id": run_id,
        "user_id": tenant.ids["mia"] if user else None,
        "provider": "anthropic",
        "model": model,
        "endpoint": "messages",
        "status_code": 200,
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 5,
        "key_source": key_source,
        "cost_usd": Decimal(cost),
        "duration_ms": 900,
    }
    if when is not None:
        values["created_at"] = when
    async with engine.begin() as connection:
        await connection.execute(insert(ModelCall).values(**values))


# -- usage -----------------------------------------------------------------------------------


def test_the_usage_window_starts_at_midnight_utc() -> None:
    now = datetime(2026, 11, 21, 13, 30, tzinfo=UTC)
    since, until = usage_window(7, now)
    assert since == datetime(2026, 11, 15, tzinfo=UTC)
    assert until == now


async def test_an_admin_reads_the_usage_of_the_org(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    run_shared = await add_run(engine, tenant, "s1")
    run_private = await add_run(engine, tenant, "mine")
    await add_call(engine, tenant, run_shared, cost="0.10")
    await add_call(engine, tenant, run_shared, cost="0.20", model="claude-y", key_source="org")
    # A private session of mia: an admin sees its cost, but not its text.
    await add_call(engine, tenant, run_private, session="mine", cost="0.05", user=False)
    # Out of the window.
    old = datetime.now(UTC) - timedelta(days=40)
    await add_call(engine, tenant, run_shared, cost="9.00", when=old)

    response = await client.get(f"/orgs/{tenant.org}/usage", headers=await admin(engine, tenant))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["days"] == 30
    assert len(body["by_day"]) == 30
    totals = body["totals"]
    assert Decimal(totals["cost_usd"]) == Decimal("0.35")
    assert Decimal(totals["org_key_cost_usd"]) == Decimal("0.20")
    assert totals["model_calls"] == 3
    assert totals["runs"] == 2
    assert totals["sessions"] == 2
    assert totals["input_tokens"] == 300
    assert totals["cache_read_tokens"] == 15
    assert Decimal(body["by_day"][-1]["cost_usd"]) == Decimal("0.35")
    assert sum(day["model_calls"] for day in body["by_day"]) == 3

    users = {row["label"]: Decimal(row["cost_usd"]) for row in body["by_user"]}
    assert users == {"mia": Decimal("0.30"), "Jobs and system": Decimal("0.05")}
    profiles = body["by_profile"]
    assert [(p["key"], p["label"]) for p in profiles] == [(str(tenant.ids["profile"]), "p")]
    models = [row["key"] for row in body["by_model"]]
    assert models == ["anthropic/claude-y", "anthropic/claude-x"]
    sources = {row["key"]: row["label"] for row in body["by_key_source"]}
    assert sources == {"org": "Org key", "sama": "Sama key"}


async def test_the_usage_takes_the_days(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    headers = await admin(engine, tenant)
    response = await client.get(f"/orgs/{tenant.org}/usage?days=7", headers=headers)
    assert response.status_code == 200
    assert len(response.json()["by_day"]) == 7
    assert Decimal(response.json()["totals"]["cost_usd"]) == 0
    for days in (0, 91):
        bad = await client.get(f"/orgs/{tenant.org}/usage?days={days}", headers=headers)
        assert bad.status_code == 422


async def test_only_an_admin_reads_the_admin_views(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    for path in ("usage", "audit", "curator-reports"):
        member = await client.get(f"/orgs/{tenant.org}/{path}", headers=mia(tenant))
        assert member.status_code == 403, path
        stranger = await client.get(f"/orgs/{tenant.org}/{path}", headers=bearer(uuid.uuid4()))
        assert stranger.status_code == 404, path


async def test_an_admin_of_another_org_cannot_read_the_views(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    other = await add_tenant(engine)
    try:
        headers = await admin(engine, other)
        for path in ("usage", "audit", "curator-reports"):
            response = await client.get(f"/orgs/{tenant.org}/{path}", headers=headers)
            assert response.status_code == 404, path
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL sama.audit_purge = 'on'"))
            await connection.execute(delete(Org).where(Org.id == other.org))


# -- audit -----------------------------------------------------------------------------------


async def add_audit(engine: AsyncEngine, tenant: Tenant, action: str, **details: Any) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            insert(AuditEntry).values(
                org_id=tenant.org,
                actor_kind="user",
                actor_user_id=tenant.ids["mia"],
                action=action,
                target_type="org",
                target_id=str(tenant.org),
                details=details,
            )
        )


async def test_the_audit_log_pages_and_filters(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    headers = await admin(engine, tenant, role="owner")
    for n in range(5):
        await add_audit(engine, tenant, "member.role_changed", n=n)
    await add_audit(engine, tenant, "invite.created", token=FAKE_KEY)
    await add_audit(engine, tenant, "member_x.other")

    first = (await client.get(f"/orgs/{tenant.org}/audit?limit=3", headers=headers)).json()
    assert [e["action"] for e in first["entries"]] == [
        "member_x.other",
        "invite.created",
        "member.role_changed",
    ]
    assert first["entries"][0]["actor_name"] == "mia"
    # The redactor runs again on the details.
    assert FAKE_KEY not in str(first["entries"][1]["details"])
    assert first["next_before"] == first["entries"][-1]["id"]

    rest = (
        await client.get(
            f"/orgs/{tenant.org}/audit?limit=50&before={first['next_before']}", headers=headers
        )
    ).json()
    assert len(rest["entries"]) == 4
    assert rest["next_before"] is None

    # A prefix filter. "_" is not a wildcard.
    members = (await client.get(f"/orgs/{tenant.org}/audit?action=member_", headers=headers)).json()
    assert [e["action"] for e in members["entries"]] == ["member_x.other"]
    members = (await client.get(f"/orgs/{tenant.org}/audit?action=member.", headers=headers)).json()
    assert len(members["entries"]) == 5

    by_actor = (
        await client.get(f"/orgs/{tenant.org}/audit?actor_user_id={uuid.uuid4()}", headers=headers)
    ).json()
    assert by_actor["entries"] == []

    too_long = await client.get(f"/orgs/{tenant.org}/audit?action={'a' * 65}", headers=headers)
    assert too_long.status_code == 422


# -- curator reports -------------------------------------------------------------------------


async def test_the_curator_reports_of_the_org(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        job_id = (
            await connection.execute(
                insert(Job)
                .values(
                    org_id=tenant.org,
                    workspace_id=tenant.ids["ws"],
                    owner_user_id=tenant.ids["mia"],
                    agent_profile_id=tenant.ids["profile"],
                    name="Curator",
                    schedule={"kind": "cron", "expr": "0 3 * * 1"},
                    mode="curator",
                    created_by_kind="system",
                )
                .returning(Job.id)
            )
        ).scalar_one()
        other_job = (
            await connection.execute(
                insert(Job)
                .values(
                    org_id=tenant.org,
                    workspace_id=tenant.ids["ws"],
                    owner_user_id=tenant.ids["mia"],
                    agent_profile_id=tenant.ids["profile"],
                    name="Daily",
                    schedule={"kind": "interval", "seconds": 3600},
                    prompt="Go",
                )
                .returning(Job.id)
            )
        ).scalar_one()
        for n, (status, output) in enumerate(
            [
                ("completed", f"Week 1. 2 skills are stale. key {FAKE_KEY}"),
                ("failed", None),
                ("running", None),
                ("completed", "word " * (MAX_REPORT_CHARS // 4)),
            ]
        ):
            await connection.execute(
                insert(JobFire).values(
                    org_id=tenant.org,
                    job_id=job_id,
                    job_version=1,
                    scheduled_for=now - timedelta(days=10 - n),
                    created_at=now - timedelta(days=10 - n),
                    status=status,
                    output=output,
                    error="boom" if status == "failed" else None,
                    finished_at=None if status == "running" else now,
                )
            )
        await connection.execute(
            insert(JobFire).values(
                org_id=tenant.org,
                job_id=other_job,
                job_version=1,
                scheduled_for=now,
                status="completed",
                output="not a curator report",
            )
        )

    response = await client.get(
        f"/orgs/{tenant.org}/curator-reports", headers=await admin(engine, tenant)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [job["id"] for job in body["jobs"]] == [str(job_id)]
    assert body["jobs"][0]["workspace_name"] == "ws"
    reports = body["reports"]
    assert [r["status"] for r in reports] == ["completed", "failed", "completed"]
    assert reports[0]["truncated"] is True
    assert len(reports[0]["output"]) <= MAX_REPORT_CHARS
    assert reports[1]["error"] == "boom"
    assert FAKE_KEY not in reports[2]["output"]
    assert "stale" in reports[2]["output"]

    limited = await client.get(
        f"/orgs/{tenant.org}/curator-reports?limit=1", headers=await admin(engine, tenant)
    )
    assert len(limited.json()["reports"]) == 1


# -- agent profile create --------------------------------------------------------------------


async def test_an_admin_makes_an_agent_profile(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    headers = await admin(engine, tenant)
    response = await client.post(
        f"/orgs/{tenant.org}/agent-profiles",
        headers=headers,
        json={
            "name": "  Researcher ",
            "soul": "You find facts.",
            "workspace_id": str(tenant.ids["ws"]),
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Researcher"
    assert body["soul"] == "You find facts."
    assert body["runtime"] == "claude_agent_sdk"
    assert body["workspace_id"] == str(tenant.ids["ws"])
    assert body["editable"] is True
    assert "core" in body["toolsets"]
    async with engine.connect() as connection:
        audit = (
            await connection.execute(
                select(AuditEntry.action, AuditEntry.details).where(
                    AuditEntry.org_id == tenant.org, AuditEntry.target_id == body["id"]
                )
            )
        ).one()
    assert audit.action == "profile.created"
    assert audit.details["soul"] is True
    assert "You find" not in str(audit.details)

    # The new profile is in /me for a member of the workspace.
    me = (await client.get("/me", headers=mia(tenant))).json()
    names = {p["name"] for o in me["orgs"] for p in o["agent_profiles"]}
    assert "Researcher" in names


async def test_the_profile_create_checks_the_values(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    headers = await admin(engine, tenant)
    url = f"/orgs/{tenant.org}/agent-profiles"
    member = await client.post(url, headers=mia(tenant), json={"name": "x"})
    assert member.status_code == 403
    secret = await client.post(url, headers=headers, json={"name": "x", "soul": f"use {FAKE_KEY}"})
    assert secret.status_code == 422
    model = await client.post(url, headers=headers, json={"name": "x", "model": "no-such-model"})
    assert model.status_code == 422
    fake = await client.post(url, headers=headers, json={"name": "x", "runtime": "fake"})
    assert fake.status_code == 422
    blank = await client.post(url, headers=headers, json={"name": "   "})
    assert blank.status_code == 422
    extra = await client.post(url, headers=headers, json={"name": "x", "toolsets": []})
    assert extra.status_code == 422

    other = await add_tenant(engine)
    try:
        foreign = await client.post(
            url, headers=headers, json={"name": "x", "workspace_id": str(other.ids["ws"])}
        )
        assert foreign.status_code == 422
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL sama.audit_purge = 'on'"))
            await connection.execute(delete(Org).where(Org.id == other.org))

    openai = await client.post(
        url, headers=headers, json={"name": "o", "runtime": "openai_agents_sdk"}
    )
    assert openai.status_code == 201, openai.text
    assert openai.json()["runtime"] == "openai_agents_sdk"


async def test_the_profile_create_uses_the_plan_budget_and_the_cap(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    async with engine.begin() as connection:
        await connection.execute(update(Org).where(Org.id == tenant.org).values(plan="free"))
    headers = await admin(engine, tenant)
    url = f"/orgs/{tenant.org}/agent-profiles"
    first = await client.post(url, headers=headers, json={"name": "one"})
    assert first.status_code == 201
    async with engine.connect() as connection:
        budget = (
            await connection.execute(
                select(AgentProfile.budget).where(AgentProfile.id == uuid.UUID(first.json()["id"]))
            )
        ).scalar_one()
    assert budget["max_cost_usd"] == "1.00"

    # The tenant has one profile, and this test made one more.
    async with engine.begin() as connection:
        for n in range(MAX_PROFILES_PER_ORG - 2):
            await connection.execute(
                insert(AgentProfile).values(
                    org_id=tenant.org, name=f"filler {n}", model="claude-sonnet-5-5"
                )
            )
    full = await client.post(url, headers=headers, json={"name": "one more"})
    assert full.status_code == 409
    assert "at most" in full.json()["detail"]
