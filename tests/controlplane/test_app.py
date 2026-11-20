from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import socket
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.controlplane import ControlPlane, ControlPlaneSettings, build
from sama.controlplane.server import DrainingServer
from sama.db.models import Run
from sama.runtime.contract import RunCompleted, RuntimeKind, TextDelta, Usage
from sama.runtime.fake import FakeRuntime, FakeScript
from sama.sandbox.local import LocalProvider
from sama.vault.crypto import VaultKey
from sama.vault.redact import REDACTED, Redactor, default_redactor, redact
from sama.vault.store import SecretName, Vault
from tests.api.conftest import SECRET, bearer
from tests.controlplane.test_settings import BASE
from tests.orchestrator.conftest import Tenant
from tests.orchestrator.test_worker import until


def settings(**changes: Any) -> ControlPlaneSettings:
    base = ControlPlaneSettings.from_env(
        {**BASE, "SAMA_SUPABASE_URL": "https://project.example.test"}
    )
    return dataclasses.replace(base, supabase_jwt_secret=SecretStr(SECRET), **changes)


@contextlib.asynccontextmanager
async def running(plane: ControlPlane) -> AsyncIterator[httpx.AsyncClient]:
    async with plane.app.router.lifespan_context(plane.app):
        transport = httpx.ASGITransport(app=plane.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
            yield client


def fake_plane(engine: AsyncEngine, **changes: Any) -> ControlPlane:
    return build(
        settings(**changes),
        engine=engine,
        runtimes=(FakeRuntime(),),
        worker_poll_s=0.02,
        exit_on_failure=False,
    )


async def test_each_part_answers_on_its_path(engine: AsyncEngine) -> None:
    async with running(fake_plane(engine)) as client:
        for path in ("/healthz", "/tools/healthz", "/model/healthz"):
            response = await client.get(path)
            assert response.status_code == 200, path
        config = (await client.get("/config")).json()
        assert config["supabase_url"] == "https://project.example.test"
        assert config["integrations"] is False
        # The tool server and the model proxy need a run token.
        assert (await client.post("/tools/mcp", json={})).status_code == 401
        assert (await client.post("/model/anthropic/v1/messages", json={})).status_code == 401


async def test_the_vault_turns_on_the_integrations(engine: AsyncEngine) -> None:
    plane = fake_plane(engine, vault_key=VaultKey.from_text(VaultKey.generate()))
    async with running(plane) as client:
        assert (await client.get("/config")).json()["integrations"] is True
        hook = await client.post(f"/telegram/{uuid.uuid4()}/webhook", json={"update_id": 1})
        assert hook.status_code == 401


async def test_vision_needs_a_key_or_the_vault(engine: AsyncEngine) -> None:
    assert fake_plane(engine).tools.vision is None
    with_vault = fake_plane(engine, vault_key=VaultKey.from_text(VaultKey.generate()))
    assert with_vault.tools.vision is not None
    assert with_vault.tools.vision.model == "claude-sonnet-5-5"
    with_key = fake_plane(
        engine, anthropic_key=SecretStr("sk-ant-test"), vision_model="claude-opus-5"
    )
    assert with_key.tools.vision is not None
    assert with_key.tools.vision.model == "claude-opus-5"


async def test_a_message_runs_to_the_end_in_the_process(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    async with running(fake_plane(engine)) as client:
        response = await client.post(
            f"/sessions/{tenant.ids['s1']}/messages",
            json={"text": "hello"},
            headers=bearer(tenant.ids["mia_auth"]),
        )
        assert response.status_code == 202
        await until(lambda: _is(engine, response.json()["run_id"], "completed"))


async def test_the_runner_reaches_the_process_by_its_public_url(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    plane = build(
        settings(sandbox_allow_out=("example.org",)),
        engine=engine,
        provider=LocalProvider(tmp_path),
        exit_on_failure=False,
    )
    assert plane.sandboxed is not None
    assert plane.sandboxed.kind is RuntimeKind.CLAUDE_AGENT_SDK
    sandbox = plane.sandboxed._settings
    assert sandbox.model_url == "https://sama-staging.fly.dev/model"
    assert sandbox.gate_url == "https://sama-staging.fly.dev/tools/gate"
    assert sandbox.allow_out[0] == "sama-staging.fly.dev"
    assert sandbox.allow_out[-1] == "example.org"
    assert "github.com" in sandbox.allow_out
    assert plane.tools.repos is not None
    assert not sandbox.open_egress
    servers = plane.worker._config._mcp_servers  # type: ignore[attr-defined]
    assert [(s.name, s.url) for s in servers] == [
        ("sama", "https://sama-staging.fly.dev/tools/mcp")
    ]


async def test_the_redactor_knows_the_vault_before_the_start_ends(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    key = VaultKey.generate()
    secret = "vault-value-written-before-the-start"
    await Vault(engine, VaultKey.from_text(key), redactor=Redactor()).put(
        tenant.org, SecretName.BRAVE_API_KEY, secret
    )
    plane = fake_plane(engine, vault_key=VaultKey.from_text(key))
    started = []
    original = plane.worker.serve

    async def serve(stop: asyncio.Event, *, concurrency: int = 1) -> None:
        started.append(redact(secret))
        await original(stop, concurrency=concurrency)

    plane.worker.serve = serve  # type: ignore[method-assign]
    try:
        async with running(plane):
            assert redact(secret) == REDACTED
            await until(lambda: _true(bool(started)))
        assert started == [REDACTED]
    finally:
        default_redactor().forget(secret)


async def test_a_failed_loop_stops_the_process(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    plane = fake_plane(engine)

    async def broken(stop: asyncio.Event, *, concurrency: int = 1) -> None:
        raise RuntimeError("boom")

    plane.worker.serve = broken  # type: ignore[method-assign]
    with caplog.at_level(logging.CRITICAL, logger="sama.controlplane"):
        async with running(plane):
            async with asyncio.timeout(5):
                await plane.stop.wait()
    assert "a background loop of the control plane ended" in caplog.text


async def test_a_stop_waits_for_the_worker(engine: AsyncEngine) -> None:
    plane = fake_plane(engine)
    ended = asyncio.Event()
    original = plane.worker.serve

    async def serve(stop: asyncio.Event, *, concurrency: int = 1) -> None:
        await original(stop, concurrency=concurrency)
        ended.set()

    plane.worker.serve = serve  # type: ignore[method-assign]
    async with running(plane):
        assert not plane.stop.is_set()
    assert plane.stop.is_set()
    assert ended.is_set()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _status(engine: AsyncEngine, run_id: str) -> str | None:
    async with engine.connect() as connection:
        return await connection.scalar(select(Run.status).where(Run.id == run_id))


async def _is(engine: AsyncEngine, run_id: str, status: str) -> bool:
    return await _status(engine, run_id) == status


async def _true(value: bool) -> bool:
    return value


async def test_a_stop_drains_the_runs_while_http_still_serves(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    runtime = FakeRuntime()
    words = [TextDelta(text=f"w{i} ", usage=Usage(output_tokens=i)) for i in range(1, 9)]
    runtime.add_script(
        FakeScript(events=(*words, RunCompleted(output="done", usage=words[-1].usage)), delay_s=0.1)
    )
    plane = build(
        settings(),
        engine=engine,
        runtimes=(runtime,),
        worker_poll_s=0.02,
        exit_on_failure=False,
    )
    port = _free_port()
    server = DrainingServer(
        uvicorn.Config(plane.app, host="127.0.0.1", port=port, log_config=None), plane
    )
    serving = asyncio.create_task(server.serve())
    base = f"http://127.0.0.1:{port}"
    headers = bearer(tenant.ids["mia_auth"])
    async with httpx.AsyncClient(base_url=base) as client:
        await until(lambda: _true(server.started))
        first = (
            await client.post(
                f"/sessions/{tenant.ids['s1']}/messages", json={"text": "long"}, headers=headers
            )
        ).json()["run_id"]
        await until(lambda: _is(engine, first, "running"))

        server.request_stop()
        await until(lambda: _true(plane.stop.is_set()))
        # The run is still open, and the sandbox callbacks still reach the process.
        assert await _status(engine, first) == "running"
        for path in ("/model/healthz", "/tools/healthz"):
            assert (await client.get(path)).status_code == 200
        second = (
            await client.post(
                f"/sessions/{tenant.ids['s2']}/messages", json={"text": "later"}, headers=headers
            )
        ).json()["run_id"]

    async with asyncio.timeout(15):
        await serving
    assert await _status(engine, first) == "completed"
    # The worker took no new run during the drain. The next process takes it.
    assert await _status(engine, second) == "queued"


async def test_a_second_stop_cancels_the_drain(engine: AsyncEngine, tenant: Tenant) -> None:
    runtime = FakeRuntime()
    words = [TextDelta(text=f"w{i} ", usage=Usage(output_tokens=i)) for i in range(1, 41)]
    runtime.add_script(
        FakeScript(events=(*words, RunCompleted(output="done", usage=words[-1].usage)), delay_s=0.5)
    )
    plane = build(
        settings(), engine=engine, runtimes=(runtime,), worker_poll_s=0.02, exit_on_failure=False
    )
    port = _free_port()
    server = DrainingServer(
        uvicorn.Config(plane.app, host="127.0.0.1", port=port, log_config=None), plane
    )
    serving = asyncio.create_task(server.serve())
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        await until(lambda: _true(server.started))
        run_id = (
            await client.post(
                f"/sessions/{tenant.ids['s1']}/messages",
                json={"text": "long"},
                headers=bearer(tenant.ids["mia_auth"]),
            )
        ).json()["run_id"]
        await until(lambda: _is(engine, run_id, "running"))
    server.request_stop()
    await until(lambda: _true(plane.stop.is_set()))
    server.request_stop()
    # The run needs 20 seconds more. The stop does not wait for it.
    async with asyncio.timeout(5):
        await serving
    assert all(task.done() for task in plane.tasks)
    assert await _status(engine, run_id) != "completed"
