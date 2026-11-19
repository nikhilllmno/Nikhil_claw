"""Tests of the Telegram gateway with a real database, a fake Bot API, and the fake runtime."""

from __future__ import annotations

import itertools
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import (
    AuditEntry,
    ChannelGroup,
    ChannelIdentity,
    Membership,
    Run,
    Session,
    SessionBinding,
    User,
)
from sama.gateway.pairing import create_code
from sama.gateway.telegram import PAIR_PROMPT, TelegramGateway, TelegramSettings
from sama.gateway.telegram_client import TelegramClient
from sama.runtime.contract import FailureCode, RunCompleted, RunFailed, TextDelta
from sama.runtime.fake import FakeScript
from tests.orchestrator.conftest import (  # noqa: F401
    Tenant,
    add_tenant,
    engine,
    queue_database_url,
    tenant,
)
from tests.orchestrator.test_worker import Harness, approval_script, until

BOT_ID = 999
SECRET = "webhook-secret-0123456789"
MIA_TG = 1001
DM = MIA_TG
GROUP = -5005


class FakeTelegram:
    """The Bot API: it records calls and gives each sent message a new id."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.messages: dict[int, tuple[int, str]] = {}
        self._ids = itertools.count(100)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if "/file/" in request.url.path:
            return httpx.Response(200, content=b"OGG")
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if method == "sendMessage":
            message_id = next(self._ids)
            self.messages[message_id] = (body["chat_id"], body["text"])
            return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})
        if method == "editMessageText" and body["message_id"] in self.messages:
            self.messages[body["message_id"]] = (body["chat_id"], body["text"])
        if method == "getFile":
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "v.oga"}})
        return httpx.Response(200, json={"ok": True, "result": True})

    def sent(self, chat_id: int | None = None) -> list[dict[str, Any]]:
        return [
            body
            for method, body in self.calls
            if method == "sendMessage" and chat_id in (None, body["chat_id"])
        ]

    def texts(self, chat_id: int | None = None) -> list[str]:
        return [body["text"] for body in self.sent(chat_id)]

    def shown(self, chat_id: int) -> list[str]:
        """The text of each message in the chat, after the edits."""
        return [text for chat, text in self.messages.values() if chat == chat_id]


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
    telegram: FakeTelegram,
    transcriber: FakeTranscriber,
) -> AsyncIterator[TelegramGateway]:
    client = TelegramClient(
        SecretStr(f"{BOT_ID}:token"),
        base_url="https://tg.test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(telegram)),
    )
    gateway = TelegramGateway(
        engine,
        client,
        settings=TelegramSettings(
            bot_username="sama_bot", poll_s=0.02, paused_poll_s=0.02, edit_interval_s=0
        ),
        webhook_secret=SecretStr(SECRET),
        bot_user_id=BOT_ID,
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
    async with engine.begin() as connection:
        code, _ = await create_code(
            connection,
            org_id=tenant.org,
            user_id=tenant.ids[user],
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
                    SessionBinding.external_key == f"chat:{chat}",
                )
            )
        ).one_or_none()


@pytest.fixture(autouse=True)
async def clean_links(engine: AsyncEngine) -> None:  # noqa: F811
    """Telegram ids are global, so each test starts without links."""
    async with engine.begin() as connection:
        await connection.execute(delete(ChannelIdentity))
        await connection.execute(delete(ChannelGroup))
        await connection.execute(delete(SessionBinding).where(SessionBinding.channel == "telegram"))


# -- webhook ------------------------------------------------------------------------------------


async def test_the_webhook_checks_the_secret_and_drops_repeats(
    gateway: TelegramGateway, telegram: FakeTelegram
) -> None:
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(gateway.router())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw.test") as http:
        update = message("hello")
        wrong = await http.post(
            "/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "x"}
        )
        assert wrong.status_code == 401
        missing = await http.post("/telegram/webhook", json=update)
        assert missing.status_code == 401
        headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
        bad = await http.post("/telegram/webhook", content=b"{", headers=headers)
        assert bad.status_code == 400
        for _ in range(2):
            ok = await http.post("/telegram/webhook", json=update, headers=headers)
            assert ok.status_code == 200
    await gateway.drain()
    assert telegram.texts(DM) == [PAIR_PROMPT], "a repeated update runs once"


def test_a_short_webhook_secret_is_refused(engine: AsyncEngine) -> None:  # noqa: F811
    client = TelegramClient(SecretStr("1:t"))
    with pytest.raises(ValueError, match="16 characters"):
        TelegramGateway(
            engine,
            client,
            settings=TelegramSettings(bot_username="b"),
            webhook_secret=SecretStr("short"),
        )


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

    # Another org's user cannot decide.
    other = await add_tenant(engine)
    await pair(gateway, engine, other, sender=4004)
    await gateway.handle_update(
        {
            "update_id": next(_update_ids),
            "callback_query": {"id": "cb0", "from": {"id": 4004}, "data": approve},
        }
    )
    answers = [body for method, body in telegram.calls if method == "answerCallbackQuery"]
    assert answers[-1]["text"] == "You cannot decide this approval."

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
    gateway._settings = gateway._settings.model_copy(update={"max_followers": 1})
    gateway._followers.add(uuid.uuid4())  # the runs below are not followed
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
    gateway._followers.update(uuid.uuid4() for _ in range(200))
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
    gateway._followers.update(uuid.uuid4() for _ in range(200))
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
    other = await add_tenant(engine)
    await pair(gateway, engine, other, sender=8008)
    await gateway.handle_update(message("@sama_bot hi", chat=GROUP, sender=8008))
    assert telegram.texts(GROUP)[-1] == "You cannot use sama in this group."


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
