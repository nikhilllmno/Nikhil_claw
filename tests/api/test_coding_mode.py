"""The coding mode routes of the API (M7.9, Sections 11.4 and 15.5)."""

from __future__ import annotations

import shutil
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import ApiSettings, JwtVerifier, SamaApi
from sama.coding_mode.service import CodingService
from sama.db.models import RunEvent
from sama.orchestrator.checkpoints import CheckpointService
from sama.orchestrator.queue import RunQueue
from sama.runtime.checkpoints import Checkpoints
from sama.sandbox.base import Sandbox
from sama.sandbox.local import LocalSandbox
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.orchestrator.conftest import Tenant

pytestmark = [
    pytest.mark.usefixtures("tenant"),
    pytest.mark.skipif(shutil.which("git") is None, reason="needs git"),
]

ARGV = (sys.executable, "-m", "sama.runtime.checkpoints")


def mia(tenant: Tenant) -> dict[str, str]:
    return bearer(tenant.ids["mia_auth"])


@pytest.fixture
def work(tmp_path: Path) -> Path:
    box = LocalSandbox(tmp_path / "box")
    Path(box.workdir).mkdir(parents=True)
    (tmp_path / "box" / "home").mkdir()
    return Path(box.workdir)


@pytest.fixture
async def coding_client(engine: AsyncEngine, work: Path) -> AsyncIterator[httpx.AsyncClient]:
    box = LocalSandbox(work.parent)

    async def sandbox(org_id: str, session_id: str) -> Sandbox | None:
        return box

    queue = RunQueue(engine)
    app = SamaApi(
        engine,
        queue=queue,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        settings=ApiSettings(
            supabase_url="https://project.example.test", supabase_anon_key="anon-key"
        ),
        checkpoints=CheckpointService(engine, sandbox, argv=ARGV),
        coding_mode=CodingService(engine, queue),
    ).app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        yield client


async def test_without_the_service_the_writes_answer_409(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    base = f"/sessions/{tenant.ids['mine']}"
    answer = await client.post(f"{base}/repos", headers=mia(tenant), json={"repo": "o/n"})
    assert answer.status_code == 409
    assert (await client.get(f"{base}/diff", headers=mia(tenant))).status_code == 409
    assert (await client.get(f"{base}/commands", headers=mia(tenant))).json() == []


async def test_repos_and_plan_mode(coding_client: httpx.AsyncClient, tenant: Tenant) -> None:
    client = coding_client
    base = f"/sessions/{tenant.ids['mine']}"
    added = await client.post(
        f"{base}/repos", headers=mia(tenant), json={"repo": "https://github.com/octo/app"}
    )
    assert added.status_code == 201
    assert (added.json()["repo"], added.json()["status"]) == ("octo/app", "pending")
    again = await client.post(f"{base}/repos", headers=mia(tenant), json={"repo": "octo/app"})
    assert again.status_code == 409
    bad = await client.post(f"{base}/repos", headers=mia(tenant), json={"repo": "x://y"})
    assert bad.status_code == 422
    extra = await client.post(f"{base}/repos", headers=mia(tenant), json={"repo": "o/n", "x": 1})
    assert extra.status_code == 422

    mode = await client.put(f"{base}/plan-mode", headers=mia(tenant), json={"on": True})
    assert mode.json() == {"plan_mode": True}
    state = (await client.get(f"{base}/coding", headers=mia(tenant))).json()
    assert state["plan_mode"] is True
    assert state["plan"] is None
    assert [r["folder"] for r in state["repos"]] == ["app"]

    gone = await client.delete(f"{base}/repos/{added.json()['id']}", headers=mia(tenant))
    assert gone.status_code == 204
    missing = await client.delete(f"{base}/repos/{uuid.uuid4()}", headers=mia(tenant))
    assert missing.status_code == 404
    stranger = await client.get(f"{base}/coding", headers=bearer(uuid.uuid4()))
    assert stranger.status_code in (403, 404)
    other = await client.get(f"/sessions/{uuid.uuid4()}/coding", headers=mia(tenant))
    assert other.status_code == 404


async def test_the_plan_routes(
    coding_client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    client = coding_client
    session = tenant.ids["mine"]
    base = f"/sessions/{session}"
    started = await client.post(f"{base}/plan", headers=mia(tenant), json={"task": "add a route"})
    assert started.status_code == 202
    assert started.json()["run_id"]
    coding = CodingService(engine, RunQueue(engine))
    plan = await coding.propose(org_id=tenant.org, session_id=session, run_id=None, body="1. a")
    approved = await client.post(
        f"{base}/plans/{plan.id}/approve", headers=mia(tenant), json={"body": "1. a\n2. b"}
    )
    assert approved.status_code == 202
    body = approved.json()
    assert body["plan"]["status"] == "approved"
    assert body["plan"]["edited"] is True
    assert body["queued"] is True
    twice = await client.post(f"{base}/plans/{plan.id}/approve", headers=mia(tenant))
    assert twice.status_code == 409

    second = await coding.propose(org_id=tenant.org, session_id=session, run_id=None, body="1. c")
    silent = await client.post(f"{base}/plans/{second.id}/reject", headers=mia(tenant))
    assert silent.status_code == 200
    assert silent.json()["run_id"] is None
    third = await coding.propose(org_id=tenant.org, session_id=session, run_id=None, body="1. d")
    loud = await client.post(
        f"{base}/plans/{third.id}/reject", headers=mia(tenant), json={"feedback": "smaller"}
    )
    assert loud.status_code == 200
    assert loud.json()["run_id"]
    listed = (await client.get(f"{base}/plans?limit=2", headers=mia(tenant))).json()
    assert [p["version"] for p in listed] == [3, 2]

    asked = await client.post(
        f"{base}/pull-requests", headers=mia(tenant), json={"title": "Add route", "draft": True}
    )
    assert asked.status_code == 202


async def test_diff_commands_and_pull_requests(
    coding_client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant, work: Path
) -> None:
    client = coding_client
    session = tenant.ids["mine"]
    base = f"/sessions/{session}"
    none = await client.get(f"{base}/diff", headers=mia(tenant))
    assert none.status_code == 409
    checkpoints = Checkpoints(str(work))
    (work / "a.py").write_text("x = 1\n")
    checkpoints.snapshot("r1", "Write")
    (work / "a.py").write_text("x = 2\n")
    diff = await client.get(f"{base}/diff?base=oldest", headers=mia(tenant))
    assert diff.status_code == 200
    (changed,) = diff.json()["files"]
    assert (changed["path"], changed["status"], changed["additions"]) == ("a.py", "modified", 1)
    assert "+x = 2" in changed["patch"]
    assert diff.json()["head"] is None
    assert (await client.get(f"{base}/diff?base=0", headers=mia(tenant))).status_code == 422
    assert (await client.get(f"{base}/diff?base=5", headers=mia(tenant))).status_code == 409

    run_id = await RunQueue(engine).enqueue(
        org_id=tenant.org, session_id=session, text="go", user_id=None
    )
    async with engine.begin() as connection:
        for seq, (kind, payload) in enumerate(
            [
                (
                    "tool_call_started",
                    {"call_id": "c1", "name": "Bash", "arguments": {"command": "pytest"}},
                ),
                ("tool_call_finished", {"call_id": "c1", "output_preview": "3 passed in 1s"}),
                (
                    "tool_call_started",
                    {
                        "call_id": "c2",
                        "name": "mcp__sama__pr_create",
                        "arguments": {"repo": "o/n", "title": "T"},
                    },
                ),
                (
                    "tool_call_finished",
                    {"call_id": "c2", "output_preview": "https://github.com/o/n/pull/3"},
                ),
            ]
        ):
            await connection.execute(
                insert(RunEvent).values(
                    org_id=tenant.org,
                    session_id=session,
                    run_id=run_id,
                    seq=500 + seq,
                    type=kind,
                    payload=payload,
                )
            )
    commands = (await client.get(f"{base}/commands?tests=true", headers=mia(tenant))).json()
    assert [(c["command"], c["status"], c["summary"]) for c in commands] == [
        ("pytest", "ok", "3 passed in 1s")
    ]
    prs = (await client.get(f"{base}/pull-requests", headers=mia(tenant))).json()
    assert [(p["status"], p["url"]) for p in prs] == [("opened", "https://github.com/o/n/pull/3")]
    stranger = await client.get(f"{base}/commands", headers=bearer(uuid.uuid4()))
    assert stranger.status_code in (403, 404)
