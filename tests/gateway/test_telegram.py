"""Tests of the Telegram gateway with a real database, a fake Bot API, and the fake runtime."""

from __future__ import annotations

import asyncio
import itertools
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import (
    AgentProfile,
    AuditEntry,
    ChannelGroup,
    ChannelIdentity,
    Membership,
    Run,
    Session,
    SessionBinding,
    User,
)
from sama.gate.approvers import SHARED_REFUSAL
from sama.gateway.pairing import create_code
from sama.gateway.telegram import (
    BUSY,
    PAIR_PROMPT,
    TelegramBot,
    TelegramGateway,
    TelegramSettings,
)
from sama.gateway.telegram_client import TelegramClient
from sama.runtime.contract import FailureCode, RunCompleted, RunFailed, TextDelta
from sama.runtime.fake import FakeScript
from tests.integrations.support import BOT_ID, FakeTelegram, add_bot
from tests.orchestrator.conftest import (  # noqa: F401
    Tenant,
    add_tenant,
    engine,
    queue_database_url,
    tenant,
)
from tests.orchestrator.test_worker import Harness, approval_script, until

MIA_TG = 1001
DM = MIA_TG
GROUP = -5005


class FakeTranscriber:
    def __init__(self, text: str = "hi from a voice note") -> None:
        self.text = text
        self.calls: list[bytes] = []

    async def transcribe(self, audio: bytes, *, filename: str, mime_type: str) -> str:
        self.calls.append(audio)
        return self.text


@pytest.fixture
def telegram() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def transcriber() -> FakeTranscriber:
    return FakeTranscriber()


@pytest.fixture
async def gateway(
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
    transcriber: FakeTranscriber,
) -> AsyncIterator[TelegramGateway]:
    client = TelegramClient(
        SecretStr(f"{BOT_ID}:token"),
        base_url="https://tg.test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(telegram)),
    )
    integration_id = await add_bot(engine, tenant)
    gateway = TelegramGateway(
        engine,
        client,
        bot=TelegramBot(integration_id, tenant.org, "sama_bot", BOT_ID),
        settings=TelegramSettings(poll_s=0.02, paused_poll_s=0.02, edit_interval_s=0),
        transcriber=transcriber,
    )
    yield gateway
    await gateway.aclose()
    await client.aclose()


_update_ids = itertools.count(1)


def message(
    text: str | None,
    *,
    chat: int = DM,
    sender: int = MIA_TG,
    reply_to_bot: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "message_id": next(_update_ids),
        "chat": {"id": chat, "type": "private" if chat > 0 else "supergroup"},
        "from": {"id": sender, "is_bot": False, "first_name": "Mia"},
        **extra,
    }
    if text is not None:
        body["text"] = text
    if reply_to_bot:
        body["reply_to_message"] = {"message_id": 1, "from": {"id": BOT_ID, "is_bot": True}}
    return {"update_id": next(_update_ids), "message": body}


async def code_for(
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    kind: str = "user",
    user: str = "mia",
) -> str:
    if "bot" not in tenant.ids:
        await add_bot(engine, tenant, username=f"other_{uuid.uuid4().hex[:8]}_bot")
    async with engine.begin() as connection:
        code, _ = await create_code(
            connection,
            org_id=tenant.org,
            user_id=tenant.ids[user],
            integration_id=tenant.ids["bot"],
            channel="telegram",
            kind=kind,  # type: ignore[arg-type]
            workspace_id=tenant.ids["ws"],
            agent_profile_id=tenant.ids["profile"],
        )
    return code


async def pair(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    sender: int = MIA_TG,
) -> None:
    code = await code_for(engine, tenant)
    await gateway.handle_update(message(f"/start {code}", chat=sender, sender=sender))


async def bound_session(engine: AsyncEngine, chat: int) -> Any:  # noqa: F811
    async with engine.connect() as connection:
        return (
            await connection.execute(
                select(Session)
                .join(SessionBinding, SessionBinding.session_id == Session.id)
                .where(
                    SessionBinding.channel == "telegram",
                    SessionBinding.external_key.endswith(f":chat:{chat}"),
                )
            )
        ).one_or_none()


def no_followers(gateway: TelegramGateway) -> None:
    """The gateway queues runs but does not follow them."""

    async def skip(
        chat_id: int, target: Any, run_id: uuid.UUID, reply_to: int, place: object
    ) -> None:
        gateway._followers.discard(place)

    gateway._follow_run = skip  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
async def clean_links(engine: AsyncEngine) -> None:  # noqa: F811
    """Each test starts without links."""
    async with engine.begin() as connection:
        await connection.execute(delete(ChannelIdentity))
        await connection.execute(delete(ChannelGroup))
        await connection.execute(delete(SessionBinding).where(SessionBinding.channel == "telegram"))


# -- pairing ------------------------------------------------------------------------------------


async def test_a_user_pairs_with_a_code(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> None:
    await gateway.handle_update(message("/start"))
    assert telegram.texts(DM) == [PAIR_PROMPT]
    code = await code_for(engine, tenant)
    await gateway.handle_update(message(f"{code[:4].lower()}-{code[4:]}"))
    assert "is linked to sama" in telegram.texts(DM)[-1]
    async with engine.connect() as connection:
        identity = (
            await connection.execute(
                select(ChannelIdentity).where(ChannelIdentity.external_id == str(MIA_TG))
            )
        ).one()
        audit = (
            await connection.execute(
                select(AuditEntry.action, AuditEntry.target_id).where(
                    AuditEntry.org_id == tenant.org, AuditEntry.action == "channel_identity.linked"
                )
            )
        ).all()
    assert (identity.user_id, identity.workspace_id) == (tenant.ids["mia"], tenant.ids["ws"])
    assert audit == [("channel_identity.linked", f"telegram:{MIA_TG}")]

    # The code is single-use.
    await gateway.handle_update(message(f"/pair {code}", chat=2002, sender=2002))
    assert "wrong, used, or expired" in telegram.texts(2002)[-1]


async def test_wrong_codes_lock_the_sender(
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
    gateway: TelegramGateway,
) -> None:
    for _ in range(5):
        await gateway.handle_update(message("/start AAAABBBB", chat=3003, sender=3003))
    code = await code_for(engine, tenant)
    await gateway.handle_update(message(f"/start {code}", chat=3003, sender=3003))
    assert telegram.texts(3003)[-1] == "Too many wrong codes. Try again later."
    async with engine.connect() as connection:
        linked = await connection.execute(
            select(ChannelIdentity).where(ChannelIdentity.external_id == "3003")
        )
        assert linked.first() is None
    assert all(code not in text for text in telegram.texts())


# -- direct chats -------------------------------------------------------------------------------


async def test_a_direct_message_runs_in_a_private_session_and_streams(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> None:
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(
        FakeScript(
            events=(TextDelta(text="hel"), TextDelta(text="lo"), RunCompleted(output="hello"))
        )
    )
    await gateway.handle_update(message("hi"))
    session = await bound_session(engine, DM)
    assert (session.audience, session.owner_user_id) == ("private", tenant.ids["mia"])
    assert await harness.worker.run_once()
    await gateway.drain()
    assert telegram.shown(DM)[-1] == "hello"
    assert ("sendChatAction", {"chat_id": DM, "action": "typing"}) in telegram.calls

    # The next message uses the same session.
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="again"),)))
    await gateway.handle_update(message("more"))
    assert (await bound_session(engine, DM)).id == session.id
    assert await harness.worker.run_once()
    await gateway.drain()
    assert telegram.texts(DM)[-1] == "again", "without deltas the stored answer is sent"
    async with engine.connect() as connection:
        config = (
            await connection.execute(select(Session.run_config).where(Session.id == session.id))
        ).scalar_one()
    assert "Channel: Telegram." in config["environment"]


async def test_a_failed_run_is_reported(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> None:
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(
        FakeScript(events=(RunFailed(code=FailureCode.MODEL_ERROR, message="the model broke"),))
    )
    await gateway.handle_update(message("hi"))
    assert await harness.worker.run_once()
    await gateway.drain()
    assert telegram.texts(DM)[-1].startswith("The run failed:")


async def test_an_approval_card_is_decided_with_a_button(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> None:
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(approval_script())
    await gateway.handle_update(message("clean up"))
    assert await harness.worker.run_once()

    async def carded() -> bool:
        return any("reply_markup" in body for body in telegram.sent(DM))

    await until(carded)
    card = next(body for body in telegram.sent(DM) if "reply_markup" in body)
    assert card["text"].startswith("Approval needed: Bash")
    assert "rm -rf build" in card["text"]
    approve = card["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert approve.endswith(":y")

    # A user of another org cannot link to this bot, so cannot decide.
    other = await add_tenant(engine)
    await pair(gateway, engine, other, sender=4004)
    await gateway.handle_update(
        {
            "update_id": next(_update_ids),
            "callback_query": {"id": "cb0", "from": {"id": 4004}, "data": approve},
        }
    )
    answers = [body for method, body in telegram.calls if method == "answerCallbackQuery"]
    assert answers[-1]["text"] == "Link your Telegram account to sama first."

    await gateway.handle_update(
        {
            "update_id": next(_update_ids),
            "callback_query": {
                "id": "cb1",
                "from": {"id": MIA_TG, "first_name": "Mia"},
                "data": approve,
                "message": {"message_id": 101, "chat": {"id": DM}, "text": card["text"]},
            },
        }
    )
    answers = [body for method, body in telegram.calls if method == "answerCallbackQuery"]
    assert answers[-1] == {"callback_query_id": "cb1", "text": "Approved"}
    edits = [body for method, body in telegram.calls if method == "editMessageText"]
    assert edits[-1]["text"].endswith("Approved by Mia.")
    assert edits[-1]["reply_markup"] == {"inline_keyboard": []}
    assert await harness.worker.run_once()
    await gateway.drain()
    assert telegram.texts(DM)[-1] == "deleted"


async def test_commands_new_stop_and_model(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> None:
    await pair(gateway, engine, tenant)
    no_followers(gateway)  # the runs below are not followed
    await gateway.handle_update(message("first"))
    first = await bound_session(engine, DM)

    await gateway.handle_update(message("/stop"))
    assert telegram.texts(DM)[-1] == "Stopped."
    async with engine.connect() as connection:
        statuses = (
            await connection.execute(select(Run.status).where(Run.session_id == first.id))
        ).scalars()
        assert set(statuses) == {"cancelled"}
    await gateway.handle_update(message("/stop"))
    assert telegram.texts(DM)[-1] == "No run is open now."

    await gateway.handle_update(message("/new"))
    second = await bound_session(engine, DM)
    assert second.id != first.id
    assert second.parent_session_id is None

    await gateway.handle_update(message("/model"))
    assert "* p (fake-model)" in telegram.texts(DM)[-1]
    await gateway.handle_update(message("/model nope"))
    assert "No agent profile" in telegram.texts(DM)[-1]
    await gateway.handle_update(message("/model P"))
    third = await bound_session(engine, DM)
    assert (third.parent_session_id, third.lineage) == (second.id, "branch")
    await gateway.handle_update(message("/help"))
    assert "/new" in telegram.texts(DM)[-1]


async def test_a_voice_note_is_transcribed(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    transcriber: FakeTranscriber,
) -> None:
    await pair(gateway, engine, tenant)
    no_followers(gateway)
    await gateway.handle_update(
        message(None, voice={"file_id": "f1", "duration": 3, "mime_type": "audio/ogg"})
    )
    assert transcriber.calls == [b"OGG"]
    session = await bound_session(engine, DM)
    async with engine.connect() as connection:
        run = (await connection.execute(select(Run).where(Run.session_id == session.id))).one()
    assert run.status == "queued"


async def test_voice_notes_are_off_without_a_transcriber(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> None:
    await pair(gateway, engine, tenant)
    gateway._transcriber = None
    await gateway.handle_update(message(None, voice={"file_id": "f1", "duration": 3}))
    assert telegram.texts(DM)[-1] == "Voice notes are off. Send text."
    await gateway.handle_update(message(None, voice={"file_id": "f1", "duration": 900}, chat=DM))
    assert telegram.texts(DM)[-1] == "Voice notes are off. Send text."


async def test_a_user_without_access_is_refused(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> None:
    await pair(gateway, engine, tenant)
    async with engine.begin() as connection:
        await connection.execute(
            delete(Membership).where(
                Membership.user_id == tenant.ids["mia"], Membership.workspace_id.is_not(None)
            )
        )
    await gateway.handle_update(message("hi"))
    assert telegram.texts(DM)[-1] == "You do not have access to this workspace now."
    assert await bound_session(engine, DM) is None


# -- groups -------------------------------------------------------------------------------------


async def test_a_group_is_linked_and_shares_a_session(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> None:
    no_followers(gateway)
    # A stranger and an unlinked group get no session.
    await gateway.handle_update(message("@sama_bot hi", chat=GROUP, sender=7007))
    assert telegram.texts(GROUP) == []
    await pair(gateway, engine, tenant)
    await gateway.handle_update(message("@sama_bot hi", chat=GROUP))
    assert "not linked to sama" in telegram.texts(GROUP)[-1]

    user_code = await code_for(engine, tenant)
    await gateway.handle_update(message(f"/pair@sama_bot {user_code}", chat=GROUP))
    assert "wrong, used, or expired" in telegram.texts(GROUP)[-1], "a user code is not a group code"
    group_code = await code_for(engine, tenant, kind="group")
    await gateway.handle_update(message(f"/pair@sama_bot {group_code}", chat=GROUP))
    assert "This group is linked" in telegram.texts(GROUP)[-1]

    sent = len(telegram.calls)
    await gateway.handle_update(message("just chatting", chat=GROUP))
    await gateway.handle_update(message("/new@other_bot", chat=GROUP))
    assert len(telegram.calls) == sent, "the bot ignores messages that are not for it"
    assert await bound_session(engine, GROUP) is None

    await gateway.handle_update(message("@sama_bot what is up", chat=GROUP))
    session = await bound_session(engine, GROUP)
    assert (session.audience, session.owner_user_id) == ("shared", None)
    await gateway.handle_update(message("and this", chat=GROUP, reply_to_bot=True))
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(Run.id).where(Run.session_id == session.id).order_by(Run.created_at)
            )
        ).all()
        texts = (
            await connection.execute(
                select(AuditEntry.details).where(
                    AuditEntry.org_id == tenant.org, AuditEntry.action == "session.created"
                )
            )
        ).scalars()
        assert {"audience": "shared", "channel": "telegram"} in list(texts)
    assert len(rows) == 2


async def test_a_group_needs_a_paired_sender_from_the_same_org(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> None:
    await pair(gateway, engine, tenant)
    group_code = await code_for(engine, tenant, kind="group")
    await gateway.handle_update(message(f"/pair {group_code}", chat=GROUP, sender=8008))
    assert "Link your own Telegram account first" in telegram.texts(GROUP)[-1]
    await gateway.handle_update(message(f"/pair {group_code}", chat=GROUP))
    # The code of another org is for the bot of that org: it does not work here.
    other = await add_tenant(engine)
    await pair(gateway, engine, other, sender=8008)
    assert "wrong, used, or expired" in telegram.texts(8008)[-1]
    before = len(telegram.texts(GROUP))
    await gateway.handle_update(message("@sama_bot hi", chat=GROUP, sender=8008))
    assert len(telegram.texts(GROUP)) == before, "the bot ignores a sender it does not know"


async def test_a_group_code_is_only_for_its_maker(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> None:
    ben = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(User).values(
                id=ben,
                org_id=tenant.org,
                auth_user_id=uuid.uuid4(),
                email="ben@example.test",
                display_name="ben",
            )
        )
        await connection.execute(
            insert(Membership).values(org_id=tenant.org, user_id=ben, role="member")
        )
        await connection.execute(
            insert(Membership).values(
                org_id=tenant.org, user_id=ben, workspace_id=tenant.ids["ws"], role="member"
            )
        )
    tenant.ids["ben"] = ben
    await pair(gateway, engine, tenant)
    async with engine.begin() as connection:
        code, _ = await create_code(
            connection,
            org_id=tenant.org,
            user_id=ben,
            integration_id=tenant.ids["bot"],
            channel="telegram",
            kind="user",
            workspace_id=tenant.ids["ws"],
            agent_profile_id=tenant.ids["profile"],
        )
    await gateway.handle_update(message(f"/start {code}", chat=9009, sender=9009))
    ben_group = await code_for(engine, tenant, kind="group", user="ben")
    await gateway.handle_update(message(f"/pair {ben_group}", chat=GROUP))
    assert "wrong, used, or expired" in telegram.texts(GROUP)[-1]
    await gateway.handle_update(message(f"/pair {ben_group}", chat=GROUP, sender=9009))
    assert "This group is linked" in telegram.texts(GROUP)[-1]


# -- review fixes -------------------------------------------------------------------------------


async def test_an_unlinked_chat_gets_no_more_output(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> None:
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(approval_script())
    await gateway.handle_update(message("clean up"))
    assert await harness.worker.run_once()

    async def carded() -> bool:
        return any("reply_markup" in body for body in telegram.sent(DM))

    await until(carded)
    card = next(body for body in telegram.sent(DM) if "reply_markup" in body)
    await gateway.handle_update(
        {
            "update_id": next(_update_ids),
            "callback_query": {
                "id": "cb1",
                "from": {"id": MIA_TG, "first_name": "Mia"},
                "data": card["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                "message": {"message_id": 101, "chat": {"id": DM}, "text": card["text"]},
            },
        }
    )
    sent = len(telegram.sent(DM))
    async with engine.begin() as connection:
        await connection.execute(delete(ChannelIdentity))
    assert await harness.worker.run_once()
    await gateway.drain()
    assert len(telegram.sent(DM)) == sent, "the answer does not reach an unlinked chat"
    assert "deleted" not in telegram.shown(DM)


async def test_a_full_gateway_refuses_new_runs(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> None:
    await pair(gateway, engine, tenant)
    places = [object() for _ in range(200)]
    gateway._followers.update(places)
    try:
        await gateway.handle_update(message("hi"))
    finally:
        gateway._followers.difference_update(places)
    assert telegram.texts(DM)[-1] == BUSY
    assert await bound_session(engine, DM) is None, "no session and no run were made"
    assert not gateway._followers


async def test_updates_of_one_chat_run_one_at_a_time(
    gateway: TelegramGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = 0
    most = 0
    order: list[str] = []

    async def slow(message: dict[str, Any]) -> None:
        nonlocal active, most
        active += 1
        most = max(most, active)
        await asyncio.sleep(0.01)
        order.append(message["text"])
        active -= 1

    monkeypatch.setattr(gateway, "_on_message", slow)
    await asyncio.gather(*(gateway.handle_update(message(text)) for text in ("a", "b", "c")))
    assert most == 1
    assert sorted(order) == ["a", "b", "c"]
    assert len(gateway._chat_locks) == 0, "the lock goes away when no update waits"


# -- approvals in a group (Section 11.3) -------------------------------------------------------

BEN_TG = 9009


async def add_ben(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    *,
    role: str = "member",
) -> None:
    """A second member of the workspace, with a linked Telegram account."""
    ben = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(User).values(
                id=ben,
                org_id=tenant.org,
                auth_user_id=uuid.uuid4(),
                email="ben@example.test",
                display_name="ben",
            )
        )
        await connection.execute(
            insert(Membership).values(org_id=tenant.org, user_id=ben, role="member")
        )
        await connection.execute(
            insert(Membership).values(
                org_id=tenant.org, user_id=ben, workspace_id=tenant.ids["ws"], role=role
            )
        )
    tenant.ids["ben"] = ben
    code = await code_for(engine, tenant, user="ben")
    await gateway.handle_update(message(f"/start {code}", chat=BEN_TG, sender=BEN_TG))


async def group_card(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
    harness: Harness,
) -> dict[str, Any]:
    """Mia asks in the linked group. The run pauses for an approval. Gives the card."""
    await pair(gateway, engine, tenant)
    group_code = await code_for(engine, tenant, kind="group")
    await gateway.handle_update(message(f"/pair@sama_bot {group_code}", chat=GROUP))
    harness.runtime.add_script(approval_script())
    await gateway.handle_update(message("@sama_bot clean up", chat=GROUP))
    assert await harness.worker.run_once()

    async def carded() -> bool:
        return any("reply_markup" in body for body in telegram.sent(GROUP))

    await until(carded)
    return next(body for body in telegram.sent(GROUP) if "reply_markup" in body)


def press(card: dict[str, Any], sender: int, name: str, callback_id: str) -> dict[str, Any]:
    return {
        "update_id": next(_update_ids),
        "callback_query": {
            "id": callback_id,
            "from": {"id": sender, "first_name": name},
            "data": card["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
            "message": {"message_id": 101, "chat": {"id": GROUP}, "text": card["text"]},
        },
    }


def last_answer(telegram: FakeTelegram) -> dict[str, Any]:
    return [body for method, body in telegram.calls if method == "answerCallbackQuery"][-1]


async def test_a_group_card_names_who_may_decide_and_refuses_another_member(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> None:
    harness = Harness(engine)
    card = await group_card(gateway, engine, tenant, telegram, harness)
    assert "mia, who asked, or a workspace admin can decide this." in card["text"]
    await add_ben(gateway, engine, tenant)

    await gateway.handle_update(press(card, BEN_TG, "Ben", "cb-ben"))
    answer = last_answer(telegram)
    assert answer["callback_query_id"] == "cb-ben"
    assert answer["text"] == SHARED_REFUSAL, "the member is told why the press does not count"
    assert not [call for call in telegram.calls if call[0] == "editMessageText"]
    assert not await harness.worker.run_once(), "the run still waits for the approval"

    await gateway.handle_update(press(card, MIA_TG, "Mia", "cb-mia"))
    assert last_answer(telegram) == {"callback_query_id": "cb-mia", "text": "Approved"}
    assert await harness.worker.run_once()


async def test_a_workspace_admin_decides_a_group_card(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> None:
    harness = Harness(engine)
    card = await group_card(gateway, engine, tenant, telegram, harness)
    await add_ben(gateway, engine, tenant, role="admin")
    await gateway.handle_update(press(card, BEN_TG, "Ben", "cb-ben"))
    assert last_answer(telegram)["text"] == "Approved"


async def test_a_star_approver_lets_any_member_decide_a_group_card(
    gateway: TelegramGateway,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    telegram: FakeTelegram,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == tenant.ids["profile"])
            .values(policy={"approvers": ["*"]})
        )
    harness = Harness(engine)
    card = await group_card(gateway, engine, tenant, telegram, harness)
    assert "Each member of the workspace can decide this." in card["text"]
    await add_ben(gateway, engine, tenant)
    await gateway.handle_update(press(card, BEN_TG, "Ben", "cb-ben"))
    assert last_answer(telegram)["text"] == "Approved"
