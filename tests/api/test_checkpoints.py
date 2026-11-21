"""The checkpoint routes of the API (M6.4): list, undo, and rollback (Sections 11.4 and
15.3)."""

from __future__ import annotations

import shutil
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import ApiSettings, JwtVerifier, SamaApi
from sama.orchestrator.checkpoints import BUSY, CheckpointService
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
async def checkpoint_client(engine: AsyncEngine, work: Path) -> AsyncIterator[httpx.AsyncClient]:
    box = LocalSandbox(work.parent)

    async def sandbox(org_id: str, session_id: str) -> Sandbox | None:
        return box

    app = SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        settings=ApiSettings(
            supabase_url="https://project.example.test", supabase_anon_key="anon-key"
        ),
        checkpoints=CheckpointService(engine, sandbox, argv=ARGV),
    ).app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        yield client


async def test_the_routes_without_the_service_answer_409(
    client: httpx.AsyncClient, tenant: Tenant
) -> None:
    base = f"/sessions/{tenant.ids['mine']}"
    assert (await client.get(f"{base}/checkpoints", headers=mia(tenant))).status_code == 409
    assert (await client.post(f"{base}/undo", headers=mia(tenant))).status_code == 409
    rollback = await client.post(f"{base}/rollback", headers=mia(tenant), json={"number": 1})
    assert rollback.status_code == 409


async def test_list_undo_and_rollback(
    checkpoint_client: httpx.AsyncClient, tenant: Tenant, work: Path
) -> None:
    client = checkpoint_client
    base = f"/sessions/{tenant.ids['mine']}"
    empty = await client.get(f"{base}/checkpoints", headers=mia(tenant))
    assert (empty.status_code, empty.json()) == (200, [])
    nothing = await client.post(f"{base}/undo", headers=mia(tenant))
    assert nothing.status_code == 409

    checkpoints = Checkpoints(str(work))
    (work / "a.txt").write_text("one")
    checkpoints.snapshot("r1", "Write")
    (work / "a.txt").write_text("two")

    listed = await client.get(f"{base}/checkpoints", headers=mia(tenant))
    assert listed.status_code == 200
    (item,) = listed.json()
    assert (item["number"], item["tool"], item["kind"]) == (1, "Write", "tool")

    undone = await client.post(f"{base}/undo", headers=mia(tenant))
    assert undone.status_code == 200
    assert (undone.json()["files"], undone.json()["deleted"]) == (1, 0)
    assert undone.json()["text"].endswith("To go back, send /rollback 1.")
    assert (work / "a.txt").read_text() == "one"

    rolled = await client.post(f"{base}/rollback", headers=mia(tenant), json={"number": 1})
    assert rolled.status_code == 200
    assert (work / "a.txt").read_text() == "two"
    missing = await client.post(f"{base}/rollback", headers=mia(tenant), json={"number": 9})
    assert (missing.status_code, missing.json()["detail"]) == (409, "There is no checkpoint 9.")


async def test_the_routes_check_the_body_the_access_and_an_open_run(
    checkpoint_client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    client = checkpoint_client
    session = tenant.ids["mine"]
    base = f"/sessions/{session}"
    for body in ({}, {"number": 0}, {"number": "x"}):
        response = await client.post(f"{base}/rollback", headers=mia(tenant), json=body)
        assert response.status_code == 422
    stranger = await client.post(f"{base}/undo", headers=bearer(uuid.uuid4()))
    assert stranger.status_code in (403, 404)
    await RunQueue(engine).enqueue(org_id=tenant.org, session_id=session, text="hi", user_id=None)
    busy = await client.post(f"{base}/undo", headers=mia(tenant))
    assert (busy.status_code, busy.json()["detail"]) == (409, BUSY)


async def test_a_rollback_with_a_commit_checks_the_checkpoint(
    checkpoint_client: httpx.AsyncClient, tenant: Tenant, work: Path
) -> None:
    """Coding mode (Section 15.5): a restore names the checkpoint by its commit, so a list
    that changed in the meantime does not restore another checkpoint."""
    client = checkpoint_client
    base = f"/sessions/{tenant.ids['mine']}"
    checkpoints = Checkpoints(str(work))
    (work / "a.txt").write_text("one")
    checkpoints.snapshot("r1", "Write")
    (work / "a.txt").write_text("two")
    (item,) = (await client.get(f"{base}/checkpoints", headers=mia(tenant))).json()
    assert len(item["commit"]) == 40
    checkpoints.snapshot("r2", "Edit")
    stale = await client.post(
        f"{base}/rollback", headers=mia(tenant), json={"number": 1, "commit": item["commit"]}
    )
    assert stale.status_code == 409
    assert "changed" in stale.json()["detail"]
    assert (work / "a.txt").read_text() == "two"
    fresh = await client.post(
        f"{base}/rollback", headers=mia(tenant), json={"number": 2, "commit": item["commit"]}
    )
    assert fresh.status_code == 200
    assert (work / "a.txt").read_text() == "one"
    bad = await client.post(
        f"{base}/rollback", headers=mia(tenant), json={"number": 1, "commit": "x"}
    )
    assert bad.status_code == 422
