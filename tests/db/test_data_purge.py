"""The purge worker and the retention sweep of M6.8 (docs/architecture.md, Section 17.3)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.datacontrols.purge import FIND_LIMIT, PurgeWorker
from sama.datacontrols.service import ServiceCleanup
from sama.datacontrols.sweep import RetentionSweep
from sama.db.models import AuditEntry, DataPurge, Job, Message, Org, Run, Session
from sama.tenancy.access import Grants
from tests.db.world import World


@pytest.fixture(autouse=True)
async def quiet(engine: AsyncEngine) -> None:
    """No open purge of an earlier test, so a worker sees only the rows of this test."""
    async with engine.begin() as connection:
        await connection.execute(
            update(DataPurge)
            .where(DataPurge.done_at.is_(None))
            .where(DataPurge.next_attempt_at < text("now() + interval '1 year'"))
            .values(done_at=func.now())
        )


@dataclass
class Found:
    sandbox_id: str
    labels: Mapping[str, str]


@dataclass
class FakeSandboxes:
    boxes: list[Found] = field(default_factory=list)
    killed: list[str] = field(default_factory=list)
    fail: bool = False

    async def find(self, labels: Mapping[str, str]) -> Sequence[Found]:
        if self.fail:
            raise RuntimeError("E2B is down")
        return [
            box
            for box in self.boxes
            if box.sandbox_id not in self.killed
            and all(box.labels.get(key) == value for key, value in labels.items())
        ][:FIND_LIMIT]

    async def kill(self, sandbox_id: str) -> None:
        self.killed.append(sandbox_id)


@dataclass
class FakeTraces:
    deleted: list[str] = field(default_factory=list)

    async def delete_traces(self, trace_ids: Sequence[str]) -> None:
        self.deleted.extend(trace_ids)


async def add_purge(engine: AsyncEngine, org_id: uuid.UUID, kind: str, refs: object) -> uuid.UUID:
    async with engine.begin() as connection:
        return (
            await connection.execute(
                insert(DataPurge)
                .values(org_id=org_id, kind=kind, refs=refs)
                .returning(DataPurge.id)
            )
        ).scalar_one()


async def purge_row(engine: AsyncEngine, row_id: uuid.UUID) -> Any:
    async with engine.connect() as connection:
        return (await connection.execute(select(DataPurge).where(DataPurge.id == row_id))).one()


def box(name: str, org: uuid.UUID, session: uuid.UUID | None = None) -> Found:
    labels = {"sama.org_id": str(org)}
    if session is not None:
        labels["sama.session_id"] = str(session)
    return Found(name, labels)


async def test_the_worker_kills_only_the_sandboxes_of_the_deleted_sessions(
    engine: AsyncEngine, fresh: World
) -> None:
    org_a, org_b = fresh.ids["org-a"], fresh.ids["org-b"]
    gone, kept = fresh.ids["a1-alice"], fresh.ids["a1-shared"]
    sandboxes = FakeSandboxes(
        [box("gone", org_a, gone), box("kept", org_a, kept), box("other", org_b, gone)]
    )
    row = await add_purge(engine, org_a, "sandboxes", {"session_ids": [str(gone)]})

    await PurgeWorker(engine, sandboxes=sandboxes).run_once()

    assert sandboxes.killed == ["gone"]
    done = await purge_row(engine, row)
    assert done.done_at is not None
    assert done.attempts == 1


async def test_an_org_purge_kills_every_sandbox_of_the_org_only(
    engine: AsyncEngine, fresh: World
) -> None:
    org_a, org_b = fresh.ids["org-a"], fresh.ids["org-b"]
    sandboxes = FakeSandboxes([box("a1", org_a), box("a2", org_a), box("b", org_b)])
    row = await add_purge(engine, org_a, "sandboxes", {"all": True})

    await PurgeWorker(engine, sandboxes=sandboxes).run_once()

    assert sorted(sandboxes.killed) == ["a1", "a2"]
    assert (await purge_row(engine, row)).done_at is not None


async def test_a_full_page_of_sandboxes_keeps_the_purge_open(
    engine: AsyncEngine, fresh: World
) -> None:
    org_a = fresh.ids["org-a"]
    sandboxes = FakeSandboxes([box(f"s{n}", org_a) for n in range(FIND_LIMIT + 5)])
    row = await add_purge(engine, org_a, "sandboxes", {"all": True})
    worker = PurgeWorker(engine, sandboxes=sandboxes)

    await worker.run_once()
    assert (await purge_row(engine, row)).done_at is None
    async with engine.begin() as connection:
        await connection.execute(
            update(DataPurge).where(DataPurge.id == row).values(next_attempt_at=func.now())
        )
    await worker.run_once()

    assert len(sandboxes.killed) == FIND_LIMIT + 5
    assert (await purge_row(engine, row)).done_at is not None


async def test_a_failure_waits_with_a_backoff(engine: AsyncEngine, fresh: World) -> None:
    row = await add_purge(engine, fresh.ids["org-a"], "sandboxes", {"all": True})

    await PurgeWorker(engine, sandboxes=FakeSandboxes(fail=True)).run_once()

    failed = await purge_row(engine, row)
    assert failed.done_at is None
    assert failed.last_error == "RuntimeError"
    assert failed.next_attempt_at > datetime.now(UTC) + timedelta(seconds=20)


async def test_the_worker_deletes_traces_and_leaves_kinds_it_cannot_do(
    engine: AsyncEngine, fresh: World
) -> None:
    org_a = fresh.ids["org-a"]
    traces = await add_purge(engine, org_a, "traces", {"trace_ids": ["a" * 32, "b" * 32]})
    sandboxes = await add_purge(engine, org_a, "sandboxes", {"all": True})
    fake = FakeTraces()

    await PurgeWorker(engine, traces=fake).run_once()

    assert sorted(fake.deleted) == ["a" * 32, "b" * 32]
    assert (await purge_row(engine, traces)).done_at is not None
    waiting = await purge_row(engine, sandboxes)
    assert waiting.done_at is None
    assert waiting.attempts == 0


async def test_old_done_purges_are_forgotten(engine: AsyncEngine, fresh: World) -> None:
    row = await add_purge(engine, fresh.ids["org-a"], "traces", {"trace_ids": []})
    async with engine.begin() as connection:
        await connection.execute(
            update(DataPurge)
            .where(DataPurge.id == row)
            .values(done_at=text("now() - interval '31 days'"))
        )
    await PurgeWorker(engine, traces=FakeTraces()).run_once()
    async with engine.connect() as connection:
        found = (
            await connection.execute(select(DataPurge.id).where(DataPurge.id == row))
        ).scalar_one_or_none()
    assert found is None


async def age(engine: AsyncEngine, world: World, session: str, days: int) -> None:
    """Moves the activity of a seeded session ``days`` into the past."""
    session_id = world.ids[session]
    async with engine.begin() as connection:
        past = text(f"now() - interval '{days} days'")
        await connection.execute(
            update(Session).where(Session.id == session_id).values(created_at=past, updated_at=past)
        )
        await connection.execute(
            update(Message).where(Message.session_id == session_id).values(created_at=past)
        )
        await connection.execute(
            update(Run)
            .where(Run.session_id == session_id)
            .values(status="completed", created_at=past, finished_at=past)
        )


async def set_retention(engine: AsyncEngine, org_id: uuid.UUID, days: int | None) -> None:
    async with engine.begin() as connection:
        await connection.execute(update(Org).where(Org.id == org_id).values(retention_days=days))


async def session_ids(engine: AsyncEngine, org_id: uuid.UUID) -> set[uuid.UUID]:
    async with engine.connect() as connection:
        return set(
            (await connection.execute(select(Session.id).where(Session.org_id == org_id))).scalars()
        )


async def test_the_sweep_deletes_old_sessions_of_orgs_with_a_retention_only(
    engine: AsyncEngine, fresh: World
) -> None:
    org_a, org_b = fresh.ids["org-a"], fresh.ids["org-b"]
    for session in ("a1-shared", "a1-alice", "b1-shared"):
        await age(engine, fresh, session, 40)
    await set_retention(engine, org_a, 30)
    before_b = await session_ids(engine, org_b)

    deleted = await RetentionSweep(engine).sweep_org(org_a, datetime.now(UTC) - timedelta(days=30))

    assert deleted == 2
    assert await session_ids(engine, org_a) == {fresh.ids["a2-shared"]}
    assert await session_ids(engine, org_b) == before_b
    async with engine.connect() as connection:
        details = (
            await connection.execute(
                select(AuditEntry.details).where(
                    AuditEntry.org_id == org_a, AuditEntry.action == "data.retention"
                )
            )
        ).scalar_one()
    assert details["sessions"] == 2


async def test_the_sweep_keeps_recent_running_and_scheduled_sessions(
    engine: AsyncEngine, fresh: World
) -> None:
    org_a = fresh.ids["org-a"]
    for session in ("a1-shared", "a1-alice", "a2-shared"):
        await age(engine, fresh, session, 40)
    async with engine.begin() as connection:
        # a1-shared has a new message, a1-alice has a queued run, a2-shared has a job.
        await connection.execute(
            update(Message)
            .where(Message.session_id == fresh.ids["a1-shared"])
            .values(created_at=func.now())
        )
        await connection.execute(
            update(Run).where(Run.session_id == fresh.ids["a1-alice"]).values(status="queued")
        )
        job = (await connection.execute(select(Job.id).where(Job.org_id == org_a))).first()
        if job is not None:
            await connection.execute(
                update(Job).where(Job.id == job.id).values(session_id=fresh.ids["a2-shared"])
            )
    deleted = await RetentionSweep(engine).sweep_org(org_a, datetime.now(UTC) - timedelta(days=30))
    kept = await session_ids(engine, org_a)
    assert {fresh.ids["a1-shared"], fresh.ids["a1-alice"]} <= kept
    if job is not None:
        assert fresh.ids["a2-shared"] in kept
        assert deleted == 0


async def test_run_once_uses_the_org_setting(engine: AsyncEngine, fresh: World) -> None:
    org_b = fresh.ids["org-b"]
    await age(engine, fresh, "b1-shared", 10)
    await set_retention(engine, org_b, 5)
    try:
        assert await RetentionSweep(engine).run_once() >= 1
    finally:
        await set_retention(engine, org_b, None)
    assert fresh.ids["b1-shared"] not in await session_ids(engine, org_b)


@dataclass
class FakeConnections:
    deleted: list[uuid.UUID] = field(default_factory=list)
    fail: bool = False

    async def delete(
        self, org_id: uuid.UUID, connection_id: uuid.UUID, *, actor: uuid.UUID
    ) -> bool:
        if self.fail:
            raise RuntimeError("Composio is down")
        self.deleted.append(connection_id)
        return True


@dataclass
class FakeTriggers:
    deleted: list[uuid.UUID] = field(default_factory=list)
    fail: bool = False

    async def delete(
        self, org_id: uuid.UUID, workspace_id: uuid.UUID, trigger_id: uuid.UUID, grants: Grants
    ) -> None:
        if self.fail:
            raise RuntimeError("Composio is down")
        assert grants.org_owner
        self.deleted.append(trigger_id)


@dataclass
class FakeIntegrations:
    disconnected: list[tuple[uuid.UUID, str]] = field(default_factory=list)

    async def disconnect(self, org_id: uuid.UUID, kind: str, user_id: uuid.UUID) -> bool:
        self.disconnected.append((org_id, kind))
        return True


async def test_the_user_cleanup_removes_only_the_accounts_of_the_user(
    engine: AsyncEngine, fresh: World
) -> None:
    connections, triggers = FakeConnections(), FakeTriggers()
    cleanup = ServiceCleanup(engine, connections=connections, triggers=triggers)

    await cleanup.before_user_delete(fresh.ids["org-a"], fresh.ids["alice"], fresh.ids["carol"])

    assert connections.deleted == [fresh.ids["conn:a1-alice"]]
    assert triggers.deleted == [fresh.ids["trig:a1"]]


async def test_the_org_cleanup_removes_every_account_and_survives_a_failure(
    engine: AsyncEngine, fresh: World
) -> None:
    org_b = fresh.ids["org-b"]
    integrations = FakeIntegrations()
    cleanup = ServiceCleanup(
        engine,
        connections=FakeConnections(fail=True),
        triggers=FakeTriggers(fail=True),
        integrations=integrations,
    )

    await cleanup.before_org_delete(org_b, fresh.ids["bob"])

    assert integrations.disconnected == [(org_b, "telegram")]
