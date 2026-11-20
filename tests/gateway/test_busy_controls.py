"""Busy controls in the chat gateways (M3.9): the queued state, ``/steer``, and ``/handoff``
(docs/architecture.md, Sections 4.3 and 11.1)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import (
    AuditEntry,
    ChannelGroup,
    ChannelIdentity,
    Message,
    Run,
    RunSteer,
    SessionBinding,
    SessionHandoff,
)
from sama.gateway.handoff import USAGE, Handoffs, moved_away
from sama.gateway.sessions import STEER_EMPTY, STEERED, FollowSettings, queued_text
from sama.gateway.slack import HELP as SLACK_HELP
from sama.gateway.slack import SlackApp, SlackGateway
from sama.gateway.telegram import HELP, TelegramBot, TelegramGateway, TelegramSettings
from sama.gateway.telegram_client import TelegramClient
from sama.orchestrator.queue import RunQueue
from sama.runtime.contract import RunCompleted, Usage
from sama.runtime.fake import FakeScript
from tests.gateway import test_slack as slack_tests
from tests.gateway import test_telegram as telegram_tests
from tests.integrations.support import (
    APP_ID,
    BOT_ID,
    BOT_USER,
    SLACK_TOKEN,
    TEAM,
    FakeSlack,
    FakeTelegram,
    add_bot,
    add_slack,
)
from tests.orchestrator.conftest import (  # noqa: F401
    Tenant,
    engine,
    queue_database_url,
    tenant,
)
from tests.orchestrator.test_worker import Harness

TG = telegram_tests.DM
SDM = slack_tests.DM
WEB_URL = "https://app.test"


@dataclass
class Channels:
    telegram: TelegramGateway
    slack: SlackGateway
    tg_api: FakeTelegram
    slack_api: FakeSlack
    handoffs: Handoffs


@pytest.fixture
async def channels(
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> AsyncIterator[Channels]:
    async with engine.begin() as connection:
        await connection.execute(delete(ChannelIdentity))
        await connection.execute(delete(ChannelGroup))
    tg_api = FakeTelegram()
    slack_api = FakeSlack()
    tg_client = TelegramClient(
        SecretStr(f"{BOT_ID}:token"),
        base_url="https://tg.test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(tg_api)),
    )
    slack_client = slack_api.client(SecretStr(SLACK_TOKEN))
    bot_id = await add_bot(engine, tenant)
    slack_id = await add_slack(engine, tenant)
    handoffs = Handoffs(engine, web_url=WEB_URL)
    telegram = TelegramGateway(
        engine,
        tg_client,
        bot=TelegramBot(bot_id, tenant.org, "sama_bot", BOT_ID),
        settings=TelegramSettings(poll_s=0.02, paused_poll_s=0.02, edit_interval_s=0),
        handoffs=handoffs,
    )
    slack = SlackGateway(
        engine,
        slack_client,
        app=SlackApp(slack_id, tenant.org, TEAM, BOT_USER, APP_ID),
        settings=FollowSettings(poll_s=0.02, paused_poll_s=0.02, edit_interval_s=0),
        handoffs=handoffs,
    )

    async def telegram_door(integration_id: uuid.UUID) -> TelegramGateway | None:
        return telegram if integration_id == bot_id else None

    async def slack_door(integration_id: uuid.UUID) -> SlackGateway | None:
        return slack if integration_id == slack_id else None

    handoffs.register("telegram", telegram_door)
    handoffs.register("slack", slack_door)
    yield Channels(telegram, slack, tg_api, slack_api, handoffs)
    await telegram.aclose()
    await slack.aclose()
    await tg_client.aclose()
    await slack_client.aclose()


async def pair_telegram(
    channels: Channels,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    await telegram_tests.pair(channels.telegram, engine, tenant)


async def pair_slack(
    channels: Channels,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    await slack_tests.pair(channels.slack, engine, tenant)


def tg(text: str, *, chat: int = TG) -> dict[str, Any]:
    return telegram_tests.message(text, chat=chat)


async def bindings(engine: AsyncEngine, session_id: uuid.UUID) -> set[tuple[str, str]]:  # noqa: F811
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(SessionBinding.channel, SessionBinding.external_key).where(
                SessionBinding.session_id == session_id
            )
        )
        return {(row.channel, row.external_key) for row in rows}


async def claim(engine: AsyncEngine) -> Any:  # noqa: F811
    run = await RunQueue(engine).claim("test-owner")
    assert run is not None
    return run


# -- the queued state and /steer ----------------------------------------------------------------


async def test_a_message_behind_an_open_run_says_it_is_queued(
    channels: Channels,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    await pair_telegram(channels, engine, tenant)
    telegram_tests.no_followers(channels.telegram)
    await channels.telegram.handle_update(tg("one"))
    assert queued_text("/steer", "/stop") not in channels.tg_api.texts(TG)
    await channels.telegram.handle_update(tg("two"))
    assert channels.tg_api.texts(TG)[-1] == queued_text("/steer", "/stop")

    await pair_slack(channels, engine, tenant)
    slack_tests.no_followers(channels.slack)
    await channels.slack.handle_event(slack_tests.dm("one"))
    await channels.slack.handle_event(slack_tests.dm("two"))
    assert channels.slack_api.texts(SDM)[-1] == queued_text("/sama steer", "/sama stop")


async def test_telegram_steer_reaches_the_running_run_and_the_rest_runs_next(
    channels: Channels,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    await pair_telegram(channels, engine, tenant)
    await channels.telegram.handle_update(tg("plan my trip"))
    run = await claim(engine)
    await channels.telegram.handle_update(tg("/steer"))
    assert channels.tg_api.texts(TG)[-1] == STEER_EMPTY.format(command="/steer")
    await channels.telegram.handle_update(tg("/steer use metric units"))
    assert channels.tg_api.texts(TG)[-1] == STEERED
    async with engine.connect() as connection:
        notes = (
            await connection.execute(select(RunSteer.text, RunSteer.status, RunSteer.run_id))
        ).all()
    assert [(note.text, note.status, note.run_id) for note in notes] == [
        ("use metric units", "pending", run.run_id)
    ]

    # The run ends before a tool call reads the note: the note runs next, in the same chat.
    queue = RunQueue(engine)
    assert await queue.complete(
        run, "test-owner", output="first answer", runtime_state=None, usage=Usage()
    )
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="metric answer"),)))
    assert await harness.worker.run_once()
    await channels.telegram.drain()
    shown = channels.tg_api.texts(TG)
    assert "first answer" in shown
    assert shown[-1] == "metric answer", "the follower goes on with the run of the note"


async def test_telegram_steer_without_an_open_run_is_a_message(
    channels: Channels,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    await pair_telegram(channels, engine, tenant)
    telegram_tests.no_followers(channels.telegram)
    await channels.telegram.handle_update(tg("/steer hello there"))
    session = await telegram_tests.bound_session(engine, TG)
    assert session is not None
    async with engine.connect() as connection:
        texts = (
            await connection.scalars(
                select(Message.content).where(
                    Message.session_id == session.id, Message.role == "user"
                )
            )
        ).all()
    assert texts == [{"text": "hello there"}]
    assert STEERED not in channels.tg_api.texts(TG)
    assert "/steer" in HELP
    assert "/handoff" in HELP


async def test_slack_steer_in_a_direct_message_and_in_a_thread(
    channels: Channels,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    gateway = channels.slack
    await pair_slack(channels, engine, tenant)
    slack_tests.no_followers(gateway)
    assert await gateway.command(slack_tests.command("steer")) == STEER_EMPTY.format(
        command="/sama steer"
    )
    assert (
        await gateway.command(slack_tests.command("steer a note", channel=slack_tests.CHANNEL))
        == "In a thread, mention the sama app and write /steer TEXT."
    )
    await gateway.handle_event(slack_tests.dm("hello"))
    run = await claim(engine)
    assert await gateway.command(slack_tests.command("steer be short")) == STEERED
    await gateway.handle_event(slack_tests.dm("/steer and friendly"))
    assert channels.slack_api.texts(SDM)[-1] == STEERED
    async with engine.connect() as connection:
        notes = (
            await connection.scalars(
                select(RunSteer.text)
                .where(RunSteer.run_id == run.run_id)
                .order_by(RunSteer.created_at)
            )
        ).all()
    assert notes == ["be short", "and friendly"]

    # Without an open run, the note starts a run as a normal message.
    assert await gateway.command(slack_tests.command("stop")) == "Stopped."
    answer = await gateway.command(slack_tests.command("steer a new question"))
    assert answer == "No run is open. sama takes the note as a new message."
    async with engine.connect() as connection:
        open_runs = await connection.scalar(
            select(func.count())
            .select_from(Run)
            .where(Run.status == "queued", Run.org_id == tenant.org)
        )
    assert open_runs == 1
    assert "steer" in SLACK_HELP
    assert "handoff" in SLACK_HELP


# -- /handoff -----------------------------------------------------------------------------------


async def test_a_session_moves_from_telegram_to_slack_with_its_open_run_and_back(
    channels: Channels,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    await pair_telegram(channels, engine, tenant)
    await pair_slack(channels, engine, tenant)
    await channels.telegram.handle_update(tg("write a long report"))
    session = await telegram_tests.bound_session(engine, TG)
    telegram_key = f"{tenant.ids['bot']}:chat:{TG}"
    assert await bindings(engine, session.id) == {("telegram", telegram_key)}

    await channels.telegram.handle_update(tg("/handoff slack"))
    assert channels.tg_api.texts(TG)[-1].startswith("The session moved to Slack.")
    assert channels.slack_api.method_calls("conversations.open") == [{"users": slack_tests.MIA}]
    moved = await bindings(engine, session.id)
    assert [channel for channel, _ in moved] == ["slack"]
    assert channels.slack_api.texts(SDM)[-1].startswith("This chat now continues")

    # The open run answers in Slack, not in Telegram.
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="the report"),)))
    assert await harness.worker.run_once()
    await channels.telegram.drain()
    await channels.slack.drain()
    assert channels.slack_api.shown(SDM)[-1] == "the report"
    assert "the report" not in channels.tg_api.shown(TG)

    # A Slack message goes on in the same session, and the session can go back.
    slack_tests.no_followers(channels.slack)
    await channels.slack.handle_event(slack_tests.dm("one more thing"))
    (slack_session,) = await slack_tests.bound(engine, f":dm:{SDM}")
    assert slack_session.id == session.id
    answer = await channels.slack.command(slack_tests.command("handoff telegram"))
    assert answer.startswith("The session moved to Telegram.")
    assert await bindings(engine, session.id) == {("telegram", telegram_key)}

    async with engine.connect() as connection:
        handoffs = (
            await connection.execute(
                select(SessionHandoff.from_channel, SessionHandoff.to_channel)
                .where(SessionHandoff.session_id == session.id)
                .order_by(SessionHandoff.id)
            )
        ).all()
        audit = (
            await connection.scalars(
                select(AuditEntry.details).where(
                    AuditEntry.action == "session.handoff",
                    AuditEntry.target_id == str(session.id),
                )
            )
        ).all()
    assert [tuple(row) for row in handoffs] == [("telegram", "slack"), ("slack", "telegram")]
    assert {(entry["from_channel"], entry["to_channel"]) for entry in audit} == {
        ("telegram", "slack"),
        ("slack", "telegram"),
    }


async def test_a_handoff_to_the_web_app_and_the_refusals(
    channels: Channels,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    gateway = channels.telegram
    await pair_telegram(channels, engine, tenant)
    telegram_tests.no_followers(gateway)

    async def last(text: str) -> str:
        await gateway.handle_update(tg(text))
        return channels.tg_api.texts(TG)[-1]

    assert await last("/handoff web") == "This chat has no session to move."
    await gateway.handle_update(tg("hello"))
    session = await telegram_tests.bound_session(engine, TG)
    assert await last("/handoff") == USAGE.format(command="/handoff")
    assert await last("/handoff mars") == "sama cannot move a session to mars."
    assert await last("/handoff telegram") == "The session is in Telegram now."
    assert (await last("/handoff slack")).startswith("Link your Slack account to sama first")
    assert (
        await last("/handoff web") == f"The session moved to the web app: {WEB_URL}/s/{session.id}"
    )
    assert await bindings(engine, session.id) == set()

    # A new message in the old chat starts a new session.
    await gateway.handle_update(tg("again"))
    fresh = await telegram_tests.bound_session(engine, TG)
    assert fresh.id != session.id


async def test_a_handoff_needs_an_active_door_and_a_direct_chat(
    channels: Channels,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    await pair_telegram(channels, engine, tenant)
    await pair_slack(channels, engine, tenant)
    telegram_tests.no_followers(channels.telegram)
    await channels.telegram.handle_update(tg("hello"))
    session = await telegram_tests.bound_session(engine, TG)

    async def closed(integration_id: uuid.UUID) -> None:
        return None

    channels.handoffs.register("slack", closed)
    await channels.telegram.handle_update(tg("/handoff slack"))
    assert channels.tg_api.texts(TG)[-1] == "The Slack app of sama is not available now."
    assert len(await bindings(engine, session.id)) == 1, "a refused handoff changes nothing"

    channels.slack_api.failing.add("conversations.open")
    channels.handoffs.register("slack", lambda _id: _door(channels.slack))
    await channels.telegram.handle_update(tg("/handoff slack"))
    assert channels.tg_api.texts(TG)[-1].startswith("sama cannot open your Slack chat now.")

    assert (
        await channels.slack.command(
            slack_tests.command("handoff web", channel=slack_tests.CHANNEL)
        )
        == "Only a direct message with the sama app can move its session."
    )
    assert (
        await channels.slack.command(slack_tests.command("handoff web"))
        == "This conversation has no session to move."
    )


async def _door(gateway: SlackGateway) -> SlackGateway:
    return gateway


# -- the owner of the output of a run after handoffs -------------------------------------------


async def run_report(engine: AsyncEngine, channels: Channels) -> None:  # noqa: F811
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="the report"),)))
    assert await harness.worker.run_once()
    await channels.telegram.drain()
    await channels.slack.drain()


async def test_a_round_trip_handoff_sends_the_answer_once(
    channels: Channels,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    await pair_telegram(channels, engine, tenant)
    await pair_slack(channels, engine, tenant)
    await channels.telegram.handle_update(tg("write a long report"))
    await channels.telegram.handle_update(tg("/handoff slack"))
    answer = await channels.slack.command(slack_tests.command("handoff telegram"))
    assert answer.startswith("The session moved to Telegram.")

    # The first follower and the follower of the return handoff are both in Telegram. Only the
    # follower of the newest handoff sends.
    await run_report(engine, channels)
    assert channels.tg_api.shown(TG).count("the report") == 1
    assert "the report" not in channels.slack_api.shown(SDM)


async def test_a_full_target_still_follows_the_runs_of_a_handoff(
    channels: Channels,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    await pair_telegram(channels, engine, tenant)
    await pair_slack(channels, engine, tenant)
    await channels.telegram.handle_update(tg("write a long report"))
    busy = [object() for _ in range(channels.slack._settings.max_followers)]
    channels.slack._followers.update(busy)
    await channels.telegram.handle_update(tg("/handoff slack"))
    await run_report(engine, channels)
    assert channels.slack_api.shown(SDM)[-1] == "the report"
    assert "the report" not in channels.tg_api.shown(TG)
    channels.slack._followers.difference_update(busy)


async def test_only_the_follower_of_the_newest_handoff_of_a_run_sends(
    channels: Channels,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    await pair_telegram(channels, engine, tenant)
    await pair_slack(channels, engine, tenant)
    telegram_tests.no_followers(channels.telegram)
    slack_tests.no_followers(channels.slack)
    await channels.telegram.handle_update(tg("a quick question"))
    session = await telegram_tests.bound_session(engine, TG)
    async with engine.connect() as connection:
        first = await connection.scalar(select(Run.id).where(Run.session_id == session.id))
    assert first is not None

    # The run ends before the handoff: the handoff does not take it, so its first follower in
    # the old chat still sends the answer.
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="done"),)))
    assert await harness.worker.run_once()
    await channels.telegram.handle_update(tg("/handoff slack"))
    await channels.slack.handle_event(slack_tests.dm("one more thing"))
    async with engine.connect() as connection:
        second = await connection.scalar(
            select(Run.id).where(Run.session_id == session.id, Run.id != first)
        )
        assert second is not None
        assert not await moved_away(connection, tenant.org, first, None)
        assert not await moved_away(connection, tenant.org, second, None)

    # The run is open at the next handoff: only the follower of that handoff sends.
    await channels.slack.command(slack_tests.command("handoff telegram"))
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(SessionHandoff.id, SessionHandoff.run_ids)
                .where(SessionHandoff.session_id == session.id)
                .order_by(SessionHandoff.id)
            )
        ).all()
        assert [row.run_ids for row in rows] == [[], [second]]
        back = rows[-1].id
        assert await moved_away(connection, tenant.org, second, None)
        assert await moved_away(connection, tenant.org, second, rows[0].id)
        assert not await moved_away(connection, tenant.org, second, back)


async def test_the_run_of_late_notes_answers_only_in_the_new_chat(
    channels: Channels,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    await pair_telegram(channels, engine, tenant)
    await pair_slack(channels, engine, tenant)
    await channels.telegram.handle_update(tg("plan my trip"))
    run = await claim(engine)
    queue = RunQueue(engine)
    assert await queue.add_events(run, "test-owner", [(0, "text_delta", {"text": "partial"})])
    # The Telegram follower shows the text before the handoff.
    for _ in range(200):
        if "partial" in channels.tg_api.shown(TG):
            break
        await asyncio.sleep(0.01)
    assert "partial" in channels.tg_api.shown(TG)

    await channels.telegram.handle_update(tg("/handoff slack"))
    assert await channels.slack.command(slack_tests.command("steer use metric units")) == STEERED
    # The run ends before a tool call reads the note: the note runs next, only in Slack.
    assert await queue.complete(
        run, "test-owner", output="partial", runtime_state=None, usage=Usage()
    )
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="metric answer"),)))
    assert await harness.worker.run_once()
    await channels.telegram.drain()
    await channels.slack.drain()
    assert "metric answer" in channels.slack_api.shown(SDM)
    assert "metric answer" not in channels.tg_api.shown(TG)
