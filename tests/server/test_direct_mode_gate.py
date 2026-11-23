"""An org API key never gets the direct-mode shortcut of a coding agent or a session grant
(docs/architecture.md, Sections 15.4 and 16.1)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.apikeys import ApiKeyStore, KeyContext
from sama.db.models import AgentProfile, SessionToolAllow
from sama.gate.service import GateService
from sama.orchestrator.queue import RunQueue
from sama.server import ServerCore
from sama.tenancy import RunTokenClaims
from tests.orchestrator.conftest import Tenant
from tests.server.conftest import Keys

pytestmark = pytest.mark.usefixtures("admin")

CODING = {"agent": "claude_code", "repo": "", "prompt": "fix it", "open_pr": False}


async def start(
    engine: AsyncEngine, core: ServerCore, keys: Keys, **options: Any
) -> tuple[KeyContext, uuid.UUID, uuid.UUID]:
    key = await keys.make(**options)
    context = await ApiKeyStore(engine).verify(key, "mcp")
    profile = (await core.profiles(context))[0]
    session = await core.open_session(context, profile, title=None, channel="mcp")
    run = await core.send(context, session, "fix the bug")
    return context, session, run


def claims(tenant: Tenant, session: uuid.UUID, run: uuid.UUID) -> RunTokenClaims:
    return RunTokenClaims(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(session),
        run_id=str(run),
        token_id="t-1",
        expires_at=2_000_000_000,
    )


async def coding_profile(engine: AsyncEngine, tenant: Tenant) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == tenant.ids["profile"])
            .values(runtime="claude_code", toolsets=["core", "coding_agents"])
        )


async def test_a_pending_key_on_a_coding_profile_still_asks_for_coding_agent_run(
    engine: AsyncEngine, core: ServerCore, keys: Keys, admin: Tenant
) -> None:
    await coding_profile(engine, admin)
    gate = GateService(engine)
    _context, session, run = await start(engine, core, keys, approvals="pending")
    # The run of the key is attended, in a private session of its user, on a coding runtime,
    # and it is no message of a person: the normal gate applies.
    verdict = await gate.check(claims(admin, session, run), "c1", "coding_agent_run", CODING)
    assert verdict.action == "ask"
    # A message of the same user from a human channel in the same session has the shortcut.
    human = await RunQueue(engine).enqueue(
        org_id=admin.org,
        session_id=session,
        text="and the tests",
        user_id=admin.ids["mia"],
        human=True,
    )
    allowed = await gate.check(claims(admin, session, human), "c2", "coding_agent_run", CODING)
    assert allowed.action == "allow"


async def test_a_deny_key_on_a_coding_profile_denies_coding_agent_run(
    engine: AsyncEngine, core: ServerCore, keys: Keys, admin: Tenant
) -> None:
    await coding_profile(engine, admin)
    gate = GateService(engine)
    _context, session, run = await start(engine, core, keys, approvals="deny")
    verdict = await gate.check(claims(admin, session, run), "c1", "coding_agent_run", CODING)
    assert verdict.action == "deny"


async def test_a_session_grant_does_not_make_a_deny_key_run_a_tool(
    engine: AsyncEngine, core: ServerCore, keys: Keys, admin: Tenant
) -> None:
    gate = GateService(engine)
    _context, session, run = await start(engine, core, keys, approvals="deny")
    async with engine.begin() as connection:
        await connection.execute(
            insert(SessionToolAllow).values(
                org_id=admin.org, session_id=session, user_id=admin.ids["mia"], tool="Bash"
            )
        )
    verdict = await gate.check(claims(admin, session, run), "c1", "Bash", {"command": "make x"})
    assert verdict.action == "deny"
    assert "unattended" in verdict.reason
