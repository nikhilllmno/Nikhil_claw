"""The pool settings, the checkout wait log, and the round trip counter (Section 4.4)."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.routing import Mount, Route

from sama.db import create_engine
from sama.db.monitor import PoolMonitor, RequestMonitor
from sama.db.pool import (
    PoolSettings,
    PoolStats,
    TimedQueuePool,
    component,
    describe,
    named,
    prewarm,
)
from sama.db.trips import count_trips


@pytest.fixture
def url(database_url: str) -> str:
    return database_url


def test_the_default_settings_fit_one_machine() -> None:
    settings = PoolSettings.from_env({})
    assert (settings.size, settings.overflow, settings.timeout_s) == (5, 2, 30.0)
    # A rolling deploy runs two processes at the same time, and the session pooler gives 15.
    assert 2 * (settings.size + settings.overflow) <= 15
    assert settings.warn_ms == 100.0


def test_the_settings_come_from_the_environment() -> None:
    settings = PoolSettings.from_env(
        {
            "SAMA_DB_POOL_SIZE": "20",
            "SAMA_DB_POOL_OVERFLOW": "0",
            "SAMA_DB_POOL_TIMEOUT_S": "5",
            "SAMA_DB_POOL_RECYCLE_S": "600",
            "SAMA_DB_POOL_PREWARM": "50",
            "SAMA_DB_POOL_WARN_MS": "250",
        }
    )
    assert (settings.size, settings.overflow, settings.timeout_s) == (20, 0, 5.0)
    assert settings.recycle_s == 600.0
    assert settings.prewarm == 20, "the prewarm never goes over the size"
    assert settings.warn_ms == 250.0
    assert settings.engine_options()["poolclass"] is TimedQueuePool


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SAMA_DB_POOL_SIZE", "0"),
        ("SAMA_DB_POOL_SIZE", "many"),
        ("SAMA_DB_POOL_OVERFLOW", "-1"),
        ("SAMA_DB_POOL_TIMEOUT_S", "0"),
    ],
)
def test_a_bad_setting_names_the_variable(name: str, value: str) -> None:
    with pytest.raises(ValueError, match=name):
        PoolSettings.from_env({name: value})


def test_the_stats_count_slow_checkouts_by_component() -> None:
    stats = PoolStats(warn_s=0.1)
    assert stats.record(0.01, 0.0, "a") is False
    assert stats.record(0.5, 0.0, "GET /x") is True
    assert stats.record(0.2, 0.3, "GET /x") is True
    assert (stats.checkouts, stats.slow, stats.connects) == (3, 2, 1)
    assert stats.by_component == {"GET /x": 2}
    assert stats.wait_max_s == 0.5


def test_the_component_name_is_scoped() -> None:
    assert component() == "unknown"
    with named("worker"):
        assert component() == "worker"
    assert component() == "unknown"


async def test_the_engine_has_the_measured_pool(url: str) -> None:
    engine = create_engine(url, pool=PoolSettings(size=2, overflow=0, timeout_s=1.0))
    try:
        pool = engine.sync_engine.pool
        assert isinstance(pool, TimedQueuePool)
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        assert pool.stats.checkouts == 1
        assert pool.stats.connects == 1
        line = describe(pool)
        assert "size=2" in line
        assert "checkouts=1" in line
        assert "@" not in line, "no host and no secret"
    finally:
        await engine.dispose()


async def test_a_checkout_that_waits_logs_a_warning_with_the_component(
    url: str, caplog: pytest.LogCaptureFixture
) -> None:
    pool = PoolSettings(size=1, overflow=0, timeout_s=5.0, warn_ms=50.0, prewarm=0)
    engine = create_engine(url, pool=pool)
    try:
        holder = await engine.connect()

        async def release_later() -> None:
            await asyncio.sleep(0.3)
            await holder.close()

        task = asyncio.create_task(release_later())
        with caplog.at_level(logging.WARNING, logger="sama.db.pool"), named("GET /slow"):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        await task
        warnings = [r.getMessage() for r in caplog.records if "pool wait" in r.getMessage()]
        assert len(warnings) == 1
        assert "component=GET /slow" in warnings[0]
        assert "in_use=" in warnings[0]
        stats = engine.sync_engine.pool.stats  # type: ignore[attr-defined]
        assert stats.slow == 1
        assert stats.by_component == {"GET /slow": 1}
    finally:
        await engine.dispose()


async def test_the_first_connect_is_not_a_wait(url: str, caplog: pytest.LogCaptureFixture) -> None:
    engine = create_engine(url, pool=PoolSettings(size=1, overflow=0, warn_ms=0.0001))
    try:
        with caplog.at_level(logging.WARNING, logger="sama.db.pool"):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        waits = [r for r in caplog.records if "pool wait" in r.getMessage()]
        assert waits == [] or all("wait 0 ms" in r.getMessage() for r in waits)
    finally:
        await engine.dispose()


async def test_the_prewarm_opens_the_connections_at_once(url: str) -> None:
    engine = create_engine(url, pool=PoolSettings(size=3, overflow=0))
    try:
        assert await prewarm(engine, 3) == 3
        pool = engine.sync_engine.pool
        assert pool.checkedin() == 3  # type: ignore[attr-defined]
        assert pool.stats.connects == 3  # type: ignore[attr-defined]
        assert await prewarm(engine, 0) == 0
    finally:
        await engine.dispose()


async def test_the_prewarm_survives_a_bad_database() -> None:
    engine = create_engine(
        "postgresql://nobody:x@127.0.0.1:1/none", pool=PoolSettings(size=1, overflow=0)
    )
    try:
        assert await prewarm(engine, 1) == 0
    finally:
        await engine.dispose()


async def test_count_trips_counts_statements_and_transactions(url: str) -> None:
    engine = create_engine(url, pool=PoolSettings(size=1, overflow=0))
    try:
        with count_trips() as trips:
            async with engine.begin() as connection:
                await connection.execute(text("SELECT 1"))
                await connection.execute(text("SELECT 2"))
        assert (trips.statements, trips.begins, trips.ends) == (2, 1, 1)
        assert trips.total == 4
        assert "SELECT 2" in trips.show()
        with count_trips() as trips:
            async with engine.connect() as connection:
                await connection.execution_options(isolation_level="AUTOCOMMIT")
                await connection.execute(text("SELECT 1"))
        assert trips.total == 1, "a statement without a transaction is one round trip"
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        # Outside a block, nothing counts and nothing fails.
    finally:
        await engine.dispose()


SECRET = "SECRET-TOKEN-123"


def monitored_app(engine: AsyncEngine, seen: list[str]) -> Starlette:
    """An app with the routes of the email and WhatsApp hubs (a secret token in the path), a
    mounted app, and the request monitor, built like the control plane."""

    async def work() -> JSONResponse:
        seen.append(component())
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return JSONResponse({})

    api = FastAPI()
    for method in ("GET", "POST"):
        api.add_api_route("/email/{integration_id}/approvals/{token}", work, methods=[method])
        api.add_api_route("/whatsapp/{integration_id}/webhook/{token}", work, methods=[method])
    api.add_api_route("/sessions/{session_id}/messages", work, methods=["POST"])

    async def plain(request: Request) -> JSONResponse:
        return await work()

    inner = Starlette(routes=[Route("/{ticket}/pack", plain, methods=["POST"])])
    return Starlette(
        routes=[Mount("/git", app=inner), Mount("", app=api)],
        middleware=[Middleware(RequestMonitor)],
    )


@pytest.mark.parametrize(
    ("method", "path", "template"),
    [
        (
            "GET",
            f"/email/{uuid.uuid4()}/approvals/{SECRET}",
            "/email/{integration_id}/approvals/{token}",
        ),
        (
            "POST",
            f"/email/{uuid.uuid4()}/approvals/{SECRET}",
            "/email/{integration_id}/approvals/{token}",
        ),
        (
            "GET",
            f"/whatsapp/{uuid.uuid4()}/webhook/{SECRET}",
            "/whatsapp/{integration_id}/webhook/{token}",
        ),
        (
            "POST",
            f"/whatsapp/{uuid.uuid4()}/webhook/{SECRET}",
            "/whatsapp/{integration_id}/webhook/{token}",
        ),
        ("POST", f"/sessions/{uuid.uuid4()}/messages", "/sessions/{session_id}/messages"),
        ("POST", f"/git/{SECRET}/pack", "/git/{ticket}/pack"),
    ],
)
async def test_the_request_name_is_the_route_template_and_no_log_line_has_the_path(
    url: str,
    caplog: pytest.LogCaptureFixture,
    method: str,
    path: str,
    template: str,
) -> None:
    engine = create_engine(url, pool=PoolSettings(size=1, overflow=0, warn_ms=50.0, prewarm=0))
    seen: list[str] = []
    app = monitored_app(engine, seen)
    try:
        holder = await engine.connect()

        async def release_later() -> None:
            await asyncio.sleep(0.2)
            await holder.close()

        task = asyncio.create_task(release_later())
        with caplog.at_level(logging.DEBUG):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://sama.test"
            ) as client:
                response = await client.request(method, path)
        await task
        assert response.status_code == 200
        assert seen == [f"{method} {template}"]
        assert component() == "unknown", "the name does not leak out of the request"
        messages = [r.getMessage() for r in caplog.records]
        assert any(f"component={method} {template} " in m for m in messages), messages
        assert any(f"request {method} {template} db_round_trips=" in m for m in messages)
        ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("sama"))
        assert "database pool wait" in ours
        assert SECRET not in ours, "a secret in the path reached a log line of sama"
        assert path not in ours
    finally:
        await engine.dispose()


async def test_a_path_that_matches_no_route_is_named_unmatched(
    url: str, caplog: pytest.LogCaptureFixture
) -> None:
    engine = create_engine(url, pool=PoolSettings(size=1, overflow=0))
    app = monitored_app(engine, [])
    try:
        with caplog.at_level(logging.DEBUG, logger="sama.db.monitor"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://sama.test"
            ) as client:
                response = await client.get(f"/nothing/{SECRET}")
        assert response.status_code == 404
        assert any(
            "request GET unmatched db_round_trips=0" in r.getMessage() for r in caplog.records
        )
        ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("sama"))
        assert SECRET not in ours
    finally:
        await engine.dispose()


async def test_the_request_monitor_ignores_other_scopes(url: str) -> None:
    engine = create_engine(url, pool=PoolSettings(size=1, overflow=0))
    seen: list[str] = []

    async def app(scope: dict, receive: object, send: object) -> None:  # type: ignore[type-arg]
        seen.append(component())

    try:
        await RequestMonitor(app)({"type": "websocket", "path": "/x"}, None, None)  # type: ignore[arg-type]
        assert seen == ["unknown"]
    finally:
        await engine.dispose()


async def test_the_pool_monitor_logs_the_state_and_stops(
    url: str, caplog: pytest.LogCaptureFixture
) -> None:
    engine = create_engine(url, pool=PoolSettings(size=1, overflow=0))
    stop = asyncio.Event()
    try:
        with caplog.at_level(logging.DEBUG, logger="sama.db.monitor"):
            task = asyncio.create_task(PoolMonitor(engine, interval_s=0.05).serve(stop))
            await asyncio.sleep(0.2)
            stop.set()
            await asyncio.wait_for(task, 2)
        assert any("database pool size=1" in r.getMessage() for r in caplog.records)
    finally:
        await engine.dispose()


def test_the_environment_default_is_used_without_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAMA_DB_POOL_SIZE", "7")
    engine = create_engine("postgresql://u:p@localhost:5432/db")
    assert isinstance(engine, AsyncEngine)
    assert engine.sync_engine.pool.size() == 7  # type: ignore[attr-defined]
    assert os.environ["SAMA_DB_POOL_SIZE"] == "7"


async def test_describe_and_prewarm_accept_a_pool_without_stats(url: str) -> None:
    from sqlalchemy.pool import NullPool

    engine = create_engine(url, poolclass=NullPool)
    try:
        assert describe(engine.sync_engine.pool) == "pool kind=NullPool"
        assert await prewarm(engine, 1) == 1
    finally:
        await engine.dispose()


def test_the_transaction_pooler_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="SAMA_DB_PREPARED_STATEMENTS=off is not supported"):
        PoolSettings.from_env({"SAMA_DB_PREPARED_STATEMENTS": "off"})
    PoolSettings.from_env({"SAMA_DB_PREPARED_STATEMENTS": "on"})


def test_a_url_with_the_transaction_pooler_port_logs_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="sama.db"):
        create_engine("postgresql://u:p@pooler.example.test:6543/db")
        create_engine("postgresql://u:p@pooler.example.test:5432/db")
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    assert "transaction pooler" in messages[0]
    assert "p@" not in messages[0]
