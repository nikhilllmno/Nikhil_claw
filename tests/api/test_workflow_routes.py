"""The workflow routes of the API (M4.5, Section 12.3)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import ApiSettings, JwtVerifier, SamaApi
from sama.db.models import WorkflowStep
from sama.orchestrator.queue import RunQueue
from sama.tools.clarify import ClarifyBroker
from sama.workflows.engine import WorkflowEngine
from sama.workflows.service import WorkflowService
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.api.test_app import add_user, mia
from tests.orchestrator.conftest import Tenant
from tests.workflows.conftest import FakeTools

READ = """
name: reader
inputs:
  topic: { default: tea }
steps:
  - id: look
    tool: notes_read
    args: { topic: "{{inputs.topic}}" }
"""

WRITE = """
name: writer
steps:
  - id: post
    tool: notes_write
    args: { text: hi }
"""

WAITER = """
name: waiter
steps:
  - id: hold
    wait: { event: go }
  - id: ask
    approval: { prompt: "Go on?" }
"""


def uid(value: Any) -> uuid.UUID:
    return uuid.UUID(str(value))


@pytest.fixture
def tools() -> FakeTools:
    return FakeTools()


@pytest.fixture
def workflows(engine: AsyncEngine, tools: FakeTools) -> WorkflowService:
    return WorkflowService(engine, RunQueue(engine), tools.access)


@pytest.fixture
def runner(engine: AsyncEngine, tools: FakeTools) -> WorkflowEngine:
    return WorkflowEngine(engine, RunQueue(engine), tools, retry_base_s=0)


@pytest.fixture
async def wf(engine: AsyncEngine, workflows: WorkflowService) -> AsyncIterator[httpx.AsyncClient]:
    api = SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        clarify=ClarifyBroker(wait_s=5),
        settings=ApiSettings(supabase_url="https://project.example.test", supabase_anon_key="k"),
        workflows=workflows,
    )
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        yield client


def base(tenant: Tenant) -> str:
    return f"/orgs/{tenant.org}/workflows"


def instances_of(tenant: Tenant) -> str:
    return f"/orgs/{tenant.org}/workflow-instances"


async def create(wf: httpx.AsyncClient, tenant: Tenant, source: str, **body: Any) -> dict[str, Any]:
    response = await wf.post(base(tenant), json={"yaml": source, **body}, headers=mia(tenant))
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


async def approve(wf: httpx.AsyncClient, tenant: Tenant, view: dict[str, Any]) -> dict[str, Any]:
    response = await wf.post(
        f"{base(tenant)}/{view['id']}/approve",
        json={"version": view["version"]},
        headers=mia(tenant),
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


async def start(
    wf: httpx.AsyncClient, tenant: Tenant, view: dict[str, Any], **inputs: Any
) -> dict[str, Any]:
    response = await wf.post(
        f"{base(tenant)}/{view['id']}/start", json={"inputs": inputs}, headers=mia(tenant)
    )
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


# -- the service is off -----------------------------------------------------------------------


async def test_every_workflow_route_answers_409_without_the_service(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    ids = f"{uuid.uuid4()}"
    org = f"/orgs/{tenant.org}"
    routes = [
        ("POST", "/workflows/validate", {"yaml": READ}),
        ("GET", f"{org}/workflows", None),
        ("POST", f"{org}/workflows", {"yaml": READ}),
        ("GET", f"{org}/workflows/{ids}", None),
        ("PUT", f"{org}/workflows/{ids}", {"yaml": READ}),
        ("POST", f"{org}/workflows/{ids}/approve", {"version": 1}),
        ("POST", f"{org}/workflows/{ids}/start", {}),
        ("POST", f"{org}/workflows/{ids}/pause", None),
        ("POST", f"{org}/workflows/{ids}/resume", None),
        ("DELETE", f"{org}/workflows/{ids}", None),
        ("GET", f"{org}/workflow-instances", None),
        ("GET", f"{org}/workflow-instances/{ids}", None),
        ("POST", f"{org}/workflow-instances/{ids}/cancel", None),
        ("POST", f"{org}/workflow-instances/{ids}/signal", {"event": "go"}),
        ("POST", f"{org}/workflow-instances/{ids}/steps/ask/decide", {"approve": True}),
        ("POST", f"{org}/workflow-instances/{ids}/steps/post/resolve", {"action": "fail"}),
    ]
    for method, path, body in routes:
        response = await client.request(method, path, json=body, headers=mia(tenant))
        assert response.status_code == 409, (method, path, response.text)
        assert response.json()["detail"] == "workflows are off in this process"


# -- the user must sign in --------------------------------------------------------------------


async def test_every_workflow_route_needs_a_signed_in_user(
    wf: httpx.AsyncClient, tenant: Tenant
) -> None:
    ids = f"{uuid.uuid4()}"
    org = f"/orgs/{tenant.org}"
    routes = [
        ("POST", "/workflows/validate", {"yaml": READ}),
        ("GET", f"{org}/workflows", None),
        ("POST", f"{org}/workflows", {"yaml": READ}),
        ("GET", f"{org}/workflows/{ids}", None),
        ("POST", f"{org}/workflows/{ids}/start", {}),
        ("GET", f"{org}/workflow-instances", None),
        ("POST", f"{org}/workflow-instances/{ids}/signal", {"event": "go"}),
    ]
    for method, path, body in routes:
        missing = await wf.request(method, path, json=body)
        assert missing.status_code == 401, (method, path)
        forged = await wf.request(
            method, path, json=body, headers={"Authorization": "Bearer not-a-token"}
        )
        assert forged.status_code == 401, (method, path)


async def test_a_user_outside_the_org_cannot_use_the_workflows(
    wf: httpx.AsyncClient, tenant: Tenant
) -> None:
    stranger = bearer(uuid.uuid4())
    assert (await wf.get(base(tenant), headers=stranger)).status_code == 404
    made = await wf.post(base(tenant), json={"yaml": READ}, headers=stranger)
    assert made.status_code == 404
    assert (await wf.get(instances_of(tenant), headers=stranger)).status_code == 404


# -- validate ---------------------------------------------------------------------------------


async def test_validate_shows_the_workflow_without_saving_it(
    wf: httpx.AsyncClient, tenant: Tenant
) -> None:
    response = await wf.post("/workflows/validate", json={"yaml": WRITE}, headers=mia(tenant))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["definition"]["name"] == "writer"
    assert body["needs_approval"] is True
    assert body["approval_reasons"]
    assert len(body["hash"]) == 64
    assert (await wf.get(base(tenant), headers=mia(tenant))).json() == []


async def test_validate_refuses_a_bad_workflow_with_400(
    wf: httpx.AsyncClient, tenant: Tenant
) -> None:
    response = await wf.post(
        "/workflows/validate", json={"yaml": "name: x\nsteps: []\n"}, headers=mia(tenant)
    )
    assert response.status_code == 400
    assert response.json()["detail"]


async def test_the_body_of_a_route_is_checked(wf: httpx.AsyncClient, tenant: Tenant) -> None:
    headers = mia(tenant)
    assert (await wf.post("/workflows/validate", json={}, headers=headers)).status_code == 422
    empty = await wf.post("/workflows/validate", json={"yaml": ""}, headers=headers)
    assert empty.status_code == 422
    big = await wf.post("/workflows/validate", json={"yaml": "x" * 50_001}, headers=headers)
    assert big.status_code == 422
    extra = await wf.post("/workflows/validate", json={"yaml": READ, "more": 1}, headers=headers)
    assert extra.status_code == 422
    bad_id = await wf.post(
        base(tenant), json={"yaml": READ, "workspace_id": "nope"}, headers=headers
    )
    assert bad_id.status_code == 422
    no_version = await wf.post(
        f"{base(tenant)}/{uuid.uuid4()}/approve", json={"version": 0}, headers=headers
    )
    assert no_version.status_code == 422
    no_event = await wf.post(
        f"{instances_of(tenant)}/{uuid.uuid4()}/signal", json={"event": ""}, headers=headers
    )
    assert no_event.status_code == 422
    bad_action = await wf.post(
        f"{instances_of(tenant)}/{uuid.uuid4()}/steps/a/resolve",
        json={"action": "explode"},
        headers=headers,
    )
    assert bad_action.status_code == 422
    unknown = await wf.post(f"{base(tenant)}/{uuid.uuid4()}/explode", headers=headers)
    assert unknown.status_code == 422
    big_limit = await wf.get(f"{instances_of(tenant)}?limit=101", headers=headers)
    assert big_limit.status_code == 422


# -- the life of a workflow -------------------------------------------------------------------


async def test_a_member_makes_approves_and_runs_a_workflow(
    wf: httpx.AsyncClient, tenant: Tenant
) -> None:
    view = await create(wf, tenant, READ)
    assert (view["name"], view["version"], view["needs_approval"]) == ("reader", 1, False)
    assert uid(view["id"])
    listed = await wf.get(base(tenant), headers=mia(tenant))
    assert [item["id"] for item in listed.json()] == [view["id"]]
    shown = await wf.get(f"{base(tenant)}/{view['id']}", headers=mia(tenant))
    assert shown.status_code == 200
    assert shown.json()["id"] == view["id"]

    instance = await start(wf, tenant, view, topic="jam")
    assert instance["status"] == "running"
    fetched = await wf.get(f"{instances_of(tenant)}/{instance['id']}", headers=mia(tenant))
    assert fetched.status_code == 200, fetched.text
    body = fetched.json()
    assert body["workflow_id"] == view["id"]
    assert body["inputs"] == {"topic": "jam"}
    listed_instances = await wf.get(instances_of(tenant), headers=mia(tenant))
    assert [item["id"] for item in listed_instances.json()] == [instance["id"]]
    for_workflow = await wf.get(
        f"{instances_of(tenant)}?workflow_id={view['id']}&limit=1", headers=mia(tenant)
    )
    assert [item["id"] for item in for_workflow.json()] == [instance["id"]]
    other = await wf.get(f"{instances_of(tenant)}?workflow_id={uuid.uuid4()}", headers=mia(tenant))
    assert other.json() == []


async def test_a_workflow_that_writes_waits_for_an_approval(
    wf: httpx.AsyncClient, tenant: Tenant
) -> None:
    view = await create(wf, tenant, WRITE)
    assert view["needs_approval"] is True
    assert view["status"] == "pending_approval"
    refused = await wf.post(f"{base(tenant)}/{view['id']}/start", json={}, headers=mia(tenant))
    assert refused.status_code == 400
    stale = await wf.post(
        f"{base(tenant)}/{view['id']}/approve", json={"version": 2}, headers=mia(tenant)
    )
    assert stale.status_code == 400
    approved = await approve(wf, tenant, view)
    assert approved["status"] == "active"
    instance = await start(wf, tenant, approved)
    assert instance["workflow_id"] == view["id"]


async def test_an_update_gives_a_new_version(wf: httpx.AsyncClient, tenant: Tenant) -> None:
    view = await create(wf, tenant, READ)
    changed = await wf.put(
        f"{base(tenant)}/{view['id']}",
        json={"yaml": READ.replace("tea", "jam")},
        headers=mia(tenant),
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["version"] == 2
    same = await wf.put(
        f"{base(tenant)}/{view['id']}",
        json={"yaml": READ.replace("tea", "jam")},
        headers=mia(tenant),
    )
    assert same.json()["version"] == 2
    bad = await wf.put(
        f"{base(tenant)}/{view['id']}", json={"yaml": "name: x\nsteps: []\n"}, headers=mia(tenant)
    )
    assert bad.status_code == 400


async def test_a_workflow_is_paused_resumed_and_removed(
    wf: httpx.AsyncClient, tenant: Tenant
) -> None:
    view = await create(wf, tenant, READ)
    path = f"{base(tenant)}/{view['id']}"
    paused = await wf.post(f"{path}/pause", headers=mia(tenant))
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "paused"
    resumed = await wf.post(f"{path}/resume", headers=mia(tenant))
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "active"
    removed = await wf.delete(path, headers=mia(tenant))
    assert removed.status_code == 204
    assert removed.content == b""
    assert (await wf.get(path, headers=mia(tenant))).status_code == 404
    assert (await wf.get(base(tenant), headers=mia(tenant))).json() == []


async def test_a_bad_workflow_is_refused_with_400(wf: httpx.AsyncClient, tenant: Tenant) -> None:
    response = await wf.post(
        base(tenant), json={"yaml": "name: x\nsteps: []\n"}, headers=mia(tenant)
    )
    assert response.status_code == 400
    await create(wf, tenant, READ)
    twice = await wf.post(base(tenant), json={"yaml": READ}, headers=mia(tenant))
    assert twice.status_code == 400


async def test_a_workflow_in_a_workspace_that_the_user_cannot_see_is_refused(
    wf: httpx.AsyncClient, tenant: Tenant
) -> None:
    response = await wf.post(
        base(tenant), json={"yaml": READ, "workspace_id": str(uuid.uuid4())}, headers=mia(tenant)
    )
    assert response.status_code == 400
    assert "workspace" in response.json()["detail"]


async def test_a_workflow_is_made_in_a_named_workspace_and_profile(
    wf: httpx.AsyncClient, tenant: Tenant
) -> None:
    view = await create(
        wf,
        tenant,
        READ,
        workspace_id=str(tenant.ids["ws"]),
        agent_profile_id=str(tenant.ids["profile"]),
    )
    assert view["workspace_id"] == str(tenant.ids["ws"])


# -- the instance -----------------------------------------------------------------------------


async def test_an_instance_is_cancelled(wf: httpx.AsyncClient, tenant: Tenant) -> None:
    view = await create(wf, tenant, WAITER)
    instance = await start(wf, tenant, view)
    path = f"{instances_of(tenant)}/{instance['id']}"
    cancelled = await wf.post(f"{path}/cancel", headers=mia(tenant))
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert (await wf.get(path, headers=mia(tenant))).json()["status"] == "cancelled"


async def test_a_signal_ends_a_wait_step(
    wf: httpx.AsyncClient, tenant: Tenant, runner: WorkflowEngine
) -> None:
    view = await create(wf, tenant, WAITER)
    instance = await start(wf, tenant, view)
    await runner.run_once()
    path = f"{instances_of(tenant)}/{instance['id']}/signal"
    other = await wf.post(path, json={"event": "other"}, headers=mia(tenant))
    assert (other.status_code, other.json()) == (200, {"ended": 0})
    sent = await wf.post(path, json={"event": "go", "data": {"ok": 1}}, headers=mia(tenant))
    assert (sent.status_code, sent.json()) == (200, {"ended": 1})
    shown = await wf.get(f"{instances_of(tenant)}/{instance['id']}", headers=mia(tenant))
    [hold] = shown.json()["steps"]
    assert (hold["status"], hold["output"]) == ("completed", {"ok": 1})


async def test_an_approval_step_is_decided(
    wf: httpx.AsyncClient, tenant: Tenant, runner: WorkflowEngine
) -> None:
    view = await create(wf, tenant, WAITER)
    instance = await start(wf, tenant, view)
    path = f"{instances_of(tenant)}/{instance['id']}"
    await runner.run_once()
    await wf.post(f"{path}/signal", json={"event": "go"}, headers=mia(tenant))
    await runner.run_once()
    await runner.run_once()
    early = await wf.post(f"{path}/steps/hold/decide", json={"approve": True}, headers=mia(tenant))
    assert early.status_code == 400
    decided = await wf.post(f"{path}/steps/ask/decide", json={"approve": True}, headers=mia(tenant))
    assert decided.status_code == 200, decided.text
    steps = {step["step_id"]: step["status"] for step in decided.json()["steps"]}
    assert steps["ask"] == "completed"


async def test_a_tool_step_with_an_unknown_result_is_resolved(
    engine: AsyncEngine, wf: httpx.AsyncClient, tenant: Tenant, runner: WorkflowEngine
) -> None:
    view = await create(wf, tenant, READ)
    instance = await start(wf, tenant, view)
    await runner.run_once()
    await runner.run_once()
    path = f"{instances_of(tenant)}/{instance['id']}/steps/look/resolve"
    not_waiting = await wf.post(path, json={"action": "done"}, headers=mia(tenant))
    assert not_waiting.status_code == 400
    assert "does not wait for a person" in not_waiting.json()["detail"]
    async with engine.begin() as connection:
        await connection.execute(
            update(WorkflowStep)
            .where(WorkflowStep.instance_id == uid(instance["id"]))
            .values(status="waiting_human")
        )
    resolved = await wf.post(
        path, json={"action": "done", "output": {"saved": True}}, headers=mia(tenant)
    )
    assert resolved.status_code == 200, resolved.text
    [step] = resolved.json()["steps"]
    assert (step["status"], step["output"]) == ("completed", {"saved": True})


# -- other users ------------------------------------------------------------------------------


async def test_a_user_sees_only_the_workflows_that_the_user_owns(
    engine: AsyncEngine, wf: httpx.AsyncClient, tenant: Tenant
) -> None:
    view = await create(wf, tenant, READ)
    instance = await start(wf, tenant, view)
    zed = bearer(await add_user(engine, tenant, workspace_member=True))
    path = f"{base(tenant)}/{view['id']}"
    assert (await wf.get(base(tenant), headers=zed)).json() == []
    assert (await wf.get(instances_of(tenant), headers=zed)).json() == []
    for method, url, body in (
        ("GET", path, None),
        ("PUT", path, {"yaml": READ.replace("tea", "jam")}),
        ("POST", f"{path}/approve", {"version": 1}),
        ("POST", f"{path}/start", {}),
        ("POST", f"{path}/pause", None),
        ("POST", f"{path}/resume", None),
        ("DELETE", path, None),
        ("GET", f"{instances_of(tenant)}/{instance['id']}", None),
        ("POST", f"{instances_of(tenant)}/{instance['id']}/cancel", None),
        ("POST", f"{instances_of(tenant)}/{instance['id']}/signal", {"event": "go"}),
        (
            "POST",
            f"{instances_of(tenant)}/{instance['id']}/steps/look/decide",
            {"approve": True},
        ),
        (
            "POST",
            f"{instances_of(tenant)}/{instance['id']}/steps/look/resolve",
            {"action": "fail"},
        ),
    ):
        response = await wf.request(method, url, json=body, headers=zed)
        assert response.status_code in (403, 404), (method, url, response.status_code)
    # Nothing changed for the owner.
    assert (await wf.get(path, headers=mia(tenant))).json()["version"] == 1


async def test_two_users_can_use_the_same_workflow_name(
    engine: AsyncEngine, wf: httpx.AsyncClient, tenant: Tenant
) -> None:
    await create(wf, tenant, READ)
    zed = bearer(await add_user(engine, tenant, workspace_member=True))
    made = await wf.post(
        base(tenant), json={"yaml": READ, "workspace_id": str(tenant.ids["ws"])}, headers=zed
    )
    assert made.status_code == 201, made.text
    assert len((await wf.get(base(tenant), headers=zed)).json()) == 1


async def test_a_workflow_that_does_not_exist_answers_404(
    wf: httpx.AsyncClient, tenant: Tenant
) -> None:
    missing = uuid.uuid4()
    headers = mia(tenant)
    path = f"{base(tenant)}/{missing}"
    assert (await wf.get(path, headers=headers)).status_code == 404
    assert (await wf.post(f"{path}/pause", headers=headers)).status_code == 404
    assert (await wf.delete(path, headers=headers)).status_code == 404
    assert (await wf.get(f"{instances_of(tenant)}/{missing}", headers=headers)).status_code == 404
