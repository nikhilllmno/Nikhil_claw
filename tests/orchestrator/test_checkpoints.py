"""``/undo`` and ``/rollback`` in the control plane (Section 15.3)."""

from __future__ import annotations

import asyncio
import shutil
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AuditEntry
from sama.orchestrator.checkpoints import (
    BUSY,
    NO_CHECKPOINTS,
    UNAVAILABLE,
    CheckpointService,
    CheckpointServiceError,
    checkpoint_reply,
)
from sama.orchestrator.queue import RunQueue
from sama.runtime.checkpoints import Checkpoints
from sama.sandbox.base import ExecResult, Sandbox, SandboxError
from sama.sandbox.local import LocalSandbox
from tests.orchestrator.conftest import Tenant

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

ARGV = (sys.executable, "-m", "sama.runtime.checkpoints")


class _Sandboxes:
    def __init__(self, sandbox: Sandbox | None) -> None:
        self.sandbox = sandbox
        self.asked: list[tuple[str, str]] = []

    async def __call__(self, org_id: str, session_id: str) -> Sandbox | None:
        self.asked.append((org_id, session_id))
        return self.sandbox


@pytest.fixture
def sandbox(tmp_path: Path) -> LocalSandbox:
    box = LocalSandbox(tmp_path / "box")
    Path(box.workdir).mkdir(parents=True)
    (tmp_path / "box" / "home").mkdir()
    return box


def _two_runs(work: Path) -> None:
    """Run r1 writes a.txt = two, run r2 writes b.txt. Checkpoints come before each."""
    checkpoints = Checkpoints(str(work))
    (work / "a.txt").write_text("one")
    checkpoints.snapshot("r1", "Write")
    (work / "a.txt").write_text("two")
    checkpoints.snapshot("r2", "Bash")
    (work / "b.txt").write_text("b")


async def test_the_service_lists_undoes_and_rolls_back(
    engine: AsyncEngine, tenant: Tenant, sandbox: LocalSandbox
) -> None:
    work = Path(sandbox.workdir)
    _two_runs(work)
    sandboxes = _Sandboxes(sandbox)
    service = CheckpointService(engine, sandboxes, argv=ARGV)
    org, session, mia = tenant.org, tenant.ids["mine"], tenant.ids["mia"]

    listed = await service.listing(org_id=org, session_id=session)
    assert [(c.number, c.tool, c.kind) for c in listed] == [
        (1, "Bash", "tool"),
        (2, "Write", "tool"),
    ]
    assert sandboxes.asked[0] == (str(org), str(session))

    restored = await service.undo(org_id=org, user_id=mia, session_id=session)
    assert not (work / "b.txt").exists()
    assert (work / "a.txt").read_text() == "two"
    assert (restored.files, restored.deleted) == (1, 1)
    assert restored.created_at == listed[0].created_at

    await service.rollback(org_id=org, user_id=mia, session_id=session, number=3)
    assert (work / "a.txt").read_text() == "one"

    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(AuditEntry.action, AuditEntry.actor_user_id, AuditEntry.target_id)
                .where(AuditEntry.org_id == org, AuditEntry.action.like("session.%"))
                .order_by(AuditEntry.id)
            )
        ).all()
    assert [(row.action, row.actor_user_id, row.target_id) for row in rows] == [
        ("session.undo", mia, str(session)),
        ("session.rollback", mia, str(session)),
    ]


async def test_a_session_with_an_open_run_cannot_restore(
    engine: AsyncEngine, tenant: Tenant, sandbox: LocalSandbox
) -> None:
    _two_runs(Path(sandbox.workdir))
    service = CheckpointService(engine, _Sandboxes(sandbox), argv=ARGV)
    org, session = tenant.org, tenant.ids["mine"]
    await RunQueue(engine).enqueue(
        org_id=org, session_id=session, text="hi", user_id=tenant.ids["mia"]
    )
    with pytest.raises(CheckpointServiceError, match="A run is open"):
        await service.undo(org_id=org, user_id=None, session_id=session)
    assert (Path(sandbox.workdir) / "b.txt").exists()
    # The list works during a run.
    assert len(await service.listing(org_id=org, session_id=session)) == 2


class _Slow:
    """A sandbox whose command waits until the test lets it go."""

    def __init__(self, inner: LocalSandbox) -> None:
        self.inner = inner
        self.workdir = inner.workdir
        self.started = asyncio.Event()
        self.go = asyncio.Event()

    async def exec(self, argv: Sequence[str], **kwargs: Any) -> ExecResult:
        self.started.set()
        await self.go.wait()
        return await self.inner.exec(argv, **kwargs)


async def test_no_run_can_start_during_a_restore(
    engine: AsyncEngine, tenant: Tenant, sandbox: LocalSandbox
) -> None:
    _two_runs(Path(sandbox.workdir))
    slow = _Slow(sandbox)
    service = CheckpointService(engine, _Sandboxes(cast(Sandbox, slow)), argv=ARGV)
    org, session = tenant.org, tenant.ids["mine"]
    undo = asyncio.create_task(service.undo(org_id=org, user_id=None, session_id=session))
    await asyncio.wait_for(slow.started.wait(), 10)
    enqueue = asyncio.create_task(
        RunQueue(engine).enqueue(org_id=org, session_id=session, text="hi", user_id=None)
    )
    await asyncio.sleep(0.5)
    # The restore holds the session row: the new run waits for it.
    assert not enqueue.done()
    slow.go.set()
    await asyncio.wait_for(undo, 30)
    await asyncio.wait_for(enqueue, 10)
    assert not (Path(sandbox.workdir) / "b.txt").exists()


async def test_a_session_without_a_sandbox_has_no_checkpoints(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    service = CheckpointService(engine, _Sandboxes(None), argv=ARGV)
    org, session = tenant.org, tenant.ids["mine"]
    assert await service.listing(org_id=org, session_id=session) == []
    with pytest.raises(CheckpointServiceError, match=NO_CHECKPOINTS):
        await service.undo(org_id=org, user_id=None, session_id=session)
    with pytest.raises(CheckpointServiceError, match="The session does not exist"):
        await service.undo(org_id=org, user_id=None, session_id=uuid.uuid4())
    with pytest.raises(CheckpointServiceError, match="1 or more"):
        await service.rollback(org_id=org, user_id=None, session_id=session, number=0)


class _Broken:
    workdir = "/work"
    id = "broken"

    def __init__(self, result: ExecResult | None) -> None:
        self.result = result

    async def exec(self, argv: object, **_: object) -> ExecResult:
        if self.result is None:
            raise SandboxError("gone")
        return self.result


@pytest.mark.parametrize(
    "result",
    [
        None,
        ExecResult(1, "", "Traceback"),
        ExecResult(0, "not json", ""),
        ExecResult(0, "", ""),
    ],
)
async def test_a_failed_command_gives_a_short_message(
    engine: AsyncEngine, tenant: Tenant, result: ExecResult | None
) -> None:
    service = CheckpointService(engine, _Sandboxes(_Broken(result)))  # type: ignore[arg-type]
    with pytest.raises(CheckpointServiceError, match=UNAVAILABLE):
        await service.listing(org_id=tenant.org, session_id=tenant.ids["mine"])


async def test_the_message_of_the_command_goes_to_the_user(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    result = ExecResult(0, '{"ok": false, "busy": true, "error": "A run works."}\n', "")
    service = CheckpointService(engine, _Sandboxes(_Broken(result)))  # type: ignore[arg-type]
    with pytest.raises(CheckpointServiceError, match=r"A run works\."):
        await service.undo(org_id=tenant.org, user_id=None, session_id=tenant.ids["mine"])


async def test_the_reply_of_a_chat(
    engine: AsyncEngine, tenant: Tenant, sandbox: LocalSandbox
) -> None:
    work = Path(sandbox.workdir)
    _two_runs(work)
    service = CheckpointService(engine, _Sandboxes(sandbox), argv=ARGV)
    org, mia, session = tenant.org, tenant.ids["mia"], tenant.ids["mine"]

    async def reply(name: str, rest: str = "", prefix: str = "/") -> str:
        return await checkpoint_reply(
            service,
            org_id=org,
            user_id=mia,
            session_id=session,
            name=name,
            rest=rest,
            prefix=prefix,
        )

    listing = await reply("rollback", prefix="/sama ")
    assert listing.startswith("Checkpoints of the session files, newest first:\n1. ")
    assert " - before Bash\n2. " in listing
    assert "Send /sama rollback NUMBER to restore one. /sama undo restores" in listing
    assert "Write the number" in await reply("rollback", "two")
    undone = await reply("undo")
    assert undone.startswith("The session files are back to the checkpoint of ")
    assert undone.endswith("(1 files, 1 deleted). To go back, send /rollback 1.")
    assert "the files before a restore" in await reply("rollback")
    assert "back to the checkpoint" in await reply("rollback", " 1 ")
    assert (work / "b.txt").exists()
    assert await reply("rollback", "40") == "There is no checkpoint 40."
    unavailable = await checkpoint_reply(
        None, org_id=org, user_id=mia, session_id=session, name="undo", rest=""
    )
    assert unavailable == "Checkpoints are not available here."
    no_session = await checkpoint_reply(
        service, org_id=org, user_id=mia, session_id=None, name="undo", rest=""
    )
    assert no_session == NO_CHECKPOINTS
    await RunQueue(engine).enqueue(org_id=tenant.org, session_id=session, text="hi", user_id=None)
    assert await reply("undo") == BUSY
