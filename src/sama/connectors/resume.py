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
from sqlalchemy.ext.asyncio import AsyncEngine

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
        engine: AsyncEngine,
        queue: RunQueue,
        connections: ConnectionService,
        follower: Follower | None = None,
    ) -> None:
        self._engine = engine
        self._queue = queue
        self._connections = connections
        self._follower = follower

    async def __call__(
        self, org_id: uuid.UUID, view: ConnectionView, origin: Mapping[str, str]
    ) -> None:
        try:
            session_id = uuid.UUID(origin["session_id"])
            user_id = uuid.UUID(origin["user_id"]) if origin.get("user_id") else None
        except (KeyError, ValueError):
            log.warning("connection=%s has a broken origin", view.id)
            return
        title = self._title(view)
        text = resume_text(title, view.scope)
        try:
            async with self._engine.begin() as connection:
                # The note is not a message of a person: it does not count for the message rate.
                run_id = await self._queue.enqueue_in(
                    connection,
                    org_id=org_id,
                    session_id=session_id,
                    text=text,
                    user_id=user_id,
                    check_rate=False,
                )
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
        except QueueError as error:
            # An archived session, a quota, or a rate limit: the connection stays connected.
            log.info("connection=%s did not continue session=%s: %s", view.id, session_id, error)
            return
        if self._follower is not None:
            try:
                await self._follower.follow(
                    org_id=org_id,
                    session_id=session_id,
                    run_id=run_id,
                    text=f"{title} is connected. sama continues your request.",
                )
            except Exception:
                log.exception("the chat of session=%s does not follow run=%s", session_id, run_id)

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
