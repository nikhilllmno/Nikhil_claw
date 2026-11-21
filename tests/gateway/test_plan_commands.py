"""``/plan`` in Telegram and Slack (M7.9, docs/architecture.md, Section 15.5)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.coding_mode.service import CodingService
from sama.db.models import Session
from sama.gateway.slack import HELP as SLACK_HELP
from sama.gateway.telegram import HELP, OWN_COMMANDS
from sama.orchestrator.queue import RunQueue
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

GROUP = telegram_tests.GROUP


async def _plan_sessions(engine: AsyncEngine, tenant: Tenant) -> list[bool]:  # noqa: F811
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(Session.plan_mode).where(
                Session.org_id == tenant.org, Session.id != tenant.ids["mine"]
            )
        )
        return [bool(row.plan_mode) for row in rows]


def test_the_help_texts_name_the_command() -> None:
    assert "/plan" in OWN_COMMANDS
    assert "/plan <task>" in HELP
    assert "/sama plan TASK" in SLACK_HELP


async def test_telegram_plan_flow(
    channels: Channels,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    gateway = channels.telegram
    await pair_telegram(channels, engine, tenant)
    telegram_tests.no_followers(gateway)

    async def last(text: str, chat: int = TG) -> str:
        await gateway.handle_update(tg(text, chat=chat))
        return channels.tg_api.texts(chat)[-1]

    assert await last("/plan fix it") == "Coding mode is not available here."
    coding = CodingService(engine, RunQueue(engine))
    gateway._coding_mode = coding
    assert await last("/plan approve") == (
        "This session has no open plan. Send /plan TASK to start one."
    )
    assert await last("/plan add a health route") == "Plan mode is on. sama makes a plan first."
    assert True in await _plan_sessions(engine, tenant)
    async with engine.connect() as connection:
        session_id = (
            await connection.execute(
                select(Session.id).where(Session.org_id == tenant.org, Session.plan_mode)
            )
        ).scalar_one()
    await coding.propose(org_id=tenant.org, session_id=session_id, run_id=None, body="1. route")
    shown = await last("/plan")
    assert "1. route" in shown
    assert "/plan approve" in shown
    assert (await last("/plan approve")).startswith("Plan version 1 is approved.")
    assert not await coding.plan_mode(org_id=tenant.org, session_id=session_id)

    group_code = await telegram_tests.code_for(engine, tenant, kind="group")
    await gateway.handle_update(tg(f"/pair@sama_bot {group_code}", chat=GROUP))
    assert await last("/plan@sama_bot x", chat=GROUP) == (
        "Plan mode works in a direct chat with sama."
    )


async def test_slack_plan_flow(
    channels: Channels,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    gateway = channels.slack
    await pair_slack(channels, engine, tenant)
    slack_tests.no_followers(gateway)
    coding = CodingService(engine, RunQueue(engine))
    gateway._coding_mode = coding
    command = slack_tests.command
    assert await gateway.command(command("plan x", channel=slack_tests.CHANNEL)) == (
        "Plan mode works in a direct message with the sama app."
    )
    assert await gateway.command(command("plan")) == (
        "This session has no open plan. Send /sama plan TASK to start one."
    )
    assert await gateway.command(command("plan fix the bug")) == (
        "Plan mode is on. sama makes a plan first."
    )
    assert await gateway.command(command("plan off")) == "Plan mode is off."
    shown = await gateway.command(command("plan"))
    assert shown.startswith("Plan mode is off.")
    assert "/sama plan TASK" in shown
