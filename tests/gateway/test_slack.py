"""Tests of the Slack gateway with a real database, a fake Web API, and the fake runtime."""

from __future__ import annotations

import asyncio
import itertools
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.connectors.service import CHAT_WARNING
from sama.db.models import (
    ChannelGroup,
    ChannelIdentity,
    Membership,
    Message,
    Run,
    Session,
    SessionBinding,
    User,
)
from sama.gate.approvers import SHARED_REFUSAL
from sama.gateway.connect import NO_CONNECTIONS
from sama.gateway.pairing import create_code
from sama.gateway.sessions import COMPACTING, FollowSettings
from sama.gateway.slack import BUSY, HELP, LINK_FIRST, PAIR_PROMPT, SlackApp, SlackGateway
from sama.runtime.contract import RunCompleted, TextDelta
from sama.runtime.fake import FakeScript
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import Vault
from tests.connectors.support import WEB, FakeComposio, FakeForm
from tests.connectors.support import service as connections_service
from tests.integrations.support import APP_ID, BOT_USER, SLACK_TOKEN, TEAM, FakeSlack, add_slack
from tests.orchestrator.conftest import (  # noqa: F401
    Tenant,
    engine,
    queue_database_url,
    tenant,
)
from tests.orchestrator.test_worker import Harness, approval_script, until

MIA = "UMIA0001"
BOB = "UBOB0002"
DM = "DMIA0001"
CHANNEL = "CTEAM001"

_ts = itertools.count(1)


def ts() -> str:
    return f"1690000000.{next(_ts):06d}"


def dm(text: str, *, user: str = MIA, channel: str = DM, **extra: Any) -> dict[str, Any]:
    return {
        "type": "message",
        "channel_type": "im",
        "channel": channel,
        "user": user,
        "text": text,
        "ts": ts(),
        **extra,
    }


def mention(
    text: str, *, user: str = MIA, channel: str = CHANNEL, thread_ts: str | None = None
) -> dict[str, Any]:
    event = {
        "type": "app_mention",
        "channel_type": "channel",
        "channel": channel,
        "user": user,
        "text": f"<@{BOT_USER}> {text}",
        "ts": ts(),
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return event


def command(text: str, *, user: str = MIA, channel: str = DM) -> dict[str, str]:
    return {"user_id": user, "channel_id": channel, "text": text, "team_id": TEAM}


@pytest.fixture
def slack() -> FakeSlack:
    return FakeSlack()


@pytest.fixture
async def gateway(
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,
) -> AsyncIterator[SlackGateway]:
    async with engine.begin() as connection:
        await connection.execute(delete(ChannelIdentity))
        await connection.execute(delete(ChannelGroup))
        await connection.execute(delete(SessionBinding).where(SessionBinding.channel == "slack"))
    client = slack.client(SecretStr(SLACK_TOKEN))
    integration_id = await add_slack(engine, tenant)
    gateway = SlackGateway(
        engine,
        client,
        app=SlackApp(integration_id, tenant.org, TEAM, BOT_USER, APP_ID),
        settings=FollowSettings(poll_s=0.02, paused_poll_s=0.02, edit_interval_s=0),
    )
    yield gateway
    await gateway.aclose()
    await client.aclose()


async def code_for(
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    kind: str = "user",
) -> str:
    async with engine.begin() as connection:
        code, _ = await create_code(
            connection,
            org_id=tenant.org,
            user_id=tenant.ids["mia"],
            integration_id=tenant.ids["slack"],
            channel="slack",
            kind=kind,  # type: ignore[arg-type]
            workspace_id=tenant.ids["ws"],
            agent_profile_id=tenant.ids["profile"],
        )
    return code


async def pair(gateway: SlackGateway, engine: AsyncEngine, tenant: Tenant) -> None:  # noqa: F811
    await gateway.handle_event(dm(await code_for(engine, tenant)))


async def link_channel(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    code = await code_for(engine, tenant, "group")
    answer = await gateway.command(command(f"pair {code}", channel=CHANNEL))
    assert answer.startswith("This channel is linked"), answer


async def bound(engine: AsyncEngine, suffix: str) -> list[Any]:  # noqa: F811
    async with engine.connect() as connection:
        return list(
            (
                await connection.execute(
                    select(Session)
                    .join(SessionBinding, SessionBinding.session_id == Session.id)
                    .where(
                        SessionBinding.channel == "slack",
                        SessionBinding.external_key.endswith(suffix),
                    )
                )
            ).all()
        )


def no_followers(gateway: SlackGateway) -> None:
    """The gateway queues runs but does not follow them."""

    async def skip(
        place: Any, target: Any, run_id: uuid.UUID, holder: object, *, via: int | None = None
    ) -> None:
        gateway._followers.discard(holder)

    gateway._follow = skip  # type: ignore[method-assign]


# -- direct messages ----------------------------------------------------------------------------


async def test_a_user_pairs_in_a_direct_message(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,
) -> None:
    await gateway.handle_event(dm("hi"))
    assert slack.texts(DM) == [PAIR_PROMPT]
    code = await code_for(engine, tenant)
    await gateway.handle_event(dm(f"{code[:4].lower()}-{code[4:]}"))
    assert "is linked to sama" in slack.texts(DM)[-1]
    async with engine.connect() as connection:
        identity = (
            await connection.execute(
                select(ChannelIdentity).where(ChannelIdentity.external_id == MIA)
            )
        ).one()
    assert (identity.channel, identity.user_id) == ("slack", tenant.ids["mia"])

    # The code is single-use.
    await gateway.handle_event(dm(code, user=BOB, channel="DBOB0002"))
    assert "wrong, used, or expired" in slack.texts("DBOB0002")[-1]


async def test_a_direct_message_runs_in_a_private_session(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,
) -> None:
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(
        FakeScript(
            events=(
                TextDelta(text="a <!channel> "),
                TextDelta(text="& b"),
                RunCompleted(output="a <!channel> & b"),
            )
        )
    )
    await gateway.handle_event(dm("hi &amp; bye"))
    (session,) = await bound(engine, f":dm:{DM}")
    assert (session.audience, session.owner_user_id) == ("private", tenant.ids["mia"])
    async with engine.connect() as connection:
        text = await connection.scalar(
            select(Message.content).where(Message.session_id == session.id)
        )
    assert text == {"text": "hi & bye"}, "the text of Slack is unescaped"
    assert await harness.worker.run_once()
    await gateway.drain()
    assert slack.shown(DM)[-1] == "a &lt;!channel&gt; &amp; b", "the answer cannot mention"
    async with engine.connect() as connection:
        config = (
            await connection.execute(select(Session.run_config).where(Session.id == session.id))
        ).scalar_one()
    assert "Channel: Slack." in config["environment"]

    # Messages of bots and edits are not for the app.
    before = len(slack.calls)
    await gateway.handle_event(dm("echo", bot_id="B1"))
    await gateway.handle_event(dm("edit", subtype="message_changed"))
    await gateway.handle_event(dm("me", user=BOT_USER))
    assert len(slack.calls) == before


async def test_a_repeated_event_is_handled_once(gateway: SlackGateway, slack: FakeSlack) -> None:
    envelope = {"type": "event_callback", "event_id": "Ev1", "event": dm("hi")}
    gateway.accept(envelope)
    gateway.accept(envelope)
    await gateway.drain()
    assert slack.texts(DM) == [PAIR_PROMPT]


async def test_commands_in_a_direct_message(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    assert await gateway.command(command("")) == HELP
    assert await gateway.command(command("new")) == PAIR_PROMPT
    assert await gateway.command(command("new", channel=CHANNEL)) == LINK_FIRST
    assert await gateway.command(command("new", user="bad id")) == "This command is not complete."
    await pair(gateway, engine, tenant)
    no_followers(gateway)
    await gateway.handle_event(dm("first"))
    (first,) = await bound(engine, f":dm:{DM}")
    assert await gateway.command(command("stop")) == "Stopped."
    assert await gateway.command(command("stop")) == "No run is open now."
    assert await gateway.command(command("new")) == "A new session started."
    (second,) = await bound(engine, f":dm:{DM}")
    assert second.id != first.id


async def test_a_new_session_waits_for_the_message_of_the_chat(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    """/sama new takes the lock of the direct message, so a message at the same time cannot
    bind another session."""
    await pair(gateway, engine, tenant)
    no_followers(gateway)
    async with gateway._locks.hold((DM, None)):
        task = asyncio.create_task(gateway.command(command("new")))
        await asyncio.sleep(0.05)
        assert not task.done()
        assert await bound(engine, f":dm:{DM}") == []
    assert await task == "A new session started."
    (first,) = await bound(engine, f":dm:{DM}")
    await gateway.handle_event(dm("hello"))
    (same,) = await bound(engine, f":dm:{DM}")
    assert same.id == first.id


async def test_a_full_gateway_refuses_new_runs(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,
) -> None:
    await pair(gateway, engine, tenant)
    for index in range(gateway._settings.max_followers):
        gateway._followers.add(index)
    await gateway.handle_event(dm("hi"))
    assert slack.texts(DM)[-1] == BUSY


# -- channels -----------------------------------------------------------------------------------


async def test_a_linked_channel_answers_mentions_in_the_thread(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,
) -> None:
    await pair(gateway, engine, tenant)
    await link_channel(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="hello team"),)))
    first = mention("hi")
    await gateway.handle_event(first)
    (session,) = await bound(engine, f":thread:{CHANNEL}:{first['ts']}")
    assert session.audience == "shared"
    async with engine.connect() as connection:
        text = await connection.scalar(
            select(Message.content).where(Message.session_id == session.id)
        )
    assert text == {"text": "hi"}, "the mention of the app is removed"
    assert await harness.worker.run_once()
    await gateway.drain()
    answer = slack.posted(CHANNEL)[-1]
    assert (answer["text"], answer["thread_ts"]) == ("hello team", first["ts"])

    # A mention in the thread uses the same session; a new mention starts a new thread.
    no_followers(gateway)
    await gateway.handle_event(mention("more", thread_ts=first["ts"]))
    assert len(await bound(engine, f":thread:{CHANNEL}:{first['ts']}")) == 1
    second = mention("other topic")
    await gateway.handle_event(second)
    assert len(await bound(engine, f":thread:{CHANNEL}:{second['ts']}")) == 1
    assert await gateway.command(command("stop", channel=CHANNEL)) == "Stopped."
    async with engine.connect() as connection:
        statuses = await connection.scalars(select(Run.status).where(Run.org_id == tenant.org))
        assert "queued" not in set(statuses), "stop ends the runs of all threads"


async def test_an_unknown_sender_or_channel_gets_a_private_answer(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,
) -> None:
    await gateway.handle_event(mention("hi", user=BOB))
    assert slack.whispers(CHANNEL) == [LINK_FIRST]
    await pair(gateway, engine, tenant)
    await gateway.handle_event(mention("hi"))
    assert "This channel is not linked" in slack.whispers(CHANNEL)[-1]
    assert slack.texts(CHANNEL) == [], "nothing is posted for all to see"
    assert await gateway.command(command("stop", channel=CHANNEL)) == (
        "This channel is not linked to sama."
    )
    # A group code needs a linked sender.
    code = await code_for(engine, tenant, "group")
    assert await gateway.command(command(f"pair {code}", user=BOB, channel=CHANNEL)) == LINK_FIRST


# -- approvals ----------------------------------------------------------------------------------


async def test_an_approval_card_is_decided_with_a_button(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,
) -> None:
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(approval_script())
    await gateway.handle_event(dm("clean up"))
    assert await harness.worker.run_once()

    async def carded() -> bool:
        return any("blocks" in body for body in slack.posted(DM))

    await until(carded)
    card = next(body for body in slack.posted(DM) if "blocks" in body)
    card_ts = next(ts for ts, (_, text) in slack.messages.items() if text == card["text"])
    assert card["text"].startswith("Approval needed: Bash")
    approve = card["blocks"][1]["elements"][0]["value"]

    def click(user: str, value: str = approve) -> dict[str, Any]:
        return {
            "type": "block_actions",
            "team": {"id": TEAM},
            "user": {"id": user},
            "channel": {"id": DM},
            "message": {"ts": card_ts, "blocks": card["blocks"]},
            "actions": [{"action_id": "sama_approve", "value": value}],
        }

    await gateway.handle_action(click(BOB))
    assert slack.whispers(DM)[-1] == "Link your Slack account to sama first."
    await gateway.handle_action(click(MIA, "ap:nothex:y"))
    assert slack.method_calls("chat.update") == [], "a wrong value is dropped"

    await gateway.handle_action(click(MIA))
    update = slack.method_calls("chat.update")[-1]
    assert update["ts"] == card_ts
    assert update["text"].endswith(f"Approved by <@{MIA}>.")
    assert update["blocks"] == [], "the buttons go"
    await gateway.handle_action(click(MIA))
    assert slack.whispers(DM)[-1] == "the approval is already approved"
    assert await harness.worker.run_once()
    await gateway.drain()
    assert slack.texts(DM)[-1] == "deleted"


async def test_an_unlinked_account_gets_no_more_output(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,
) -> None:
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="secret answer"),)))
    await gateway.handle_event(dm("hi"))
    async with engine.begin() as connection:
        await connection.execute(delete(ChannelIdentity).where(ChannelIdentity.external_id == MIA))
    assert await harness.worker.run_once()
    await gateway.drain()
    assert "secret answer" not in slack.texts(DM)


# -- approvals in a channel (Section 11.3) ------------------------------------------------------


async def add_bob(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    *,
    role: str = "member",
) -> None:
    """A second member of the workspace, with a linked Slack account."""
    bob = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(User).values(
                id=bob,
                org_id=tenant.org,
                auth_user_id=uuid.uuid4(),
                email="bob@example.test",
                display_name="bob",
            )
        )
        await connection.execute(
            insert(Membership).values(org_id=tenant.org, user_id=bob, role="member")
        )
        await connection.execute(
            insert(Membership).values(
                org_id=tenant.org, user_id=bob, workspace_id=tenant.ids["ws"], role=role
            )
        )
        code, _ = await create_code(
            connection,
            org_id=tenant.org,
            user_id=bob,
            integration_id=tenant.ids["slack"],
            channel="slack",
            kind="user",
            workspace_id=tenant.ids["ws"],
            agent_profile_id=tenant.ids["profile"],
        )
    await gateway.handle_event(dm(code, user=BOB, channel="DBOB0002"))


async def channel_card(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,
    harness: Harness,
) -> tuple[dict[str, Any], str]:
    """Mia asks in the linked channel. The run pauses for an approval. Gives the card."""
    await pair(gateway, engine, tenant)
    await link_channel(gateway, engine, tenant)
    harness.runtime.add_script(approval_script())
    await gateway.handle_event(mention("clean up"))
    assert await harness.worker.run_once()

    async def carded() -> bool:
        return any("blocks" in body for body in slack.posted(CHANNEL))

    await until(carded)
    card = next(body for body in slack.posted(CHANNEL) if "blocks" in body)
    card_ts = next(ts for ts, (_, text) in slack.messages.items() if text == card["text"])
    return card, card_ts


def press(card: dict[str, Any], card_ts: str, user: str) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "team": {"id": TEAM},
        "user": {"id": user},
        "channel": {"id": CHANNEL},
        "message": {"ts": card_ts, "blocks": card["blocks"]},
        "actions": [
            {"action_id": "sama_approve", "value": card["blocks"][1]["elements"][0]["value"]}
        ],
    }


async def test_a_channel_card_names_who_may_decide_and_refuses_another_member(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,
) -> None:
    harness = Harness(engine)
    card, card_ts = await channel_card(gateway, engine, tenant, slack, harness)
    assert "Mia (who asked) or a workspace or org admin can decide this." in card["text"]
    await add_bob(gateway, engine, tenant)

    await gateway.handle_action(press(card, card_ts, BOB))
    assert slack.whispers(CHANNEL)[-1] == SHARED_REFUSAL
    assert slack.method_calls("chat.update") == [], "the card does not change"
    assert not await harness.worker.run_once(), "the run still waits for the approval"

    await gateway.handle_action(press(card, card_ts, MIA))
    assert slack.method_calls("chat.update")[-1]["text"].endswith(f"Approved by <@{MIA}>.")
    assert await harness.worker.run_once()


async def test_a_workspace_admin_decides_a_channel_card(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,
) -> None:
    harness = Harness(engine)
    card, card_ts = await channel_card(gateway, engine, tenant, slack, harness)
    await add_bob(gateway, engine, tenant, role="admin")
    await gateway.handle_action(press(card, card_ts, BOB))
    assert slack.method_calls("chat.update")[-1]["text"].endswith(f"Approved by <@{BOB}>.")


# -- context: /sama compact and pins (Section 6.3) ----------------------------------------------


async def test_compact_and_pin_commands_in_a_direct_message(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,
) -> None:
    assert await gateway.command(command("compact")) == PAIR_PROMPT
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    assert await gateway.command(command("compact")) == "This conversation has no session yet."
    assert await gateway.command(command("pins")) == "This session has no pins."
    assert await gateway.command(command("pin keep metric")) == (
        "Send a message first. Then you can pin."
    )
    await gateway.handle_event(dm("hello"))
    assert await harness.worker.run_once()
    await gateway.drain()
    assert (await gateway.command(command("pin keep metric units"))).startswith("Pinned.")
    assert await gateway.command(command("pins")) == "Pins:\n1. keep metric units"
    assert await gateway.command(command("compact the plan")) == COMPACTING
    assert "busy" in await gateway.command(command("compact"))
    assert await harness.worker.run_once()
    await gateway.drain()
    assert slack.shown(DM)[-1] == (
        "The context is compacted: 1000 tokens before and 500 tokens now."
    )
    assert await gateway.command(command("unpin 1")) == "Removed."
    assert await gateway.command(command("pins")) == "This session has no pins."


async def test_compact_and_pins_work_only_in_a_direct_message(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    await link_channel(gateway, engine, tenant)
    assert "direct message" in await gateway.command(command("compact", channel=CHANNEL))
    assert "direct message" in await gateway.command(command("pin x", channel=CHANNEL))
    assert gateway._followers == set()


async def test_help_names_the_context_commands(gateway: SlackGateway) -> None:
    assert "compact" in HELP
    assert "pin" in HELP


async def test_connect_gives_a_web_link_only_to_the_sender(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    """``/sama connect <name>`` (Section 10.3): the answer is a link, and only the sender sees
    it. A direct message gets a personal link, a linked channel a workspace link."""
    assert "connect" in HELP
    assert await gateway.command(command("connect gmail")) == PAIR_PROMPT
    await pair(gateway, engine, tenant)
    assert await gateway.command(command("connect gmail")) == NO_CONNECTIONS
    vault = Vault(engine, VaultKey(b"k" * KEY_BYTES))
    gateway._connections = connections_service(engine, vault, FakeComposio(), FakeForm())
    answer = await gateway.command(command("connect github"))
    assert f"{WEB}/connections?workspace={tenant.ids['ws']}&connect=github&scope=personal" in answer
    assert CHAT_WARNING in answer
    assert await gateway.command(command("connect gmail", channel=CHANNEL)) == (
        "This channel is not linked to sama."
    )
    await link_channel(gateway, engine, tenant)
    answer = await gateway.command(command("connect gmail", channel=CHANNEL))
    assert "connect=gmail&scope=workspace" in answer
