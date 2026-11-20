"""Skills as slash commands in Telegram (docs/architecture.md, Section 9.5)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AgentProfile, Message
from sama.gateway.telegram import HELP, TelegramBot, TelegramGateway, TelegramSettings
from sama.gateway.telegram_client import TelegramClient
from sama.skills import SkillLibrary
from tests.gateway.test_telegram import (  # noqa: F401
    DM,
    GROUP,
    bound_session,
    clean_links,
    code_for,
    message,
    no_followers,
    pair,
)
from tests.integrations.support import BOT_ID, FakeTelegram, add_bot
from tests.orchestrator.conftest import (  # noqa: F401
    Tenant,
    engine,
    queue_database_url,
    tenant,
)
from tests.skills.helpers import doc


@pytest.fixture
def telegram() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
async def gateway(
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> AsyncIterator[TelegramGateway]:
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == tenant.ids["profile"])
            .values(toolsets=["core", "skills"])
        )
    client = TelegramClient(
        SecretStr(f"{BOT_ID}:token"),
        base_url="https://tg.test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(telegram)),
    )
    integration_id = await add_bot(engine, tenant)
    library = SkillLibrary(
        engine,
        {
            "summarize": doc("summarize", description="Summarize a text"),
            "code-review": doc("code-review", description="Review code"),
        },
    )
    gateway = TelegramGateway(
        engine,
        client,
        bot=TelegramBot(integration_id, tenant.org, "sama_bot", BOT_ID),
        settings=TelegramSettings(poll_s=0.02, paused_poll_s=0.02, edit_interval_s=0),
        skills=library,
    )
    no_followers(gateway)
    yield gateway
    await gateway.aclose()
    await client.aclose()


async def user_texts(engine: AsyncEngine, chat: int) -> list[str]:  # noqa: F811
    session = await bound_session(engine, chat)
    assert session is not None
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(Message.content["text"].as_string())
            .where(Message.session_id == session.id, Message.role == "user")
            .order_by(Message.seq)
        )
        return list(rows.scalars())


async def test_a_skill_command_becomes_a_message_that_names_the_skill(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> None:
    await pair(gateway, engine, tenant)
    await gateway.handle_update(message("/summarize the report"))
    await gateway.handle_update(message("/code_review@sama_bot my patch"))
    texts = await user_texts(engine, DM)
    assert len(texts) == 2
    assert texts[0].startswith("the report\n\n")
    assert 'load the skill "summarize"' in texts[0]
    assert texts[1].startswith("my patch\n\n")
    assert 'load the skill "code-review"' in texts[1]
    assert HELP not in telegram.texts(DM)


async def test_an_unknown_command_gets_the_help_and_starts_no_run(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> None:
    await pair(gateway, engine, tenant)
    await gateway.handle_update(message("/nothing at all"))
    assert telegram.texts(DM)[-1] == HELP
    assert await bound_session(engine, DM) is None


async def test_slash_text_that_is_not_a_skill_stays_plain_text(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    await gateway.handle_update(message("/usr/bin/env is a path"))
    await gateway.handle_update(message("/not-a-skill but a hyphen"))
    assert await user_texts(engine, DM) == ["/usr/bin/env is a path", "/not-a-skill but a hyphen"]


async def test_the_skills_command_lists_the_skills(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> None:
    await pair(gateway, engine, tenant)
    await gateway.handle_update(message("/skills"))
    reply = telegram.texts(DM)[-1]
    assert "/code_review - Review code" in reply
    assert "/summarize - Summarize a text" in reply
    assert "/skills" in HELP


async def test_the_skills_toolset_is_needed(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == tenant.ids["profile"])
            .values(toolsets=["core"])
        )
    await pair(gateway, engine, tenant)
    await gateway.handle_update(message("/skills"))
    assert telegram.texts(DM)[-1] == "There are no skills."
    await gateway.handle_update(message("/summarize x"))
    assert telegram.texts(DM)[-1] == HELP


async def test_a_group_needs_a_mention_for_a_plain_skill_text_but_not_for_a_command(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> None:
    await pair(gateway, engine, tenant)
    group_code = await code_for(engine, tenant, kind="group")
    await gateway.handle_update(message(f"/pair@sama_bot {group_code}", chat=GROUP))
    sent = len(telegram.calls)
    # A command for another bot, and chat that is not for this bot, stay ignored.
    await gateway.handle_update(message("/summarize@other_bot text", chat=GROUP))
    await gateway.handle_update(message("summarize this", chat=GROUP))
    assert len(telegram.calls) == sent
    assert await bound_session(engine, GROUP) is None

    await gateway.handle_update(message("/summarize@sama_bot the minutes", chat=GROUP))
    texts = await user_texts(engine, GROUP)
    assert len(texts) == 1
    assert 'load the skill "summarize"' in texts[0]
    # A shared session has a skill index without user skills (Section 9.2).
    session = await bound_session(engine, GROUP)
    assert session.audience == "shared"
