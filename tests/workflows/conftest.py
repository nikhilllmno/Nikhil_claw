"""Fixtures for the workflow tests with Postgres.

The workflow engine reads all orgs, so these tests use their own database. Each test gets a new
org, and the org goes away after the test, with its workflows, instances, steps, and receipts.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import delete, insert, text, update
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from sama.connectors.service import RunScope as ConnectionScope
from sama.db import create_engine
from sama.db.models import (
    AgentProfile,
    Membership,
    Org,
    Run,
    Session,
    User,
    Workspace,
)
from sama.orchestrator.queue import RunQueue
from sama.scheduler.jobs import Actor
from sama.workflows.engine import Reconciled, WorkflowEngine
from sama.workflows.service import WorkflowService
from tests.db.conftest import migrated_database

TOOLSETS = ["core", "todo", "web", "clarify", "cron", "integrations", "workflows"]


@pytest.fixture(scope="session")
def workflow_database_url() -> Iterator[str]:
    with migrated_database() as url:
        yield url


@pytest.fixture
async def engine(workflow_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(workflow_database_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@dataclass
class Tenant:
    """One org with the workspace ``ws``, the member ``ana`` (email ``ana@example.test``), the
    profile ``profile``, ana's private session ``chat``, and the queued, attended run
    ``chat-run`` in it."""

    engine: AsyncEngine
    ids: dict[str, uuid.UUID] = field(default_factory=dict)

    @property
    def org(self) -> uuid.UUID:
        return self.ids["org"]

    def user(self, **changes: Any) -> Actor:
        values: dict[str, Any] = {
            "org_id": self.org,
            "user_id": self.ids["ana"],
            "kind": "user",
            "session_id": self.ids["chat"],
            "workspace_id": self.ids["ws"],
            "agent_profile_id": self.ids["profile"],
        }
        values.update(changes)
        return Actor(**values)

    def agent(self, run: str = "chat-run", **changes: Any) -> Actor:
        return self.user(kind="agent", run_id=self.ids[run], **changes)


@pytest.fixture
async def tenant(engine: AsyncEngine) -> AsyncIterator[Tenant]:
    world = Tenant(engine)
    ids = world.ids
    for label in ("org", "ws", "profile", "ana", "chat", "chat-run"):
        ids[label] = uuid.uuid4()
    org_id = ids["org"]
    rows: list[tuple[Any, dict[str, Any]]] = [
        (Org, {"id": org_id, "name": "J", "slug": f"j-{org_id.hex[:8]}"}),
        (Workspace, {"id": ids["ws"], "org_id": org_id, "name": "ws", "slug": "ws"}),
        (
            User,
            {
                "id": ids["ana"],
                "org_id": org_id,
                "auth_user_id": uuid.uuid4(),
                "email": "ana@example.test",
                "display_name": "ana",
            },
        ),
        (Membership, {"org_id": org_id, "user_id": ids["ana"], "role": "member"}),
        (
            Membership,
            {"org_id": org_id, "user_id": ids["ana"], "workspace_id": ids["ws"], "role": "member"},
        ),
        (
            AgentProfile,
            {
                "id": ids["profile"],
                "org_id": org_id,
                "name": "p",
                "runtime": "fake",
                "model": "fake-model",
                "soul": "You are a test agent.",
                "toolsets": TOOLSETS,
                "group_toolsets": TOOLSETS,
            },
        ),
        (
            Session,
            {
                "id": ids["chat"],
                "org_id": org_id,
                "workspace_id": ids["ws"],
                "agent_profile_id": ids["profile"],
                "audience": "private",
                "owner_user_id": ids["ana"],
            },
        ),
        (
            Run,
            {
                "id": ids["chat-run"],
                "org_id": org_id,
                "session_id": ids["chat"],
                "agent_profile_id": ids["profile"],
                "triggered_by_user_id": ids["ana"],
                "runtime": "fake",
            },
        ),
    ]
    async with engine.begin() as connection:
        for model, values in rows:
            await connection.execute(insert(model).values(**values))
    yield world
    async with engine.begin() as connection:
        await connection.execute(
            update(Run)
            .where(Run.org_id == org_id, Run.status.in_(("queued", "running", "paused")))
            .values(status="cancelled", lease_owner=None, lease_expires_at=None)
        )
        await connection.execute(text("SET LOCAL sama.audit_purge = 'on'"))
        await connection.execute(delete(Org).where(Org.id == org_id))


class Crash(BaseException):
    """The worker process stops in the middle of a call. It is not an ``Exception``, so the
    engine does not catch it."""


Behavior = str | BaseException
"""``"ok"`` answers, ``"write-then-crash"`` does the write and then stops the worker, and an
exception is raised as it is."""


@dataclass
class FakeTools:
    """A tool runner that records each call. ``writes`` counts the calls that did a write at
    the provider."""

    accesses: dict[str, str] = field(
        default_factory=lambda: {
            "notes_read": "read",
            "notes_write": "write",
            "keyed_write": "write",
        }
    )
    keyed: set[str] = field(default_factory=lambda: {"keyed_write"})
    script: dict[str, list[Behavior]] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any], str | None]] = field(default_factory=list)
    writes: int = 0
    found: Reconciled | None = None
    checks: list[str] = field(default_factory=list)

    def access(self, name: str) -> str | None:
        return self.accesses.get(name)

    def idempotent(self, name: str) -> bool:
        return name in self.keyed

    async def call(
        self,
        scope: ConnectionScope,
        name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str | None,
    ) -> str:
        self.calls.append((name, arguments, idempotency_key))
        steps = self.script.get(name) or []
        behavior: Behavior = steps.pop(0) if steps else "ok"
        if isinstance(behavior, BaseException):
            raise behavior
        if self.accesses.get(name) != "read":
            self.writes += 1
        if behavior == "write-then-crash":
            raise Crash
        return json.dumps({"tool": name, "args": arguments})

    async def reconcile(
        self,
        scope: ConnectionScope,
        name: str,
        arguments: dict[str, Any],
        *,
        operation_id: str,
    ) -> Reconciled | None:
        self.checks.append(operation_id)
        return self.found


@pytest.fixture
def tools() -> FakeTools:
    return FakeTools()


@pytest.fixture
def service(engine: AsyncEngine, tools: FakeTools) -> WorkflowService:
    return WorkflowService(engine, RunQueue(engine), tools.access)


@pytest.fixture
def runner(engine: AsyncEngine, tools: FakeTools) -> WorkflowEngine:
    return WorkflowEngine(engine, RunQueue(engine), tools, retry_base_s=0)
