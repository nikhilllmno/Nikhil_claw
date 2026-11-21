"""``/claude``, ``/codex``, and ``/sama`` in Telegram and Slack, and the progress lines of a coding
agent (M7.2, docs/architecture.md, Section 15.4)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.coding.commands import DIRECT_OFF, DIRECT_ON, NOT_ALLOWED, NOT_DIRECT
from sama.db.models import Membership, Message, Run, RunEvent, Session, SessionBinding
from sama.gateway.sessions import FollowSettings, Reply, coding_progress, follow_run
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

CLAUDE_ON = DIRECT_ON.format(name="Claude Code")
CODEX_ON = DIRECT_ON.format(name="Codex")


async def runs(engine: AsyncEngine, org_id: uuid.UUID) -> list[tuple[str | None, str]]:  # noqa: F811
    """``(coding_agent, text)`` of each run of the org, oldest first."""
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(Run.coding_agent, Message.content)
            .join(Message, (Message.org_id == Run.org_id) & (Message.run_id == Run.id))
            .where(Run.org_id == org_id, Message.role == "user")
            .order_by(Run.created_at)
        )
        return [(row.coding_agent, row.content["text"]) for row in rows]


async def modes(engine: AsyncEngine, org_id: uuid.UUID) -> list[str | None]:  # noqa: F811
    """The direct mode of each session that has a channel binding."""
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(Session.coding_agent).where(
                Session.org_id == org_id, Session.id.in_(await bound_sessions(connection, org_id))
            )
        )
        return [row.coding_agent for row in rows]


async def bound_sessions(connection: Any, org_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await connection.execute(
        select(SessionBinding.session_id).where(SessionBinding.org_id == org_id)
    )
    return [row.session_id for row in rows]


def test_the_help_texts_name_the_commands() -> None:
    assert {"/claude", "/codex", "/sama"} <= OWN_COMMANDS
    for name in ("/claude <task>", "/codex <task>", "/sama - "):
        assert name in HELP
    for name in ("/sama claude TASK", "/sama codex TASK", "/sama sama"):
        assert name in SLACK_HELP


# -- Telegram -----------------------------------------------------------------------------------


async def test_telegram_sends_one_task_to_a_coding_agent(
    channels: Channels,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    gateway = channels.telegram
    await pair_telegram(channels, engine, tenant)
    telegram_tests.no_followers(gateway)

    await gateway.handle_update(tg("/claude fix the tests"))
    await gateway.handle_update(tg("/codex@sama_bot /refactor the module"))
    await gateway.handle_update(tg("/CLAUDE@other_bot do it"))
    assert await runs(engine, tenant.org) == [
        ("claude_code", "fix the tests"),
        ("codex", "/refactor the module"),
        (None, "/CLAUDE@other_bot do it"),
    ], "a command to another bot is a normal message"
    assert await modes(engine, tenant.org) == [None], "one task does not start the direct mode"


async def test_telegram_switches_the_direct_mode(
    channels: Channels,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    gateway = channels.telegram
    await pair_telegram(channels, engine, tenant)
    telegram_tests.no_followers(gateway)

    async def last(text: str) -> str:
        await gateway.handle_update(tg(text))
        return channels.tg_api.texts(TG)[-1]

    assert await last("/sama") == NOT_DIRECT, "the chat has no session yet"
    assert await modes(engine, tenant.org) == []
    assert await last("/claude") == CLAUDE_ON
    assert await modes(engine, tenant.org) == ["claude_code"]
    assert await last("/codex@sama_bot") == CODEX_ON
    assert await modes(engine, tenant.org) == ["codex"]
    assert await last("/sama") == DIRECT_OFF
    assert await modes(engine, tenant.org) == [None]
    assert await last("/sama") == NOT_DIRECT
    assert await runs(engine, tenant.org) == [], "a switch queues no run"


# -- Slack --------------------------------------------------------------------------------------


async def test_slack_commands_in_a_direct_message(
    channels: Channels,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    gateway = channels.slack
    await pair_slack(channels, engine, tenant)
    slack_tests.no_followers(gateway)
    command = slack_tests.command

    assert await gateway.command(command("sama sama")) == NOT_DIRECT
    assert await gateway.command(command("claude")) == CLAUDE_ON
    assert await modes(engine, tenant.org) == ["claude_code"]
    assert await gateway.command(command("codex")) == CODEX_ON
    assert await modes(engine, tenant.org) == ["codex"]
    assert await gateway.command(command("sama")) == DIRECT_OFF
    assert await modes(engine, tenant.org) == [None]

    assert await gateway.command(command("claude fix the tests")) == (
        "sama sent the task to Claude Code."
    )
    assert await gateway.command(command("codex /refactor it")) == "sama sent the task to Codex."
    assert await runs(engine, tenant.org) == [
        ("claude_code", "fix the tests"),
        ("codex", "/refactor it"),
    ]

    answer = await gateway.command(command("claude fix it", channel=slack_tests.CHANNEL))
    assert answer == "Mention the sama app in this channel and write /claude TASK or /codex TASK."
    assert len(await runs(engine, tenant.org)) == 2


async def test_slack_text_with_a_command_goes_to_a_coding_agent(
    channels: Channels,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    gateway = channels.slack
    await pair_slack(channels, engine, tenant)
    slack_tests.no_followers(gateway)
    await slack_tests.link_channel(gateway, engine, tenant)

    await gateway.handle_event(slack_tests.dm("/codex fix the tests"))
    await gateway.handle_event(slack_tests.mention("/claude add a test"))
    await gateway.handle_event(slack_tests.dm("hello"))
    assert await runs(engine, tenant.org) == [
        ("codex", "fix the tests"),
        ("claude_code", "add a test"),
        (None, "hello"),
    ]

    async def active() -> list[str | None]:
        return sorted((m for m in await modes(engine, tenant.org) if m), key=str)

    thread = "1690000000.999999"
    await gateway.handle_event(slack_tests.dm("/claude"))
    assert channels.slack_api.texts(slack_tests.DM)[-1] == CLAUDE_ON
    # A shared thread: a member may not switch it (the rule of the settings console).
    await gateway.handle_event(slack_tests.mention("/codex", thread_ts=thread))
    assert channels.slack_api.texts(slack_tests.CHANNEL)[-1] == NOT_ALLOWED
    assert await active() == ["claude_code"]
    # A workspace admin may.
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership)
            .where(
                Membership.org_id == tenant.org,
                Membership.user_id == tenant.ids["mia"],
                Membership.workspace_id == tenant.ids["ws"],
            )
            .values(role="admin")
        )
    await gateway.handle_event(slack_tests.mention("/codex", thread_ts=thread))
    assert channels.slack_api.texts(slack_tests.CHANNEL)[-1] == CODEX_ON
    assert await active() == ["claude_code", "codex"]
    await gateway.handle_event(slack_tests.mention("/sama", thread_ts=thread))
    assert channels.slack_api.texts(slack_tests.CHANNEL)[-1] == DIRECT_OFF
    assert await active() == ["claude_code"]


# -- progress lines -----------------------------------------------------------------------------


class Box:
    """An outbox that keeps the last text of each message."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, text: str, *, reply_to: Any = None) -> int:
        self.messages.append(text)
        return len(self.messages) - 1

    async def edit_message(self, message_id: int, text: str) -> None:
        self.messages[message_id] = text


def test_a_progress_line_is_for_a_coding_agent_only() -> None:
    assert coding_progress({"subagent": "claude_code", "text": "edited\n a.py"}) == (
        "> Claude Code: edited a.py"
    )
    assert coding_progress({"subagent": "codex", "text": "ran pytest"}) == "> Codex: ran pytest"
    assert coding_progress({"subagent": "codex", "text": "  "}) is None
    assert coding_progress({"subagent": "reviewer", "text": "reading"}) is None
    assert coding_progress({"text": "x"}) is None


async def test_follow_run_shows_the_progress_of_a_coding_agent(
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    session_id = tenant.ids["s1"]
    run_id = await RunQueue(engine).enqueue(org_id=tenant.org, session_id=session_id, text="go")
    events: list[tuple[str, dict[str, Any]]] = [
        ("text_delta", {"text": "Start"}),
        ("subagent_event", {"subagent": "claude_code", "text": "edited main.py"}),
        ("subagent_event", {"subagent": "reviewer", "text": "native sub-agent"}),
        ("subagent_event", {"subagent": "codex", "text": "ran pytest"}),
        ("text_delta", {"text": "Done"}),
    ]
    async with engine.begin() as connection:
        await connection.execute(
            insert(RunEvent),
            [
                {
                    "org_id": tenant.org,
                    "session_id": session_id,
                    "run_id": run_id,
                    "seq": seq,
                    "type": kind,
                    "payload": payload,
                }
                for seq, (kind, payload) in enumerate(events)
            ],
        )
        await connection.execute(update(Run).where(Run.id == run_id).values(status="completed"))
    box = Box()

    async def no_card(row: Any) -> None:
        raise AssertionError("no approval is open")

    status = await follow_run(
        engine,
        tenant.org,
        run_id,
        Reply(box, None, 0),
        FollowSettings(poll_s=0.02),
        no_card,
    )
    assert status == "completed"
    assert box.messages == ["Start\n> Claude Code: edited main.py\n> Codex: ran pytest\nDone"]
