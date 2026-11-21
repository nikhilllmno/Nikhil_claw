"""``/undo`` and ``/rollback`` in Telegram and Slack (M6.4, docs/architecture.md, Section
15.3)."""

from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Run
from sama.gateway.slack import HELP as SLACK_HELP
from sama.gateway.telegram import HELP, OWN_COMMANDS
from sama.orchestrator.checkpoints import BUSY, NO_CHECKPOINTS, CheckpointService
from sama.runtime.checkpoints import Checkpoints
from sama.sandbox.base import Sandbox
from sama.sandbox.local import LocalSandbox
from tests.gateway import test_slack as slack_tests
from tests.gateway import test_telegram as telegram_tests
from tests.gateway.test_busy_controls import (  # noqa: F401
    TG,
    Channels,
    channels,
    pair_slack,
    pair_telegram,
    tg,
)
from tests.orchestrator.conftest import (  # noqa: F401
    Tenant,
    engine,
    queue_database_url,
    tenant,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

ARGV = (sys.executable, "-m", "sama.runtime.checkpoints")
GROUP = telegram_tests.GROUP


@pytest.fixture
def work(tmp_path: Path) -> Path:
    box = LocalSandbox(tmp_path / "box")
    Path(box.workdir).mkdir(parents=True)
    (tmp_path / "box" / "home").mkdir()
    return Path(box.workdir)


def service_for(engine: AsyncEngine, work: Path) -> CheckpointService:  # noqa: F811
    box = LocalSandbox(work.parent)

    async def sandbox(org_id: str, session_id: str) -> Sandbox | None:
        return box

    return CheckpointService(engine, sandbox, argv=ARGV)


def two_runs(work: Path) -> None:
    checkpoints = Checkpoints(str(work))
    (work / "a.txt").write_text("one")
    checkpoints.snapshot("r1", "Write")
    (work / "a.txt").write_text("two")
    checkpoints.snapshot("r2", "Bash")
    (work / "b.txt").write_text("b")


async def end_runs(engine: AsyncEngine, org_id: uuid.UUID) -> None:  # noqa: F811
    async with engine.begin() as connection:
        await connection.execute(update(Run).where(Run.org_id == org_id).values(status="completed"))


def test_the_help_texts_name_the_commands() -> None:
    assert {"/undo", "/rollback"} <= OWN_COMMANDS
    assert "/undo" in HELP
    assert "/rollback" in HELP
    assert "/sama undo" in SLACK_HELP
    assert "/sama rollback" in SLACK_HELP


async def test_telegram_lists_undoes_and_rolls_back(
    channels: Channels,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    work: Path,
) -> None:
    gateway = channels.telegram
    await pair_telegram(channels, engine, tenant)
    telegram_tests.no_followers(gateway)

    async def last(text: str, chat: int = TG) -> str:
        await gateway.handle_update(tg(text, chat=chat))
        return channels.tg_api.texts(chat)[-1]

    assert await last("/undo") == "Checkpoints are not available here."
    gateway._checkpoints = service_for(engine, work)
    assert await last("/undo") == NO_CHECKPOINTS, "no session yet"

    await gateway.handle_update(tg("hello"))
    assert await last("/undo") == BUSY, "the run of hello is open"
    await end_runs(engine, tenant.org)
    assert await last("/rollback") == NO_CHECKPOINTS

    two_runs(work)
    listed = await last("/rollback")
    assert listed.startswith("Checkpoints of the session files, newest first:\n1. ")
    assert "Send /rollback NUMBER to restore one." in listed
    assert await last("/rollback two") == (
        "Write the number of a checkpoint, for example: /rollback 2"
    )
    assert (await last("/undo")).startswith("The session files are back to the checkpoint of ")
    assert not (work / "b.txt").exists()
    assert (await last("/rollback 1")).endswith("To go back, send /rollback 1.")
    assert (work / "b.txt").exists()

    group_code = await telegram_tests.code_for(engine, tenant, kind="group")
    await gateway.handle_update(tg(f"/pair@sama_bot {group_code}", chat=GROUP))
    assert await last("/undo@sama_bot", chat=GROUP) == (
        "Checkpoints work in a direct chat with sama."
    )


async def test_slack_lists_undoes_and_works_only_in_a_direct_message(
    channels: Channels,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    work: Path,
) -> None:
    gateway = channels.slack
    await pair_slack(channels, engine, tenant)
    slack_tests.no_followers(gateway)
    gateway._checkpoints = service_for(engine, work)
    command = slack_tests.command

    assert await gateway.command(command("undo", channel=slack_tests.CHANNEL)) == (
        "Checkpoints work in a direct message with the sama app."
    )
    assert await gateway.command(command("undo")) == NO_CHECKPOINTS

    await gateway.handle_event(slack_tests.dm("hello"))
    await end_runs(engine, tenant.org)
    two_runs(work)
    listed = await gateway.command(command("rollback"))
    assert "Send /sama rollback NUMBER to restore one. /sama undo restores" in listed
    assert await gateway.command(command("rollback x")) == (
        "Write the number of a checkpoint, for example: /sama rollback 2"
    )
    undone = await gateway.command(command("undo"))
    assert undone.endswith("To go back, send /sama rollback 1.")
    assert not (work / "b.txt").exists()
    assert await gateway.command(command("rollback 9")) == "There is no checkpoint 9."
