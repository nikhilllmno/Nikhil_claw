"""Slack: approvals over chat, shared sessions, and worker bots (M3.2, M3.3, M3.10).

docs/architecture.md, Sections 9.4, 11.3, and 11.6."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AgentProfile, Approval, ModelCall, Run, ToolPolicy, WorkerBot
from sama.gateway.sessions import ALWAYS_LABEL, PROPOSAL_ANSWERERS
from sama.gateway.slack import SlackGateway
from sama.memory import MemoryScope, MemoryService, Operation, PostgresMemoryStore, Target
from sama.memory.core import Action
from sama.memory.store import Writer
from sama.orchestrator.queue import RunQueue
from sama.review.apply import ReviewApplier
from sama.review.proposals import PostgresProposalStore
from sama.runtime.contract import RunCompleted, RunRequest
from sama.runtime.fake import FakeScript
from sama.search import SessionSearch
from sama.tenancy.run_token import RunTokenClaims
from sama.workerbots import BUDGET_WHISPER
from tests.gateway.test_slack import (
    BOB,
    CHANNEL,
    DM,
    MIA,
    add_bob,
    bound,
    dm,
    gateway,  # noqa: F401
    link_channel,
    mention,
    no_followers,
    pair,
    slack,  # noqa: F401
)
from tests.integrations.support import TEAM, FakeSlack
from tests.orchestrator.conftest import (  # noqa: F401
    Tenant,
    engine,
    queue_database_url,
    tenant,
)
from tests.orchestrator.test_prompt_assembly import only
from tests.orchestrator.test_worker import Harness, approval_script, until

BOT_CHANNEL = "CBOTS0001"
MEMORY_TOOLSETS = ["core", "todo", "memory"]


async def first_card(fake: FakeSlack, channel: str) -> tuple[dict[str, Any], str]:
    async def carded() -> bool:
        return any("blocks" in body for body in fake.posted(channel))

    await until(carded)
    card = next(body for body in fake.posted(channel) if "blocks" in body)
    card_ts = next(ts for ts, (_, text) in fake.messages.items() if text == card["text"])
    return card, card_ts


def click(
    card: dict[str, Any], card_ts: str, user: str, channel: str, button: int
) -> dict[str, Any]:
    element = card["blocks"][1]["elements"][button]
    return {
        "type": "block_actions",
        "team": {"id": TEAM},
        "user": {"id": user},
        "channel": {"id": channel},
        "message": {"ts": card_ts, "blocks": card["blocks"]},
        "actions": [{"action_id": element["action_id"], "value": element["value"]}],
    }


# -- M3.2: approvals over chat ------------------------------------------------------------------


async def test_always_allow_for_me_writes_a_policy_rule(
    gateway: SlackGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(approval_script())
    await gateway.handle_event(dm("clean up"))
    assert await harness.worker.run_once()
    card, card_ts = await first_card(slack, DM)
    buttons = card["blocks"][1]["elements"]
    assert [button["action_id"] for button in buttons] == [
        "sama_approve",
        "sama_deny",
        "sama_always",
    ]
    assert buttons[2]["text"]["text"] == ALWAYS_LABEL
    assert buttons[2]["value"].endswith(":a")

    await gateway.handle_action(click(card, card_ts, MIA, DM, 2))
    update_call = slack.method_calls("chat.update")[-1]
    assert update_call["text"].endswith(f"Always allowed by <@{MIA}>.")
    async with engine.connect() as connection:
        rules = (
            await connection.execute(
                select(ToolPolicy.user_id, ToolPolicy.tool, ToolPolicy.action).where(
                    ToolPolicy.org_id == tenant.org
                )
            )
        ).all()
    assert [tuple(rule) for rule in rules] == [(tenant.ids["mia"], "Bash", "allow")]
    assert await harness.worker.run_once(), "the run goes on as approved"
    await gateway.drain()
    assert slack.texts(DM)[-1] == "deleted"


async def test_a_click_after_the_timeout_counts_as_deny(
    gateway: SlackGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(approval_script())
    await gateway.handle_event(dm("clean up"))
    assert await harness.worker.run_once()
    card, card_ts = await first_card(slack, DM)
    async with engine.begin() as connection:
        await connection.execute(
            update(Approval)
            .where(Approval.org_id == tenant.org)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    await gateway.handle_action(click(card, card_ts, MIA, DM, 0))
    assert slack.method_calls("chat.update")[-1]["text"].endswith(f"Expired by <@{MIA}>.")
    async with engine.connect() as connection:
        status = await connection.scalar(
            select(Approval.status).where(Approval.org_id == tenant.org)
        )
        rules = await connection.scalar(
            select(ToolPolicy.id).where(ToolPolicy.org_id == tenant.org)
        )
    assert (status, rules) == ("expired", None)
    assert await harness.worker.run_once()
    await gateway.drain()
    assert slack.texts(DM)[-1] == "kept", "an expired approval is a deny"


async def test_a_chat_proposal_card_goes_to_a_workspace_admin(
    gateway: SlackGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,  # noqa: F811
) -> None:
    """Mia (a member) asks the agent in a shared thread to change workspace memory. The change
    waits as a proposal. A workspace admin approves it in Slack, and it applies at once."""
    store = PostgresMemoryStore(engine)
    gateway._reviews = ReviewApplier(engine, store)
    await pair(gateway, engine, tenant)
    await link_channel(gateway, engine, tenant)
    await add_bob(gateway, engine, tenant, role="admin")
    await gateway.handle_event(mention("remember that we ship on fridays"))
    queue = RunQueue(engine)
    run = await queue.claim("w-chat")
    assert run is not None
    claims = RunTokenClaims(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(run.session_id),
        run_id=str(run.run_id),
        user_id=None,
        token_id="t-1",
        expires_at=int(time.time()) + 600,
    )
    service = MemoryService(store, PostgresProposalStore(engine))
    text = await service.write(
        claims, Target.WORKSPACE, [Operation(action=Action.ADD, content="ship on fridays")], None
    )
    assert "must approve it first" in text

    card, card_ts = await first_card(slack, CHANNEL)
    assert PROPOSAL_ANSWERERS["workspace"] in card["text"]
    assert "the agent proposes a change of workspace memory" in card["text"]
    actions = [button["action_id"] for button in card["blocks"][1]["elements"]]
    assert actions == ["sama_approve", "sama_deny"], "a proposal has no always-allow button"

    await gateway.handle_action(click(card, card_ts, MIA, CHANNEL, 0))
    assert "only a workspace admin or the org owner" in slack.whispers(CHANNEL)[-1]
    await gateway.handle_action(click(card, card_ts, BOB, CHANNEL, 0))
    assert slack.method_calls("chat.update")[-1]["text"].endswith(f"Approved by <@{BOB}>.")
    scope = MemoryScope(tenant.org, Target.WORKSPACE, workspace_id=tenant.ids["ws"])
    assert (await store.read(scope)).entries == ("ship on fridays",)
    async with engine.connect() as connection:
        status = await connection.scalar(select(Run.status).where(Run.id == run.run_id))
    assert status == "running", "the proposal does not stop the chat run"


# -- M3.3: shared sessions in Slack -------------------------------------------------------------


async def test_user_memory_does_not_reach_a_shared_thread(
    gateway: SlackGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,  # noqa: F811
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == tenant.ids["profile"])
            .values(toolsets=MEMORY_TOOLSETS, group_toolsets=MEMORY_TOOLSETS)
        )
    store = PostgresMemoryStore(engine)
    for scope, entry in (
        (MemoryScope(tenant.org, Target.USER, user_id=tenant.ids["mia"]), "likes tea"),
        (MemoryScope(tenant.org, Target.WORKSPACE, workspace_id=tenant.ids["ws"]), "uses make"),
    ):
        await store.change(scope, only(entry), writer=Writer("agent"), action="add")
    await pair(gateway, engine, tenant)
    await link_channel(gateway, engine, tenant)
    harness = Harness(engine)
    for _ in range(2):
        harness.runtime.add_script(FakeScript(events=(RunCompleted(output="ok"),)))

    first = mention("hello team")
    await gateway.handle_event(first)
    assert await harness.worker.run_once()
    await gateway.handle_event(dm("hello me"))
    assert await harness.worker.run_once()
    await gateway.drain()
    shared, private = (
        request for request in harness.runtime.requests if isinstance(request, RunRequest)
    )
    assert shared.scope.audience == "shared"
    assert shared.scope.user_id is None, "a shared run has no user"
    assert "uses make" in shared.config.memory_snapshot
    assert "likes tea" not in shared.config.memory_snapshot
    assert private.scope.audience == "private"
    assert "likes tea" in private.config.memory_snapshot

    # session_search of the shared run does not see the private session of the direct message.
    (thread,) = await bound(engine, f":thread:{CHANNEL}:{first['ts']}")
    (direct,) = await bound(engine, f":dm:{DM}")
    claims = RunTokenClaims(
        org_id=shared.scope.org_id,
        workspace_id=shared.scope.workspace_id,
        session_id=shared.scope.session_id,
        run_id=shared.scope.run_id,
        user_id=None,
        token_id="t-1",
        expires_at=int(time.time()) + 600,
    )
    search = SessionSearch(engine)
    found = {hit.session_id for hit in await search.search(claims, None, 50)}
    assert direct.id not in found
    assert tenant.ids["s1"] in found, "it sees the shared sessions of the workspace"
    assert thread.id == uuid.UUID(shared.scope.session_id)


# -- M3.10: worker bots -------------------------------------------------------------------------


async def add_bot(
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    budget: str = "1",
    channel: str = BOT_CHANNEL,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A worker bot in ``channel`` with its own profile. Returns the bot and profile ids."""
    bot_id, profile_id = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(AgentProfile).values(
                id=profile_id,
                org_id=tenant.org,
                workspace_id=tenant.ids["ws"],
                name="support",
                runtime="fake",
                model="fake-model",
                soul="You answer support questions.",
                toolsets=["core"],
                group_toolsets=["core"],
                budget={},
            )
        )
        await connection.execute(
            insert(WorkerBot).values(
                id=bot_id,
                org_id=tenant.org,
                workspace_id=tenant.ids["ws"],
                agent_profile_id=profile_id,
                integration_id=tenant.ids["slack"],
                external_id=channel,
                name="Support bot",
                daily_budget_usd=Decimal(budget),
                status="active",
            )
        )
    return bot_id, profile_id


async def test_a_mention_in_a_bot_channel_runs_the_bot(
    gateway: SlackGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,  # noqa: F811
) -> None:
    bot_id, profile_id = await add_bot(engine, tenant)
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="bot answer"),)))
    first = mention("my order is late", channel=BOT_CHANNEL)
    await gateway.handle_event(first)
    (session,) = await bound(engine, f":thread:{BOT_CHANNEL}:{first['ts']}")
    assert (session.audience, session.worker_bot_id, session.agent_profile_id) == (
        "shared",
        bot_id,
        profile_id,
    )
    assert await harness.worker.run_once()
    await gateway.drain()
    assert slack.posted(BOT_CHANNEL)[-1]["text"] == "bot answer"


async def test_a_bot_that_spent_its_budget_starts_no_run(
    gateway: SlackGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,  # noqa: F811
) -> None:
    await add_bot(engine, tenant, budget="0.50")
    await pair(gateway, engine, tenant)
    no_followers(gateway)
    first = mention("hi", channel=BOT_CHANNEL)
    await gateway.handle_event(first)
    (session,) = await bound(engine, f":thread:{BOT_CHANNEL}:{first['ts']}")
    async with engine.begin() as connection:
        run_id = await connection.scalar(select(Run.id).where(Run.session_id == session.id))
        await connection.execute(
            insert(ModelCall).values(
                call_id=uuid.uuid4(),
                org_id=tenant.org,
                workspace_id=tenant.ids["ws"],
                session_id=session.id,
                run_id=run_id,
                provider="anthropic",
                model="m",
                endpoint="messages",
                status_code=200,
                cost_usd=Decimal("0.50"),
                duration_ms=1,
            )
        )
    await gateway.handle_event(mention("again", channel=BOT_CHANNEL))
    assert slack.whispers(BOT_CHANNEL)[-1] == BUDGET_WHISPER
    async with engine.connect() as connection:
        runs = await connection.scalar(
            select(Run.id).where(Run.org_id == tenant.org, Run.session_id != session.id)
        )
    assert runs is None, "no run starts after the budget is spent"


async def test_a_removed_bot_gets_no_more_output(
    gateway: SlackGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,  # noqa: F811
) -> None:
    bot_id, _ = await add_bot(engine, tenant)
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="secret answer"),)))
    await gateway.handle_event(mention("hi", channel=BOT_CHANNEL))
    async with engine.begin() as connection:
        await connection.execute(delete(WorkerBot).where(WorkerBot.id == bot_id))
    assert await harness.worker.run_once()
    await gateway.drain()
    assert "secret answer" not in slack.texts(BOT_CHANNEL)


async def test_a_bot_bound_later_stops_the_output_of_a_group_run(
    gateway: SlackGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,  # noqa: F811
) -> None:
    """A run of the group link starts. Then a worker bot is bound to the channel. The bot wins,
    so the old run does not post into the channel."""
    await pair(gateway, engine, tenant)
    await link_channel(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="old answer"),)))
    await gateway.handle_event(mention("hi", channel=CHANNEL))
    await add_bot(engine, tenant, channel=CHANNEL)
    assert await harness.worker.run_once()
    await gateway.drain()
    assert "old answer" not in slack.texts(CHANNEL)
