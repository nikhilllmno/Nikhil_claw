"""Skills as slash commands in Slack (docs/architecture.md, Section 9.5)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AgentProfile, ChannelGroup, ChannelIdentity, Message, SessionBinding
from sama.gateway.sessions import FollowSettings
from sama.gateway.slack import HELP, LINK_FIRST, PAIR_PROMPT, SlackApp, SlackGateway
from sama.skills import SkillLibrary
from tests.gateway.test_slack import (
    CHANNEL,
    DM,
    bound,
    command,
    dm,
    link_channel,
    mention,
    no_followers,
    pair,
)
from tests.integrations.support import APP_ID, BOT_USER, SLACK_TOKEN, TEAM, FakeSlack, add_slack
from tests.orchestrator.conftest import (  # noqa: F401
    Tenant,
    engine,
    queue_database_url,
    tenant,
)
from tests.skills.helpers import doc


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
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == tenant.ids["profile"])
            .values(toolsets=["core", "skills"])
        )
    client = slack.client(SecretStr(SLACK_TOKEN))
    integration_id = await add_slack(engine, tenant)
    library = SkillLibrary(
        engine,
        {
            "summarize": doc("summarize", description="Summarize a text"),
            "code-review": doc("code-review", description="Review code"),
        },
    )
    gateway = SlackGateway(
        engine,
        client,
        app=SlackApp(integration_id, tenant.org, TEAM, BOT_USER, APP_ID),
        settings=FollowSettings(poll_s=0.02, paused_poll_s=0.02, edit_interval_s=0),
        skills=library,
    )
    no_followers(gateway)
    yield gateway
    await gateway.aclose()
    await client.aclose()


async def user_texts(engine: AsyncEngine, suffix: str) -> list[str]:  # noqa: F811
    (session,) = await bound(engine, suffix)
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(Message.content["text"].as_string())
            .where(Message.session_id == session.id, Message.role == "user")
            .order_by(Message.seq)
        )
        return list(rows.scalars())


async def test_sama_with_a_skill_name_starts_the_skill_in_a_direct_message(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    answer = await gateway.command(command("code-review my patch"))
    assert answer == "The skill code-review started."
    (text,) = await user_texts(engine, f":dm:{DM}")
    assert text.startswith("my patch\n\n")
    assert 'load the skill "code-review"' in text


async def test_a_skill_command_needs_a_known_skill_and_a_linked_account(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    assert await gateway.command(command("summarize x")) == PAIR_PROMPT
    assert await gateway.command(command("summarize x", channel=CHANNEL)) == LINK_FIRST
    await pair(gateway, engine, tenant)
    assert await gateway.command(command("nothing here")) == HELP
    assert await bound(engine, f":dm:{DM}") == []


async def test_a_channel_command_names_the_mention(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    await link_channel(gateway, engine, tenant)
    answer = await gateway.command(command("summarize the notes", channel=CHANNEL))
    assert "Mention the sama app in this channel and write /summarize" in answer
    assert await bound(engine, f":channel:{CHANNEL}") == []


async def test_a_mention_with_a_skill_command_expands_it(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    await link_channel(gateway, engine, tenant)
    first = mention("/summarize the minutes")
    await gateway.handle_event(first)
    (text,) = await user_texts(engine, f":thread:{CHANNEL}:{first['ts']}")
    assert text.startswith("the minutes\n\n")
    assert 'load the skill "summarize"' in text
    plain = mention("/usr/bin/env is a path")
    await gateway.handle_event(plain)
    assert await user_texts(engine, f":thread:{CHANNEL}:{plain['ts']}") == [
        "/usr/bin/env is a path"
    ]


async def test_a_direct_message_with_slash_text_that_is_no_skill_stays_plain(
    gateway: SlackGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    await gateway.handle_event(dm("/not-a-skill hello"))
    assert await user_texts(engine, f":dm:{DM}") == ["/not-a-skill hello"]
