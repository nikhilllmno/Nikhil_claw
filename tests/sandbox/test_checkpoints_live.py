"""Checkpoints in an E2B sandbox (roadmap M6.4, docs/architecture.md Section 15.3).

The test takes snapshots in a live E2B sandbox with the real ``sama.runtime.checkpoints`` module
of the template (``/opt/sama/.venv/bin/python``). It changes and deletes files. Then the control
plane ``CheckpointService`` runs ``/undo`` and ``/rollback`` through ``Sandbox.exec`` with the
default ``CHECKPOINT_ARGV``. Runs with ``SAMA_LIVE_TESTS=1``, ``E2B_API_KEY``, and
``SAMA_E2B_TEMPLATE`` (build the template first), and a test database
(``SAMA_TEST_DATABASE_URL``).
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.controlplane.settings import DEFAULT_SANDBOX_HOSTS
from sama.db.models import AuditEntry
from sama.orchestrator.checkpoints import (
    CHECKPOINT_ARGV,
    CheckpointService,
    CheckpointServiceError,
    checkpoint_reply,
)
from sama.sandbox import Sandbox, SandboxSpec
from tests.orchestrator.conftest import (  # noqa: F401 -- pytest fixtures
    Tenant,
    engine,
    queue_database_url,
    tenant,
)
from tests.sandbox.test_providers import E2B_KEY, E2B_TEMPLATE, LIVE_E2B

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE_E2B, reason="set SAMA_LIVE_TESTS=1 and E2B_API_KEY"),
]

PYTHON = CHECKPOINT_ARGV[0]
"""The Python of the template. ``python3`` of the login shell is the system Python."""

_SNAPSHOT = (
    "import sys, time\n"
    "from sama.runtime.checkpoints import Checkpoints\n"
    "started = time.monotonic()\n"
    "commit = Checkpoints(sys.argv[1]).snapshot(sys.argv[2], sys.argv[3])\n"
    "print(round(time.monotonic() - started, 4), commit or 'none')\n"
)


async def _snapshot(sandbox: Sandbox, run_id: str, tool: str) -> tuple[float, str]:
    """Takes a snapshot in the sandbox, as the runner does. Returns the seconds and the commit."""
    done = await sandbox.exec(
        [PYTHON, "-c", _SNAPSHOT, sandbox.workdir, run_id, tool], timeout_s=120
    )
    assert done.exit_code == 0, done.stdout + done.stderr
    seconds, commit = done.stdout.split()
    return float(seconds), commit


async def _text(sandbox: Sandbox, name: str) -> str | None:
    try:
        return (await sandbox.read_file(f"{sandbox.workdir}/{name}")).decode()
    except FileNotFoundError:
        return None


async def _files(sandbox: Sandbox) -> list[str]:
    done = await sandbox.exec(
        [
            "sh",
            "-c",
            "find . -path ./.sama -prune -o -path ./repo/.git -prune -o -type f -print"
            " -o -type l -print | sort",
        ],
        cwd=sandbox.workdir,
    )
    assert done.exit_code == 0, done.stderr
    return done.stdout.split()


async def test_snapshot_undo_and_rollback_in_a_live_sandbox(
    engine: AsyncEngine,  # noqa: F811 -- the fixture
    tenant: Tenant,  # noqa: F811 -- the fixture
) -> None:
    from sama.sandbox.e2b import E2BProvider

    org_id, session_id = tenant.org, tenant.ids["s1"]
    sandbox = await E2BProvider(api_key=E2B_KEY).create(
        SandboxSpec(
            org_id=str(org_id),
            session_id=str(session_id),
            template=E2B_TEMPLATE,
            allow_out=DEFAULT_SANDBOX_HOSTS,
        )
    )

    async def session_sandbox(_org_id: str, _session_id: str) -> Sandbox | None:
        return sandbox

    service = CheckpointService(engine, session_sandbox)  # the default CHECKPOINT_ARGV
    work = sandbox.workdir
    run = f"run-{uuid.uuid4().hex[:8]}"
    try:
        # The template has the module, git, and a work folder.
        probe = await sandbox.exec(
            [PYTHON, "-c", "import sama.runtime.checkpoints, shutil; print(shutil.which('git'))"]
        )
        assert probe.exit_code == 0, probe.stderr
        assert probe.stdout.strip().endswith("git"), probe.stdout

        # No checkpoint yet: the service says so for a restore, and lists nothing.
        assert await service.listing(org_id=org_id, session_id=session_id) == []
        with pytest.raises(CheckpointServiceError, match="no checkpoints"):
            await service.undo(org_id=org_id, user_id=None, session_id=session_id)

        # Files of the agent: text, binary, a nested folder, a git repository of its own, a
        # dependency folder (excluded), and a link.
        await sandbox.write_file(f"{work}/a.txt", b"one")
        await sandbox.write_file(f"{work}/keep/b.txt", b"keep me")
        await sandbox.write_file(f"{work}/data.bin", bytes(range(256)) * 4)
        await sandbox.write_file(f"{work}/node_modules/dep/index.js", b"dep")
        made = await sandbox.exec(
            [
                "sh",
                "-c",
                "ln -s a.txt link && git init -q repo && echo x > repo/f.txt"
                " && git -C repo add f.txt"
                " && git -C repo -c user.name=t -c user.email=t@t commit -q -m first",
            ],
            cwd=work,
        )
        assert made.exit_code == 0, made.stderr
        repo_head = (
            await sandbox.exec(["git", "-C", "repo", "rev-parse", "HEAD"], cwd=work)
        ).stdout.strip()
        before = await _files(sandbox)
        assert "./link" in before
        assert "./.sama" not in "".join(before)

        # The runner takes the snapshot before the first tool call of the run.
        first_s, first_commit = await _snapshot(sandbox, run, "Write")
        assert first_commit != "none"
        # Nothing changed: the same run does not keep a second snapshot.
        again_s, again_commit = await _snapshot(sandbox, run, "Bash")
        assert again_commit == "none"

        # The agent changes files: edit, delete, add, replace the link.
        await sandbox.write_file(f"{work}/a.txt", b"two")
        await sandbox.remove_file(f"{work}/keep/b.txt")
        await sandbox.write_file(f"{work}/new/deep/n.txt", b"new")
        await sandbox.write_file(f"{work}/data.bin", b"short")
        await sandbox.write_file(f"{work}/node_modules/dep/index.js", b"dep changed")
        changed = await sandbox.exec(["sh", "-c", "rm link && ln -s new/deep/n.txt link"], cwd=work)
        assert changed.exit_code == 0, changed.stderr
        after = await _files(sandbox)
        assert "./new/deep/n.txt" in after
        assert "./keep/b.txt" not in after

        # The control plane lists the checkpoint through the sandbox.
        listed = await service.listing(org_id=org_id, session_id=session_id)
        assert [(c.number, c.tool, c.kind) for c in listed] == [(1, "Write", "tool")]
        text = await checkpoint_reply(
            service,
            org_id=org_id,
            user_id=None,
            session_id=session_id,
            name="rollback",
            rest="",
        )
        assert "1. " in text
        assert "before Write" in text

        # /undo: the files are as they were before the run.
        undone = await service.undo(org_id=org_id, user_id=tenant.ids["mia"], session_id=session_id)
        assert undone.files >= 4
        assert undone.deleted >= 1
        assert await _text(sandbox, "a.txt") == "one"
        assert await _text(sandbox, "keep/b.txt") == "keep me"
        assert await _text(sandbox, "new/deep/n.txt") is None
        assert await sandbox.read_file(f"{work}/data.bin") == bytes(range(256)) * 4
        assert await _files(sandbox) == before
        link = await sandbox.exec(["readlink", "link"], cwd=work)
        assert link.stdout.strip() == "a.txt"
        # The excluded folder and the git repository of the agent stay as they are.
        assert await _text(sandbox, "node_modules/dep/index.js") == "dep changed"
        head = await sandbox.exec(["git", "-C", "repo", "rev-parse", "HEAD"], cwd=work)
        assert head.stdout.strip() == repo_head
        status = await sandbox.exec(["git", "-C", "repo", "status", "--porcelain"], cwd=work)
        assert status.stdout.strip() == ""

        # The restore made a rollback checkpoint with the files before it.
        listed = await service.listing(org_id=org_id, session_id=session_id)
        assert [(c.number, c.kind) for c in listed] == [(1, "rollback"), (2, "tool")]

        # /rollback 1: redo. The files before the undo come back.
        redone = await service.rollback(
            org_id=org_id, user_id=tenant.ids["mia"], session_id=session_id, number=1
        )
        assert redone.files >= 4
        assert await _text(sandbox, "a.txt") == "two"
        assert await _text(sandbox, "keep/b.txt") is None
        assert await _text(sandbox, "new/deep/n.txt") == "new"
        assert await sandbox.read_file(f"{work}/data.bin") == b"short"
        assert await _files(sandbox) == after
        link = await sandbox.exec(["readlink", "link"], cwd=work)
        assert link.stdout.strip() == "new/deep/n.txt"

        # A number that does not exist is a message for the user, not a failure.
        with pytest.raises(CheckpointServiceError, match="no checkpoint 99"):
            await service.rollback(org_id=org_id, user_id=None, session_id=session_id, number=99)

        # Each restore wrote an audit entry.
        async with engine.connect() as connection:
            actions = (
                (
                    await connection.execute(
                        select(AuditEntry.action)
                        .where(AuditEntry.org_id == org_id)
                        .where(AuditEntry.action.in_(("session.undo", "session.rollback")))
                        .order_by(AuditEntry.created_at)
                    )
                )
                .scalars()
                .all()
            )
        assert actions == ["session.undo", "session.rollback"]

        # The CLI writes one JSON object, also when it fails. No secret is in the output.
        cli = await sandbox.exec([*CHECKPOINT_ARGV, "--workdir", work, "list", "--limit", "2"])
        assert cli.exit_code == 0, cli.stderr
        assert [c["kind"] for c in json.loads(cli.stdout)["checkpoints"]] == ["rollback"] * 2

        # A runner holds the runner lock: a restore waits, then says that a run works.
        runner = await sandbox.start(
            [
                PYTHON,
                "-c",
                "import fcntl, os, time\n"
                "fd = os.open('.sama/runner.lock', os.O_RDWR | os.O_CREAT)\n"
                "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                "print('locked', flush=True)\n"
                "time.sleep(60)\n",
            ],
            cwd=work,
        )
        try:
            lines = runner.lines()
            assert await anext(lines) == "locked"
            started = time.monotonic()
            with pytest.raises(CheckpointServiceError, match="run works in this session"):
                await service.rollback(org_id=org_id, user_id=None, session_id=session_id, number=1)
            waited_s = time.monotonic() - started
            assert 9 < waited_s < 40
            # The files did not change.
            assert await _files(sandbox) == after
        finally:
            await runner.kill()

        # Timing: a work folder of 1 000 small files. The first snapshot hashes all of them.
        # The next snapshot hashes only the files that changed.
        bulk = await sandbox.exec(
            [
                "sh",
                "-c",
                "for d in $(seq 1 20); do mkdir -p bulk/d$d;"
                ' for f in $(seq 1 50); do echo "$d $f" > bulk/d$d/f$f.txt; done; done',
            ],
            cwd=work,
            timeout_s=120,
        )
        assert bulk.exit_code == 0, bulk.stderr
        cold_s, _ = await _snapshot(sandbox, "run-bulk", "Bash")
        await sandbox.write_file(f"{work}/bulk/d1/f1.txt", b"changed")
        warm_s, warm_commit = await _snapshot(sandbox, "run-bulk", "Bash")
        assert warm_commit != "none"
        started = time.monotonic()
        await service.listing(org_id=org_id, session_id=session_id)
        list_s = time.monotonic() - started
        print(
            f"checkpoint timing in E2B: first small={first_s:.3f}s same-files={again_s:.3f}s"
            f" 1000 files cold={cold_s:.3f}s warm={warm_s:.3f}s"
            f" service list round trip={list_s:.3f}s"
        )
        assert cold_s < 20
        assert warm_s < 5
        assert first_s < 5
    finally:
        await sandbox.kill()
