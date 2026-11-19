"""Tests of the HTTP API with a real database and the fake runtime (Section 11.4)."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import SamaApi
from sama.db.models import AgentProfile, AuditEntry, Membership, Org, Run, Session, User, Workspace
from sama.runtime.contract import RunCompleted, TextDelta
from sama.runtime.fake import FakeScript
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.clarify import ClarifyBroker, ClarifyQuestion
from tests.api.conftest import bearer
from tests.orchestrator.conftest import Tenant, add_tenant
from tests.orchestrator.test_worker import Harness, approval_script

pytestmark = pytest.mark.usefixtures("tenant")


def mia(tenant: Tenant) -> dict[str, str]:
    return bearer(tenant.ids["mia_auth"])


async def add_user(
    engine: AsyncEngine, tenant: Tenant, *, workspace_member: bool, role: str = "member"
) -> uuid.UUID:
    """Adds a user to the org of ``tenant`` and returns the Supabase Auth id."""
    user_id, auth_id = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(User).values(
                id=user_id, org_id=tenant.org, auth_user_id=auth_id, display_name="zed"
            )
        )
        await connection.execute(
            insert(Membership).values(org_id=tenant.org, user_id=user_id, role=role)
        )
        if workspace_member:
            await connection.execute(
                insert(Membership).values(
                    org_id=tenant.org, user_id=user_id, workspace_id=tenant.ids["ws"], role="member"
                )
            )
    return auth_id


def sse(body: str) -> list[tuple[str, dict[str, Any], str | None]]:
    events = []
    for block in body.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith(":") or ":" not in line:
                continue
            name, _, value = line.partition(":")
            fields[name] = value.lstrip()
        if "data" in fields:
            events.append(
                (fields.get("event", "message"), json.loads(fields["data"]), fields.get("id"))
            )
    return events


# -- basics ----------------------------------------------------------------------------------


async def test_the_page_and_the_config_are_public(client: httpx.AsyncClient) -> None:
    page = await client.get("/")
    assert page.status_code == 200
    assert "/static/app.js" in page.text
    csp = page.headers["content-security-policy"]
    assert "connect-src 'self' https://project.example.test" in csp
    assert "script-src" not in csp or "'unsafe-inline'" not in csp
    assert (await client.get("/static/app.js")).status_code == 200
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert (await client.get("/config")).json() == {
        "supabase_url": "https://project.example.test",
        "supabase_anon_key": "anon-key",
    }


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer "},
        bearer(uuid.uuid4(), expires_in=-60),
        bearer(uuid.uuid4(), is_anonymous=True),
    ],
)
async def test_the_api_needs_a_valid_token(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.get("/sessions", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


async def test_me_lists_what_the_user_can_see(client: httpx.AsyncClient, tenant: Tenant) -> None:
    orgs = (await client.get("/me", headers=mia(tenant))).json()["orgs"]
    assert [o["id"] for o in orgs] == [str(tenant.org)]
    assert [w["id"] for w in orgs[0]["workspaces"]] == [str(tenant.ids["ws"])]
    assert [p["id"] for p in orgs[0]["agent_profiles"]] == [str(tenant.ids["profile"])]
    assert orgs[0]["user_id"] == str(tenant.ids["mia"])
    stranger = (await client.get("/me", headers=bearer(uuid.uuid4()))).json()
    assert stranger == {"orgs": []}


# -- sessions and isolation ------------------------------------------------------------------


async def test_sessions_show_only_what_the_user_can_read(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    listed = {s["id"] for s in (await client.get("/sessions", headers=mia(tenant))).json()}
    expected = {str(tenant.ids[s]) for s in ("s1", "s2", "s3", "mine")}
    assert expected <= listed
    other = await add_tenant(engine)
    assert not {str(other.ids[s]) for s in ("s1", "mine")} & listed

    # An org member who is not in the workspace sees no session of the workspace.
    zed = await add_user(engine, tenant, workspace_member=False)
    assert (await client.get("/sessions", headers=bearer(zed))).json() == []
    # A workspace member does not see the private session of mia.
    ann = await add_user(engine, tenant, workspace_member=True)
    seen = {s["id"] for s in (await client.get("/sessions", headers=bearer(ann))).json()}
    assert str(tenant.ids["s1"]) in seen
    assert str(tenant.ids["mine"]) not in seen
    for path in ("", "/messages", "/approvals", "/questions", "/events"):
        response = await client.get(f"/sessions/{tenant.ids['mine']}{path}", headers=bearer(ann))
        assert response.status_code == 404, path


async def test_another_org_gets_404_everywhere(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    harness = Harness(engine)
    harness.runtime.add_script(approval_script())
    run_id = await harness.send(tenant, "s1", "clean up")
    assert await harness.worker.run_once()
    approval = await client.get(f"/sessions/{tenant.ids['s1']}/approvals", headers=mia(tenant))
    approval_id = approval.json()[0]["id"]

    other = await add_tenant(engine)
    eve = bearer(other.ids["mia_auth"])
    s1 = tenant.ids["s1"]
    for method, path, body in [
        ("GET", f"/sessions/{s1}", None),
        ("GET", f"/sessions/{s1}/messages", None),
        ("GET", f"/sessions/{s1}/events", None),
        ("GET", f"/sessions/{s1}/approvals", None),
        ("POST", f"/sessions/{s1}/messages", {"text": "hi"}),
        ("POST", f"/runs/{run_id}/cancel", None),
        ("POST", f"/approvals/{approval_id}/approve", {}),
        ("POST", f"/approvals/{approval_id}/deny", {}),
        (
            "POST",
            "/sessions",
            {"workspace_id": str(tenant.ids["ws"]), "agent_profile_id": str(tenant.ids["profile"])},
        ),
    ]:
        response = await client.request(method, path, headers=eve, json=body)
        assert response.status_code == 404, (method, path)
    assert await harness.status_is(run_id, "paused")


async def test_a_user_creates_a_session(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    body = {
        "workspace_id": str(tenant.ids["ws"]),
        "agent_profile_id": str(tenant.ids["profile"]),
        "audience": "private",
        "title": "Plan the trip",
    }
    response = await client.post("/sessions", headers=mia(tenant), json=body)
    assert response.status_code == 201
    created = response.json()
    assert (created["title"], created["audience"], created["status"]) == (
        "Plan the trip",
        "private",
        "active",
    )
    detail = (await client.get(f"/sessions/{created['id']}", headers=mia(tenant))).json()
    assert (detail["runs"], detail["last_event_id"]) == ([], 0)
    async with engine.connect() as connection:
        actions = (
            await connection.execute(
                select(AuditEntry.action, AuditEntry.actor_user_id).where(
                    AuditEntry.target_id == created["id"]
                )
            )
        ).all()
    assert actions == [("session.created", tenant.ids["mia"])]

    # A profile of another org or another workspace is refused.
    other = await add_tenant(engine)
    foreign = {**body, "agent_profile_id": str(other.ids["profile"])}
    assert (await client.post("/sessions", headers=mia(tenant), json=foreign)).status_code == 404
    elsewhere = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(Workspace).values(id=elsewhere, org_id=tenant.org, name="b", slug="b")
        )
        profile = uuid.uuid4()
        await connection.execute(
            insert(AgentProfile).values(
                id=profile,
                org_id=tenant.org,
                workspace_id=elsewhere,
                name="b",
                runtime="fake",
                model="m",
                soul="s",
            )
        )
    misplaced = {**body, "agent_profile_id": str(profile)}
    assert (await client.post("/sessions", headers=mia(tenant), json=misplaced)).status_code == 404
    hidden = {**body, "workspace_id": str(elsewhere)}
    assert (await client.post("/sessions", headers=mia(tenant), json=hidden)).status_code == 404


# -- messages, runs, and the event stream ------------------------------------------------------


async def test_a_message_runs_and_streams(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    harness = Harness(engine)
    harness.runtime.add_script(
        FakeScript(
            events=(TextDelta(text="hel"), TextDelta(text="lo"), RunCompleted(output="hello"))
        )
    )
    s1 = tenant.ids["s1"]
    start = (await client.get(f"/sessions/{s1}", headers=mia(tenant))).json()["last_event_id"]
    response = await client.post(
        f"/sessions/{s1}/messages", headers=mia(tenant), json={"text": "hi"}
    )
    assert response.status_code == 202
    run_id = uuid.UUID(response.json()["run_id"])
    assert await harness.worker.run_once()

    messages = (await client.get(f"/sessions/{s1}/messages", headers=mia(tenant))).json()
    assert [(m["role"], m["text"]) for m in messages] == [("user", "hi"), ("assistant", "hello")]
    later = await client.get(
        f"/sessions/{s1}/messages", headers=mia(tenant), params={"after": messages[0]["seq"]}
    )
    assert [m["text"] for m in later.json()] == ["hello"]

    stream = await client.get(
        f"/sessions/{s1}/events", headers=mia(tenant), params={"after": start}
    )
    assert stream.headers["content-type"].startswith("text/event-stream")
    events = sse(stream.text)
    run_events = [e for e in events if e[0] == "run_event"]
    assert [e[1]["type"] for e in run_events] == ["text_delta", "text_delta", "run_completed"]
    assert {e[1]["run_id"] for e in run_events} == {str(run_id)}
    ids = [int(e[2] or 0) for e in run_events]
    assert ids == sorted(ids)
    assert ids[0] > start
    assert ": ping" in stream.text

    # Last-Event-ID wins over ?after=, and a stream without a cursor starts at the end.
    resumed = await client.get(
        f"/sessions/{s1}/events",
        headers={**mia(tenant), "Last-Event-ID": str(ids[1])},
        params={"after": 0},
    )
    assert [e[1]["type"] for e in sse(resumed.text) if e[0] == "run_event"] == ["run_completed"]
    fresh = await client.get(f"/sessions/{s1}/events", headers=mia(tenant))
    assert [e for e in sse(fresh.text) if e[0] == "run_event"] == []
    bad = await client.get(f"/sessions/{s1}/events", headers={**mia(tenant), "Last-Event-ID": "x"})
    assert bad.status_code == 400


async def test_the_stream_reports_run_status_changes(
    api: SamaApi, client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    harness = Harness(engine)
    s2 = tenant.ids["s2"]
    run_id = await harness.send(tenant, "s2", "hi")

    async def finish() -> None:
        await asyncio.sleep(0.1)
        assert await harness.worker.run_once()

    stream, _ = await asyncio.gather(
        client.get(f"/sessions/{s2}/events", headers=mia(tenant)), finish()
    )
    events = sse(stream.text)
    assert events[0][0] == "runs"
    assert [(r["run_id"], r["status"]) for r in events[0][1]["runs"]] == [(str(run_id), "queued")]
    statuses = [e[1]["status"] for e in events if e[0] == "run"]
    assert statuses[-1] == "completed"
    assert all(e[1]["run_id"] == str(run_id) for e in events if e[0] == "run")


async def test_a_new_stream_shows_a_run_that_ended_while_away(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    harness = Harness(engine)
    run_id = await harness.send(tenant, "s2", "hi")
    s2 = tenant.ids["s2"]
    first = sse((await client.get(f"/sessions/{s2}/events", headers=mia(tenant))).text)
    assert first[0][1]["runs"][0]["status"] == "queued"
    # The run ends while no stream is open. A queued cancel writes no run event.
    assert await harness.queue.cancel(org_id=tenant.org, run_id=run_id) == "cancelled"
    again = sse((await client.get(f"/sessions/{s2}/events", headers=mia(tenant))).text)
    assert again[0][0] == "runs"
    assert [(r["run_id"], r["status"]) for r in again[0][1]["runs"]] == [(str(run_id), "cancelled")]


async def test_the_stream_follows_an_open_run_behind_many_newer_runs(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Org).where(Org.id == tenant.org).values(limits={"user_messages_per_minute": 50})
        )
    harness = Harness(engine)
    oldest = await harness.send(tenant, "s2", "first")
    for number in range(21):
        await harness.send(tenant, "s2", f"later {number}")
    s2 = tenant.ids["s2"]

    async def cancel() -> None:
        await asyncio.sleep(0.1)
        response = await client.post(f"/runs/{oldest}/cancel", headers=mia(tenant))
        assert response.status_code == 200

    stream, _ = await asyncio.gather(
        client.get(f"/sessions/{s2}/events", headers=mia(tenant)), cancel()
    )
    events = sse(stream.text)
    assert str(oldest) in {r["run_id"] for r in events[0][1]["runs"]}
    assert ("run", str(oldest), "cancelled") in {
        (e[0], e[1].get("run_id"), e[1].get("status")) for e in events
    }


async def test_a_stream_ends_when_the_token_expires(
    api: SamaApi, engine: AsyncEngine, tenant: Tenant
) -> None:
    long = SamaApi(
        engine,
        verifier=api._verifier,
        settings=api._settings.model_copy(update={"max_stream_s": 30}),
    )
    transport = httpx.ASGITransport(app=long.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        # The verifier accepts a token up to 10 seconds after exp. The stream does not.
        headers = bearer(tenant.ids["mia_auth"], expires_in=-5)
        response = await asyncio.wait_for(
            client.get(f"/sessions/{tenant.ids['s1']}/events", headers=headers), timeout=5
        )
    assert response.status_code == 200
    assert [e for e in sse(response.text) if e[0] == "run_event"] == []


async def test_too_many_streams_get_429(api: SamaApi, engine: AsyncEngine, tenant: Tenant) -> None:
    capped = SamaApi(
        engine,
        verifier=api._verifier,
        settings=api._settings.model_copy(update={"max_streams_per_user": 1}),
    )
    transport = httpx.ASGITransport(app=capped.app())
    path = f"/sessions/{tenant.ids['s1']}/events"
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        first = asyncio.create_task(client.get(path, headers=mia(tenant)))
        await asyncio.sleep(0.1)
        second = await client.get(path, headers=mia(tenant))
        assert second.status_code == 429
        assert second.headers["retry-after"] == "5"
        assert (await first).status_code == 200
        third = await client.get(path, headers=mia(tenant))
        assert third.status_code == 200, "the first stream gave its slot back"


async def test_a_message_too_long_is_refused(client: httpx.AsyncClient, tenant: Tenant) -> None:
    s1 = tenant.ids["s1"]
    response = await client.post(
        f"/sessions/{s1}/messages", headers=mia(tenant), json={"text": "x" * 32_001}
    )
    assert response.status_code == 422


async def test_too_many_messages_get_429(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Org).where(Org.id == tenant.org).values(limits={"user_messages_per_minute": 2})
        )
    s1 = tenant.ids["s1"]
    for _ in range(2):
        response = await client.post(
            f"/sessions/{s1}/messages", headers=mia(tenant), json={"text": "hi"}
        )
        assert response.status_code == 202
    response = await client.post(
        f"/sessions/{s1}/messages", headers=mia(tenant), json={"text": "hi"}
    )
    assert response.status_code == 429
    assert 1 <= int(response.headers["retry-after"]) <= 60
    assert "you sent 2 messages" in response.json()["detail"]


async def test_a_closed_session_refuses_messages(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Session).where(Session.id == tenant.ids["s3"]).values(status="archived")
        )
    response = await client.post(
        f"/sessions/{tenant.ids['s3']}/messages", headers=mia(tenant), json={"text": "hi"}
    )
    assert response.status_code == 409


async def test_a_user_cancels_a_queued_run(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    s1 = tenant.ids["s1"]
    response = await client.post(
        f"/sessions/{s1}/messages", headers=mia(tenant), json={"text": "a"}
    )
    run_id = response.json()["run_id"]
    cancelled = await client.post(f"/runs/{run_id}/cancel", headers=mia(tenant))
    assert cancelled.json() == {"status": "cancelled"}
    assert (
        await client.post(f"/runs/{uuid.uuid4()}/cancel", headers=mia(tenant))
    ).status_code == 404
    async with engine.connect() as connection:
        status = (await connection.execute(select(Run.status).where(Run.id == run_id))).scalar()
    assert status == "cancelled"


# -- approvals ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("action", "output"), [("approve", "deleted"), ("deny", "kept")])
async def test_a_user_decides_an_approval(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant, action: str, output: str
) -> None:
    harness = Harness(engine)
    harness.runtime.add_script(approval_script())
    s1 = tenant.ids["s1"]
    run_id = await harness.send(tenant, "s1", "clean up")
    assert await harness.worker.run_once()
    pending = (await client.get(f"/sessions/{s1}/approvals", headers=mia(tenant))).json()
    assert [(p["tool_name"], p["run_id"]) for p in pending] == [("Bash", str(run_id))]
    assert pending[0]["arguments"] == {"command": "rm -rf build"}

    decided = await client.post(
        f"/approvals/{pending[0]['id']}/{action}", headers=mia(tenant), json={"reason": "ok"}
    )
    assert decided.status_code == 200
    assert decided.json() == {
        "status": "approved" if action == "approve" else "rejected",
        "resumed": True,
    }
    assert await harness.status_is(run_id, "queued"), "the API releases the run at once"
    assert await harness.worker.run_once()
    assert (await harness.run(run_id)).status == "completed"
    messages = (await client.get(f"/sessions/{s1}/messages", headers=mia(tenant))).json()
    assert messages[-1]["text"] == output
    assert (await client.get(f"/sessions/{s1}/approvals", headers=mia(tenant))).json() == []

    again = await client.post(f"/approvals/{pending[0]['id']}/approve", headers=mia(tenant))
    assert again.status_code == 409


async def test_an_org_member_outside_the_workspace_cannot_decide(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    harness = Harness(engine)
    harness.runtime.add_script(approval_script())
    run_id = await harness.send(tenant, "s1", "clean up")
    assert await harness.worker.run_once()
    pending = (
        await client.get(f"/sessions/{tenant.ids['s1']}/approvals", headers=mia(tenant))
    ).json()
    zed = await add_user(engine, tenant, workspace_member=False)
    response = await client.post(f"/approvals/{pending[0]['id']}/approve", headers=bearer(zed))
    assert response.status_code == 404
    assert await harness.status_is(run_id, "paused")
    missing = await client.post(f"/approvals/{uuid.uuid4()}/deny", headers=mia(tenant))
    assert missing.status_code == 404


# -- clarify -----------------------------------------------------------------------------------


async def test_a_user_answers_a_clarify_question(
    client: httpx.AsyncClient, broker: ClarifyBroker, tenant: Tenant
) -> None:
    s1 = tenant.ids["s1"]
    claims = RunTokenClaims(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(s1),
        run_id=str(uuid.uuid4()),
        user_id=str(tenant.ids["mia"]),
        token_id="t1",
        expires_at=0,
    )
    question = ClarifyQuestion(question="Which day?", choices=("Mon", "Tue"))
    ask = asyncio.create_task(broker.ask(claims, [question]))
    for _ in range(100):
        if broker.pending(str(tenant.org), str(s1)):
            break
        await asyncio.sleep(0.01)
    listed = (await client.get(f"/sessions/{s1}/questions", headers=mia(tenant))).json()
    assert [q["questions"][0]["question"] for q in listed] == ["Which day?"]
    request_id = listed[0]["id"]

    # The question belongs to s1, so the path of another session does not answer it.
    wrong = await client.post(
        f"/sessions/{tenant.ids['s2']}/questions/{request_id}/answer",
        headers=mia(tenant),
        json={"answers": ["Tue"]},
    )
    assert wrong.status_code == 404
    bad = await client.post(
        f"/sessions/{s1}/questions/{request_id}/answer",
        headers=mia(tenant),
        json={"answers": ["Tue", "Wed"]},
    )
    assert bad.status_code == 409
    answered = await client.post(
        f"/sessions/{s1}/questions/{request_id}/answer",
        headers=mia(tenant),
        json={"answers": ["Tue"]},
    )
    assert answered.status_code == 204
    assert await ask == ("Tue",)
