"""The live browser of a session for the web app (docs/architecture.md, Section 14.1, M8.3).

``GET /sessions/{id}/browser`` gives the live view of the browser of an open run of the
session: a short-lived Browserbase live view URL of the active tab, the URL and the title of the
tab, and who has control. ``POST /sessions/{id}/browser/control`` gives the browser to the
person (the agent waits) or back to the agent. Only a member who can see the session reaches
these routes (``session_access``). The Browserbase key never leaves the control plane.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from sama.browser.service import BrowserService, LiveState
from sama.db.models import ENDED_RUN_STATUSES, Run


class ControlIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    action: Literal["take", "give_back"]


async def open_runs(
    connection: AsyncConnection, org_id: uuid.UUID, session_id: uuid.UUID
) -> list[uuid.UUID]:
    """The runs of the session that did not end, newest first."""
    rows = await connection.execute(
        select(Run.id)
        .where(
            Run.org_id == org_id,
            Run.session_id == session_id,
            Run.status.not_in(ENDED_RUN_STATUSES),
        )
        .order_by(Run.created_at.desc())
        .limit(8)
    )
    return list(rows.scalars())


async def live_of(
    browser: BrowserService, org_id: uuid.UUID, runs: list[uuid.UUID]
) -> dict[str, Any] | None:
    """The live browser of the newest open run that has a browser session, for the answer."""
    for run_id in runs:
        state = await browser.live_state(str(org_id), str(run_id))
        if state is not None:
            return public(state)
    return None


def public(state: LiveState) -> dict[str, Any]:
    view = state.view
    return {
        "run_id": state.run_id,
        "target": state.target,
        "control": state.control,
        "view_url": None if view is None else view.view_url,
        "page_url": None if view is None else view.page_url,
        "title": None if view is None else view.title,
        "tabs": None if view is None else view.tabs,
        "page_id": None if view is None else view.page_id,
    }
