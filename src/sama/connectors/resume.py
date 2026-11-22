"""The end of a sign-in from the chat (docs/architecture.md, Section 10.3.4, M8.1).

When a connection that ``connection_request`` started connects, ``ConnectionResumer`` writes a
note into the session that asked and queues a short run, so the agent continues the request of
the user without a "tell me when you are done". The web app shows the new run at once. When the
session is bound to the direct chat of the user in Telegram or Slack, the gateway of that chat
follows the run and sends its answer there (``Handoffs.follow``).

``SigninPoller`` checks the open Composio sign-ins of the chats every few seconds, so a sign-in
connects and continues also when the browser never reaches the return page.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final, Protocol

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from sama.connectors.chat import resume_text
from sama.db.models import AuditEntry
from sama.orchestrator.queue import QueueError, RunQueue

if TYPE_CHECKING:
    from sama.connectors.service import ConnectionService, ConnectionView

log: Final = logging.getLogger("sama.connectors.resume")

POLL_S: Final = 5.0
"""How often ``SigninPoller`` checks the open sign-ins of the chats."""


class Follower(Protocol):
    """Sends the answer of a run to the chat of a session (``Handoffs.follow``)."""

    async def follow(
        self, *, org_id: uuid.UUID, session_id: uuid.UUID, run_id: uuid.UUID, text: str
    ) -> int: ...


class ConnectionResumer:
    """``ConnectionService.on_connected``: continues the session that asked."""

    def __init__(
        self,
        queue: RunQueue,
        connections: ConnectionService,
        follower: Follower | None = None,
    ) -> None:
        self._queue = queue
        self._connections = connections
        self._follower = follower

    async def queue(
        self,
        connection: AsyncConnection,
        org_id: uuid.UUID,
        view: ConnectionView,
        origin: Mapping[str, str],
    ) -> uuid.UUID | None:
        """Queues the note in the transaction of the caller (it removes the origin). A session
        that cannot take a run gives ``None``; the origin goes anyway."""
        try:
            session_id = uuid.UUID(origin["session_id"])
            user_id = uuid.UUID(origin["user_id"]) if origin.get("user_id") else None
        except (KeyError, ValueError):
            log.warning("connection=%s has a broken origin", view.id)
            return None
        try:
            # A savepoint: a refused run does not undo the removal of the origin.
            async with connection.begin_nested():
                # The note is not a message of a person: it does not count for the message rate.
                run_id = await self._queue.enqueue_in(
                    connection,
                    org_id=org_id,
                    session_id=session_id,
                    text=resume_text(self._title(view), view.scope),
                    user_id=user_id,
                    check_rate=False,
                )
        except QueueError as error:
            # An archived session or a quota: the connection stays connected.
            log.info("connection=%s did not continue session=%s: %s", view.id, session_id, error)
            return None
        await connection.execute(
            insert(AuditEntry).values(
                org_id=org_id,
                workspace_id=view.workspace_id,
                actor_kind="system",
                action="connection.resumed",
                target_type="connection",
                target_id=str(view.id),
                details={"session_id": str(session_id), "run_id": str(run_id)},
            )
        )
        return run_id

    async def deliver(
        self, org_id: uuid.UUID, view: ConnectionView, origin: Mapping[str, str], run_id: uuid.UUID
    ) -> None:
        """After the commit: the chat of the session follows the run (Telegram, Slack)."""
        if self._follower is None:
            return
        await self._follower.follow(
            org_id=org_id,
            session_id=uuid.UUID(origin["session_id"]),
            run_id=run_id,
            text=f"{self._title(view)} is connected. sama continues your request.",
        )

    def _title(self, view: ConnectionView) -> str:
        manifest = self._connections.catalog.get(view.connector)
        if manifest is not None:
            return manifest.title
        custom = view.custom() or {}
        title = custom.get("title")
        return title if isinstance(title, str) and title else view.connector


class SigninPoller:
    """A loop of the control plane: ``ConnectionService.poll_signins`` every ``POLL_S``."""

    def __init__(self, connections: ConnectionService, *, interval_s: float = POLL_S) -> None:
        self._connections = connections
        self._interval_s = interval_s

    async def serve(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._connections.poll_signins()
            except Exception:
                log.exception("the sign-in poll failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)
