"""A connect card and a resumed run in the chat gateways (docs/architecture.md, Section 10.3.4,
M8.1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.connectors.chat import card
from sama.connectors.manifest import Catalog
from sama.connectors.resume import ConnectionResumer
from sama.connectors.service import ConnectionService, ConnectionView
from sama.db.models import AuditEntry, Message
from sama.orchestrator.queue import RunQueue
from sama.runtime.contract import RunCompleted, TextDelta, ToolCallFinished, ToolCallStarted
from sama.runtime.fake import FakeScript
from tests.connectors.support import CATALOG
from tests.gateway import test_telegram as telegram_tests
from tests.gateway.test_busy_controls import TG, Channels, channels, pair_telegram, tg
from tests.orchestrator.conftest import (  # noqa: F401
    Tenant,
    engine,
    queue_database_url,
    tenant,
)
from tests.orchestrator.test_worker import Harness

__all__ = ["channels"]

LINK = "https://connect.composio.dev/link/7"


async def test_telegram_shows_a_connect_button_for_a_sign_in(
    channels: Channels,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    await pair_telegram(channels, engine, tenant)
    await channels.telegram.handle_update(tg("connect my gmail"))
    line = card(
        connector="gmail",
        title="Gmail",
        url=LINK,
        connection_id="c-1",
        state="initiated",
        scope="personal",
    )
    harness = Harness(engine)
    harness.runtime.add_script(
        FakeScript(
            events=(
                ToolCallStarted(call_id="t1", name="mcp__sama__connection_request", arguments={}),
                ToolCallFinished(call_id="t1", output_preview=f"The sign-in is open.\n{line}"),
                TextDelta(text="Tap the button to connect Gmail."),
                RunCompleted(output="Tap the button to connect Gmail."),
            )
        )
    )
    assert await harness.worker.run_once()
    await channels.telegram.drain()
    buttons = [
        body["reply_markup"]["inline_keyboard"]
        for body in channels.tg_api.sent(TG)
        if body.get("reply_markup")
    ]
    assert buttons == [[[{"text": "Connect Gmail", "url": LINK}]]]
    assert any("Tap the button" in text for text in channels.tg_api.shown(TG))


async def test_a_resumed_run_answers_in_the_direct_chat(
    channels: Channels,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    await pair_telegram(channels, engine, tenant)
    await channels.telegram.handle_update(tg("summarize my inbox"))
    session = await telegram_tests.bound_session(engine, TG)
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="I need Gmail."),)))
    assert await harness.worker.run_once()
    await channels.telegram.drain()
    # The sign-in finishes later: sama queues a note and the chat follows the new run.
    run_id = await RunQueue(engine).enqueue(
        org_id=tenant.org,
        session_id=session.id,
        text="(sama) Gmail is connected now.",
        user_id=tenant.ids["mia"],
    )
    followed = await channels.handoffs.follow(
        org_id=tenant.org, session_id=session.id, run_id=run_id, text="Gmail is connected."
    )
    assert followed == 1
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="You have 3 emails."),)))
    assert await harness.worker.run_once()
    await channels.telegram.drain()
    shown = channels.tg_api.shown(TG)
    assert "Gmail is connected." in shown
    assert "You have 3 emails." in shown
    # A shared session or a session without a chat has no follower.
    assert (
        await channels.handoffs.follow(
            org_id=tenant.org, session_id=tenant.ids["s1"], run_id=run_id, text="x"
        )
        == 0
    )


async def test_the_resumer_queues_a_note_and_the_chat_follows_it(
    channels: Channels,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    await pair_telegram(channels, engine, tenant)
    await channels.telegram.handle_update(tg("summarize my inbox"))
    session = await telegram_tests.bound_session(engine, TG)
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="I need Gmail."),)))
    assert await harness.worker.run_once()
    await channels.telegram.drain()
    now = datetime.now(UTC)
    view = ConnectionView(
        id=uuid.uuid4(),
        workspace_id=tenant.ids["ws"],
        connector="gmail",
        kind="composio",
        scope="personal",
        owner_user_id=tenant.ids["mia"],
        state="connected",
        last_error=None,
        last_health_at=now,
        created_at=now,
        updated_at=now,
        fields={},
        has_secret=False,
    )
    resumer = ConnectionResumer(
        engine,
        RunQueue(engine),
        cast("ConnectionService", SimpleNamespace(catalog=Catalog.load(CATALOG))),
        channels.handoffs,
    )
    origin = {"session_id": str(session.id), "user_id": str(tenant.ids["mia"])}
    await resumer(tenant.org, view, origin)
    async with engine.connect() as connection:
        texts = list(
            (
                await connection.execute(
                    select(Message.content["text"].astext).where(Message.session_id == session.id)
                )
            ).scalars()
        )
        audit = (
            await connection.execute(
                select(AuditEntry.action).where(AuditEntry.target_id == str(view.id))
            )
        ).scalar_one()
    assert texts[-1].startswith("(sama) Gmail is connected now (personal connection).")
    assert audit == "connection.resumed"
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="You have 3 emails."),)))
    assert await harness.worker.run_once()
    await channels.telegram.drain()
    shown = channels.tg_api.shown(TG)
    assert "Gmail is connected. sama continues your request." in shown
    assert "You have 3 emails." in shown
    # A broken origin does nothing.
    await resumer(tenant.org, view, {"session_id": "nope"})
