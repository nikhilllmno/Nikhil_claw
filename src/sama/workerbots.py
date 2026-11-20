"""Worker bots (docs/architecture.md, Section 11.6, M3.10).

A worker bot is an agent profile that is bound to one Slack channel, with a daily budget. A
mention of the app in the channel starts a shared session with the profile of the bot, also
when a group pairing links the channel: the bot wins. The sessions of the bot carry its id
(``sessions.worker_bot_id``), so that the model cost of the bot in one UTC day is the sum of the
model calls of these sessions since 00:00 UTC.

The budget holds at two points:

- The Slack gateway does not start a run when the bot spent its budget today.
- ``sama.tenancy.authority.load_authority`` lowers the cost limit of a running run to the rest
  of the budget, so the model proxy and the tool gate stop a run at the limit.

Only a workspace admin or the org owner (or an org admin) creates, changes, or removes a bot.
Each member of the workspace can list the bots.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import (
    AgentProfile,
    AuditEntry,
    Integration,
    ModelCall,
    Session,
    WorkerBot,
)
from sama.tenancy.access import Grants

NAME_MAX_CHARS: Final = 80
MAX_DAILY_BUDGET_USD: Final = Decimal("10000")
BUDGET_REACHED: Final = "the worker bot reached its daily budget"
BOT_GONE: Final = "the worker bot is disabled or removed"
BUDGET_WHISPER: Final = "This worker bot reached its daily budget. It works again after 00:00 UTC."


class WorkerBotError(ValueError):
    """A request that is not valid. The text is safe to show."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def utc_day_start(now: datetime | None = None) -> datetime:
    """00:00 UTC of the day of ``now``."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


async def spent_today(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    bot_id: uuid.UUID,
    *,
    except_run: uuid.UUID | None = None,
    now: datetime | None = None,
) -> Decimal:
    """The model cost of the sessions of the bot since 00:00 UTC. ``except_run`` leaves out
    the calls of one run."""
    query = (
        select(func.coalesce(func.sum(ModelCall.cost_usd), 0))
        .join(
            Session,
            and_(Session.org_id == ModelCall.org_id, Session.id == ModelCall.session_id),
        )
        .where(
            ModelCall.org_id == org_id,
            Session.worker_bot_id == bot_id,
            ModelCall.created_at >= utc_day_start(now),
        )
    )
    if except_run is not None:
        query = query.where(ModelCall.run_id != except_run)
    return Decimal(await connection.scalar(query) or 0)


@dataclass(frozen=True, slots=True)
class BotLimit:
    """The rest of the budget of a bot for one run. ``problem`` is set when the run cannot
    spend more."""

    problem: str | None
    max_cost_usd: Decimal | None


async def bot_limit(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    bot_id: uuid.UUID,
    run_id: uuid.UUID | None,
    *,
    now: datetime | None = None,
) -> BotLimit:
    """The cost that a run of the bot can still spend today. The run's own calls count
    against this limit; the calls of the other runs of the bot today lower it."""
    row = (
        await connection.execute(
            select(WorkerBot.status, WorkerBot.daily_budget_usd).where(
                WorkerBot.org_id == org_id, WorkerBot.id == bot_id
            )
        )
    ).one_or_none()
    if row is None or row.status != "active":
        return BotLimit(BOT_GONE, None)
    other = await spent_today(connection, org_id, bot_id, except_run=run_id, now=now)
    rest = Decimal(row.daily_budget_usd) - other
    if rest <= 0:
        return BotLimit(BUDGET_REACHED, Decimal(0))
    return BotLimit(None, rest)


# -- admin --------------------------------------------------------------------------------


def view(row: Any, spent: Decimal | None = None) -> dict[str, Any]:
    out = {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id),
        "agent_profile_id": str(row.agent_profile_id),
        "integration_id": str(row.integration_id),
        "channel_id": row.external_id,
        "name": row.name,
        "daily_budget_usd": str(row.daily_budget_usd),
        "status": row.status,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }
    if spent is not None:
        out["spent_today_usd"] = str(spent)
    return out


def may_manage(grants: Grants, workspace_id: uuid.UUID) -> bool:
    """A workspace admin, the org owner, or an org admin."""
    return grants.org_admin or grants.admins_workspace(workspace_id)


async def list_bots(
    engine: AsyncEngine, org_id: uuid.UUID, workspace_id: uuid.UUID
) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(WorkerBot)
                .where(WorkerBot.org_id == org_id, WorkerBot.workspace_id == workspace_id)
                .order_by(WorkerBot.created_at, WorkerBot.id)
            )
        ).all()
        return [view(row, await spent_today(connection, org_id, row.id)) for row in rows]


async def _check_profile(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    profile_id: uuid.UUID,
) -> None:
    profile_workspace = (
        await connection.execute(
            select(AgentProfile.workspace_id).where(
                AgentProfile.org_id == org_id, AgentProfile.id == profile_id
            )
        )
    ).one_or_none()
    if profile_workspace is None or profile_workspace[0] not in (None, workspace_id):
        raise WorkerBotError("the agent profile does not exist in this workspace")


async def _slack_integration(connection: AsyncConnection, org_id: uuid.UUID) -> uuid.UUID:
    integration_id = (
        await connection.execute(
            select(Integration.id).where(
                Integration.org_id == org_id,
                Integration.kind == "slack",
                Integration.status == "active",
            )
        )
    ).scalar_one_or_none()
    if integration_id is None:
        raise WorkerBotError("the org has no active Slack integration", 409)
    return integration_id


async def _audit(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    action: str,
    bot_id: uuid.UUID,
    details: dict[str, Any],
) -> None:
    await connection.execute(
        insert(AuditEntry).values(
            org_id=org_id,
            workspace_id=workspace_id,
            actor_kind="user",
            actor_user_id=user_id,
            action=action,
            target_type="worker_bot",
            target_id=str(bot_id),
            details=details,
        )
    )


@dataclass(frozen=True, slots=True)
class BotFields:
    """The fields of a new bot, or the fields to change (``None`` keeps a field)."""

    name: str | None = None
    agent_profile_id: uuid.UUID | None = None
    channel_id: str | None = None
    daily_budget_usd: Decimal | None = None
    status: str | None = None


async def create_bot(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    fields: BotFields,
) -> dict[str, Any]:
    if (
        fields.name is None
        or fields.agent_profile_id is None
        or fields.channel_id is None
        or fields.daily_budget_usd is None
    ):
        raise WorkerBotError("give name, agent_profile_id, channel_id, and daily_budget_usd")
    bot_id = uuid.uuid4()
    try:
        async with engine.begin() as connection:
            await _check_profile(connection, org_id, workspace_id, fields.agent_profile_id)
            integration_id = await _slack_integration(connection, org_id)
            await connection.execute(
                insert(WorkerBot).values(
                    id=bot_id,
                    org_id=org_id,
                    workspace_id=workspace_id,
                    agent_profile_id=fields.agent_profile_id,
                    integration_id=integration_id,
                    external_id=fields.channel_id,
                    name=fields.name,
                    daily_budget_usd=fields.daily_budget_usd,
                    status=fields.status or "active",
                    created_by_user_id=user_id,
                )
            )
            await _audit(
                connection,
                org_id,
                workspace_id,
                user_id,
                "worker_bot.created",
                bot_id,
                {
                    "channel_id": fields.channel_id,
                    "agent_profile_id": str(fields.agent_profile_id),
                    "daily_budget_usd": str(fields.daily_budget_usd),
                },
            )
            row = (await connection.execute(select(WorkerBot).where(WorkerBot.id == bot_id))).one()
    except IntegrityError:
        raise WorkerBotError("the channel already has a worker bot", 409) from None
    return view(row, Decimal(0))


async def update_bot(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    bot_id: uuid.UUID,
    user_id: uuid.UUID,
    fields: BotFields,
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if fields.name is not None:
        values["name"] = fields.name
    if fields.daily_budget_usd is not None:
        values["daily_budget_usd"] = fields.daily_budget_usd
    if fields.status is not None:
        values["status"] = fields.status
    if fields.agent_profile_id is not None:
        values["agent_profile_id"] = fields.agent_profile_id
    if fields.channel_id is not None:
        values["external_id"] = fields.channel_id
    if not values:
        raise WorkerBotError("send at least one field to change")
    try:
        async with engine.begin() as connection:
            if fields.agent_profile_id is not None:
                await _check_profile(connection, org_id, workspace_id, fields.agent_profile_id)
            row = (
                await connection.execute(
                    update(WorkerBot)
                    .where(
                        WorkerBot.org_id == org_id,
                        WorkerBot.workspace_id == workspace_id,
                        WorkerBot.id == bot_id,
                    )
                    .values(**values, updated_at=func.now())
                    .returning(WorkerBot)
                )
            ).one_or_none()
            if row is None:
                raise WorkerBotError("the worker bot does not exist", 404)
            await _audit(
                connection,
                org_id,
                workspace_id,
                user_id,
                "worker_bot.updated",
                bot_id,
                {"fields": sorted("channel_id" if k == "external_id" else k for k in values)},
            )
            spent = await spent_today(connection, org_id, bot_id)
    except IntegrityError:
        raise WorkerBotError("the channel already has a worker bot", 409) from None
    return view(row, spent)


async def delete_bot(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    bot_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    async with engine.begin() as connection:
        gone = (
            await connection.execute(
                delete(WorkerBot)
                .where(
                    WorkerBot.org_id == org_id,
                    WorkerBot.workspace_id == workspace_id,
                    WorkerBot.id == bot_id,
                )
                .returning(WorkerBot.id)
            )
        ).scalar_one_or_none()
        if gone is None:
            raise WorkerBotError("the worker bot does not exist", 404)
        await _audit(connection, org_id, workspace_id, user_id, "worker_bot.deleted", bot_id, {})


async def channel_bot(
    connection: AsyncConnection, integration_id: uuid.UUID, channel: str
) -> Any | None:
    """The active bot of a Slack channel, or ``None``."""
    return (
        await connection.execute(
            select(
                WorkerBot.id,
                WorkerBot.org_id,
                WorkerBot.workspace_id,
                WorkerBot.agent_profile_id,
            ).where(
                WorkerBot.integration_id == integration_id,
                WorkerBot.external_id == channel,
                WorkerBot.status == "active",
            )
        )
    ).one_or_none()
