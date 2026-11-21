"""The data controls service: export, delete, and the retention setting (docs/architecture.md,
Section 17.3). The API calls it after its permission checks.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from sqlalchemy import insert, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.datacontrols.delete import (
    LAST_OWNER,
    DataBusyError,
    DataNotFoundError,
    LastOwnerError,
    Removed,
    busy,
    delete_org,
    delete_user,
    owner_ids,
    private_sessions,
    request_cancel,
    session_closure,
)
from sama.datacontrols.export import ExportFile, Kind, write_export
from sama.datacontrols.retention import check_days, effective_days, plan_retention
from sama.db.models import (
    AuditEntry,
    Connection,
    Integration,
    Org,
    Run,
    Trigger,
    User,
)
from sama.tenancy.access import Grants

log: Final = logging.getLogger("sama.datacontrols")


class Cleanup(Protocol):
    """Removes the accounts outside sama before a delete: the Composio accounts and trigger
    instances, the connector hosts, the Telegram webhook, and the Slack token. A failure is
    logged. The delete goes on: the rows and the vault secrets go in any case.

    This is the hook for each account outside sama. A new service with such an account adds a
    step that is safe to run again. Billing (M6.6) cancels the Stripe subscription of the org
    in ``before_org_delete``."""

    async def before_user_delete(
        self, org_id: uuid.UUID, user_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> None: ...

    async def before_org_delete(self, org_id: uuid.UUID, actor_user_id: uuid.UUID) -> None: ...


@dataclass(frozen=True, slots=True)
class RetentionView:
    plan: str
    retention_days: int | None
    plan_default_days: int | None
    plan_max_days: int | None
    effective_days: int | None

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


class DataControls:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        cleanup: Cleanup | None = None,
        export_dir: Path | None = None,
    ) -> None:
        self._engine = engine
        self._cleanup = cleanup
        self._export_dir = export_dir

    # -- export -------------------------------------------------------------------------------

    async def export(
        self,
        org_id: uuid.UUID,
        *,
        kind: Kind,
        actor_user_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
    ) -> ExportFile:
        """Writes the export from one snapshot of the database, then writes the audit row."""
        async with self._engine.connect() as connection:
            snapshot = await connection.execution_options(isolation_level="REPEATABLE READ")
            async with snapshot.begin():
                if (
                    kind == "user"
                    and (
                        await snapshot.execute(
                            select(User.id).where(User.org_id == org_id, User.id == user_id)
                        )
                    ).scalar_one_or_none()
                    is None
                ):
                    raise DataNotFoundError("the user does not exist")
                result = await write_export(
                    snapshot, org_id, kind=kind, user_id=user_id, directory=self._export_dir
                )
        try:
            async with self._engine.begin() as connection:
                await _audit(
                    connection,
                    org_id,
                    actor_user_id,
                    "data.exported",
                    "user" if kind == "user" else "org",
                    str(user_id if kind == "user" else org_id),
                    {"kind": kind, "rows": sum(result.tables.values()), "files": result.files},
                )
        except BaseException:
            result.path.unlink(missing_ok=True)
            raise
        return result

    # -- delete -------------------------------------------------------------------------------

    async def delete_user(
        self, org_id: uuid.UUID, user_id: uuid.UUID, *, actor_user_id: uuid.UUID
    ) -> Removed:
        """Deletes the user row and the data of the user. Raises ``LastOwnerError``,
        ``DataBusyError``, or ``DataNotFoundError``."""
        await self._check_user(org_id, user_id)
        if self._cleanup is not None:
            await self._cleanup.before_user_delete(org_id, user_id, actor_user_id)
        return await self._delete(
            org_id,
            lambda connection: delete_user(
                connection, org_id, user_id, actor_user_id=actor_user_id
            ),
        )

    async def delete_org(self, org_id: uuid.UUID, *, actor_user_id: uuid.UUID) -> Removed:
        """Deletes the org and all its data. Raises ``DataBusyError`` or
        ``DataNotFoundError``."""
        async with self._engine.connect() as connection:
            found = (
                await connection.execute(select(Org.id).where(Org.id == org_id))
            ).scalar_one_or_none()
            if found is None:
                raise DataNotFoundError("the org does not exist")
            await self._check_running(connection, org_id)
        if self._cleanup is not None:
            await self._cleanup.before_org_delete(org_id, actor_user_id)
        removed = await self._delete(org_id, lambda connection: delete_org(connection, org_id))
        log.info(
            "an org was deleted",
            extra={"org_id": str(org_id), "sessions": len(removed.sessions)},
        )
        return removed

    async def _check_user(self, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """The checks of ``delete_user`` before the cleanup outside sama, which sama cannot
        undo. The delete does the checks again under its locks."""
        async with self._engine.connect() as connection:
            if await owner_ids(connection, org_id) == {user_id}:
                raise LastOwnerError(LAST_OWNER)
            closure = await session_closure(
                connection, org_id, await private_sessions(connection, org_id, user_id)
            )
            running = list(
                (
                    await connection.execute(
                        select(Run.id).where(
                            Run.org_id == org_id,
                            Run.status == "running",
                            or_(Run.session_id.in_(closure), Run.triggered_by_user_id == user_id),
                        )
                    )
                ).scalars()
            )
            if running:
                await connection.rollback()
                await self._stop(org_id, running)

    async def _check_running(self, connection: AsyncConnection, org_id: uuid.UUID) -> None:
        running = list(
            (
                await connection.execute(
                    select(Run.id).where(Run.org_id == org_id, Run.status == "running")
                )
            ).scalars()
        )
        if running:
            await connection.rollback()
            await self._stop(org_id, running)

    async def _delete(
        self, org_id: uuid.UUID, action: Callable[[AsyncConnection], Awaitable[Removed]]
    ) -> Removed:
        try:
            async with self._engine.begin() as connection:
                removed = await action(connection)
                if removed.running:
                    await connection.rollback()
        except DBAPIError as error:
            if busy(error):
                raise DataBusyError("other work uses the data now; try again soon") from None
            raise
        if removed.running:
            await self._stop(org_id, removed.running)
        return removed

    async def _stop(self, org_id: uuid.UUID, running: list[uuid.UUID]) -> None:
        async with self._engine.begin() as connection:
            await request_cancel(connection, org_id, running)
        raise DataBusyError(
            f"{len(running)} runs are running; sama asked them to stop. Try again in a minute"
        )

    # -- retention ----------------------------------------------------------------------------

    async def retention(self, org_id: uuid.UUID) -> RetentionView:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(Org.plan, Org.retention_days).where(Org.id == org_id)
                )
            ).one_or_none()
        if row is None:
            raise DataNotFoundError("the org does not exist")
        return _view(row.plan, row.retention_days)

    async def set_retention(
        self, org_id: uuid.UUID, days: int | None, *, actor_user_id: uuid.UUID
    ) -> RetentionView:
        """Sets ``orgs.retention_days``. Raises ``RetentionError`` for a value that the plan
        does not allow."""
        async with self._engine.begin() as connection:
            plan = (
                await connection.execute(select(Org.plan).where(Org.id == org_id).with_for_update())
            ).scalar_one_or_none()
            if plan is None:
                raise DataNotFoundError("the org does not exist")
            check_days(plan, days)
            await connection.execute(
                update(Org).where(Org.id == org_id).values(retention_days=days)
            )
            await _audit(
                connection,
                org_id,
                actor_user_id,
                "data.retention_changed",
                "org",
                str(org_id),
                {"retention_days": days},
            )
        return _view(plan, days)


def _view(plan: str, days: int | None) -> RetentionView:
    rule = plan_retention(plan)
    return RetentionView(
        plan=plan,
        retention_days=days,
        plan_default_days=rule.default_days,
        plan_max_days=rule.max_days,
        effective_days=effective_days(plan, days),
    )


async def _audit(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    action: str,
    target_type: str,
    target_id: str,
    details: dict[str, Any],
) -> None:
    await connection.execute(
        insert(AuditEntry).values(
            org_id=org_id,
            actor_kind="user",
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
        )
    )


class ConnectorsLike(Protocol):
    async def delete(
        self, org_id: uuid.UUID, connection_id: uuid.UUID, *, actor: uuid.UUID
    ) -> bool: ...


class TriggersLike(Protocol):
    async def delete(
        self, org_id: uuid.UUID, workspace_id: uuid.UUID, trigger_id: uuid.UUID, grants: Grants
    ) -> None: ...


class IntegrationsLike(Protocol):
    async def disconnect(self, org_id: uuid.UUID, kind: str, user_id: uuid.UUID) -> bool: ...


class ServiceCleanup:
    """``Cleanup`` with the sama services. Each service delete also removes its account
    outside sama: ``ConnectionService.delete`` (the Composio account and the connector host),
    ``TriggerService.delete`` (the Composio trigger instance), and ``Integrations.disconnect``
    (the Telegram webhook and the Slack token)."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        connections: ConnectorsLike | None = None,
        triggers: TriggersLike | None = None,
        integrations: IntegrationsLike | None = None,
    ) -> None:
        self._engine = engine
        self._connections = connections
        self._triggers = triggers
        self._integrations = integrations

    async def before_user_delete(
        self, org_id: uuid.UUID, user_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> None:
        await self._clean(org_id, actor_user_id, user_id)

    async def before_org_delete(self, org_id: uuid.UUID, actor_user_id: uuid.UUID) -> None:
        await self._clean(org_id, actor_user_id, None)
        if self._integrations is None:
            return
        async with self._engine.connect() as connection:
            kinds = list(
                (
                    await connection.execute(
                        select(Integration.kind).where(Integration.org_id == org_id)
                    )
                ).scalars()
            )
        for kind in kinds:
            await _try(
                f"the {kind} integration",
                self._integrations.disconnect(org_id, kind, actor_user_id),
            )

    async def _clean(
        self, org_id: uuid.UUID, actor_user_id: uuid.UUID, user_id: uuid.UUID | None
    ) -> None:
        connections = select(Connection.id).where(Connection.org_id == org_id)
        triggers = select(Trigger.id, Trigger.workspace_id).where(Trigger.org_id == org_id)
        if user_id is not None:
            connections = connections.where(Connection.owner_user_id == user_id)
            triggers = triggers.where(Trigger.owner_user_id == user_id)
        async with self._engine.connect() as connection:
            trigger_rows = (await connection.execute(triggers)).all()
            connection_ids = list((await connection.execute(connections)).scalars())
        # The delete is a system action for the actor. The owner grants pass the trigger
        # service check: the API checked the permission of the delete.
        grants = Grants(user_id=actor_user_id, rows=((None, "owner"),))
        if self._triggers is not None:
            for row in trigger_rows:
                await _try(
                    "a trigger", self._triggers.delete(org_id, row.workspace_id, row.id, grants)
                )
        if self._connections is not None:
            for connection_id in connection_ids:
                await _try(
                    "a connection",
                    self._connections.delete(org_id, connection_id, actor=actor_user_id),
                )


async def _try(what: str, call: Awaitable[Any]) -> None:
    try:
        await call
    except Exception as error:
        # The message of a service error is safe to log: the services redact their secrets.
        log.warning("the cleanup of %s failed: %s", what, type(error).__name__)
