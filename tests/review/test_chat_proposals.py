"""Proposals from a chat run (M3.2, docs/architecture.md, Sections 9.4 and 10.2).

A chat run of a user who lacks the role for a shared change proposes the change. A new soul is
always a proposal. A person with the right role approves the proposal over chat or the web."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AgentProfile, Approval, AuditEntry, ReviewProposal, Run, Skill
from sama.gate.service import ApprovalError, GateService
from sama.memory import MemoryScope, MemoryService, Operation, PostgresMemoryStore, Target
from sama.memory.core import Action
from sama.orchestrator.queue import ClaimedRun, RunQueue
from sama.review.apply import ReviewApplier
from sama.review.proposals import PostgresProposalStore
from sama.review.soul import SoulError, SoulProposer, soul_digest
from sama.skills import ManageRequest, PostgresSkillStore
from sama.tenancy.run_token import RunTokenClaims
from sama.vault.redact import default_redactor
from tests.orchestrator.conftest import Tenant
from tests.review.support import OWNER, add_member, set_profile
from tests.skills.helpers import md

pytestmark = pytest.mark.usefixtures("tenant")

TOOLSETS = ["core", "memory", "skills", "self"]


class Chat:
    """A running chat run of one user, and the services of the control plane around it."""

    def __init__(self, engine: AsyncEngine, tenant: Tenant, run: ClaimedRun, user: str) -> None:
        self.engine = engine
        self.tenant = tenant
        self.run = run
        self.claims = RunTokenClaims(
            org_id=str(tenant.org),
            workspace_id=str(tenant.ids["ws"]),
            session_id=str(run.session_id),
            run_id=str(run.run_id),
            user_id=str(tenant.ids[user]),
            token_id="t-1",
            expires_at=int(time.time()) + 600,
        )
        self.memory_store = PostgresMemoryStore(engine)
        self.proposals = PostgresProposalStore(engine)
        self.memory = MemoryService(self.memory_store, self.proposals)
        self.skills = PostgresSkillStore(engine, {}, self.proposals)
        self.souls = SoulProposer(engine, self.proposals)
        self.applier = ReviewApplier(engine, self.memory_store, self.skills)
        self.gate = GateService(engine)

    async def workspace_memory(self) -> tuple[str, ...]:
        scope = MemoryScope(self.tenant.org, Target.WORKSPACE, workspace_id=self.tenant.ids["ws"])
        return (await self.memory_store.read(scope)).entries

    async def rows(self, model: Any) -> list[Any]:
        async with self.engine.connect() as connection:
            result = await connection.execute(
                select(model).where(model.org_id == self.tenant.org).order_by(model.created_at)
            )
            return list(result)

    async def decide(self, user: str, *, approve: bool = True, gate: Any = None) -> str:
        (approval,) = [a for a in await self.rows(Approval) if a.status == "pending"]
        return str(
            await (gate or self.gate).decide_approval(
                org_id=self.tenant.org,
                run_id=self.run.run_id,
                call_id=approval.call_id,
                user_id=self.tenant.ids[user],
                approve=approve,
                always_allow=False,
            )
        )

    async def soul(self) -> str:
        async with self.engine.connect() as connection:
            return str(
                await connection.scalar(
                    select(AgentProfile.soul).where(AgentProfile.id == self.tenant.ids["profile"])
                )
            )


async def start_chat(
    engine: AsyncEngine, tenant: Tenant, user: str = "mia", *, session: str = "s1"
) -> Chat:
    """Adds the users ``admin`` (workspace admin), ``owner`` (org owner), and ``lead`` (org
    admin, workspace member), and claims a chat run of ``user``. The run stays open."""
    await set_profile(engine, tenant, toolsets=TOOLSETS)
    await add_member(engine, tenant, "admin", workspace_role="admin")
    await add_member(engine, tenant, "owner", org_role="owner", workspace_role=None)
    await add_member(engine, tenant, "lead", org_role="admin", workspace_role="member")
    queue = RunQueue(engine)
    await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids[session], text="hi", user_id=tenant.ids[user]
    )
    run = await queue.claim(OWNER)
    assert run is not None
    assert run.kind == "chat"
    return Chat(engine, tenant, run, user)


def add(content: str) -> Operation:
    return Operation(action=Action.ADD, content=content)


# -- memory ------------------------------------------------------------------------------------


async def test_a_member_proposes_workspace_memory_and_an_admin_approves(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    chat = await start_chat(engine, tenant)
    text = await chat.memory.write(chat.claims, Target.WORKSPACE, [add("ship on fridays")], "why")
    assert "A workspace admin or the org owner must approve it first" in text
    assert await chat.workspace_memory() == ()
    (approval,) = await chat.rows(Approval)
    (proposal,) = await chat.rows(ReviewProposal)
    assert (proposal.origin, proposal.kind, proposal.scope) == ("chat", "memory", "workspace")
    assert approval.reason.startswith("the agent proposes a change of workspace memory")
    assert approval.run_id == chat.run.run_id
    with pytest.raises(ApprovalError, match="workspace admin or the org owner"):
        await chat.decide("mia")
    assert await chat.decide("admin") == "approved"
    assert await chat.applier.apply_decided(org_id=tenant.org, run_id=chat.run.run_id) == 1
    assert await chat.workspace_memory() == ("ship on fridays",)
    async with engine.connect() as connection:
        status = await connection.scalar(select(Run.status).where(Run.id == chat.run.run_id))
    assert status == "running", "a proposal does not pause the chat run"


async def test_a_workspace_admin_writes_workspace_memory_at_once(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    chat = await start_chat(engine, tenant, "admin")
    await chat.memory.write(chat.claims, Target.WORKSPACE, [add("deploy with make")], None)
    assert await chat.workspace_memory() == ("deploy with make",)
    assert await chat.rows(ReviewProposal) == []


async def test_a_chat_proposal_times_out_as_a_deny(engine: AsyncEngine, tenant: Tenant) -> None:
    chat = await start_chat(engine, tenant)
    await chat.memory.write(chat.claims, Target.WORKSPACE, [add("late")], None)
    later = datetime.now(UTC) + timedelta(days=2)
    assert await GateService(engine, clock=lambda: later).expire() >= 1
    await chat.applier.apply_decided()
    (proposal,) = await chat.rows(ReviewProposal)
    assert (proposal.status, proposal.error) == ("discarded", "the approval timed out")
    assert await chat.workspace_memory() == ()


async def test_a_user_memory_write_in_a_chat_is_never_a_proposal(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    chat = await start_chat(engine, tenant, session="mine")
    await chat.memory.write(chat.claims, Target.USER, [add("likes tea")], None)
    assert await chat.rows(ReviewProposal) == []


# -- skills ------------------------------------------------------------------------------------


async def test_a_member_proposes_a_workspace_skill(engine: AsyncEngine, tenant: Tenant) -> None:
    chat = await start_chat(engine, tenant)
    written = await chat.skills.manage(
        chat.claims,
        ManageRequest(action="create", name="deploy", scope="workspace", content=md("deploy")),
    )
    assert (written.proposed, written.scope) == (True, "workspace")
    (proposal,) = await chat.rows(ReviewProposal)
    assert (proposal.origin, proposal.kind) == ("chat", "skill")
    assert await chat.decide("admin") == "approved"
    await chat.applier.apply_decided()
    async with engine.connect() as connection:
        scope = await connection.scalar(
            select(Skill.scope).where(Skill.org_id == tenant.org, Skill.name == "deploy")
        )
    assert scope == "workspace"


async def test_an_org_skill_from_a_workspace_admin_needs_the_org_owner(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    chat = await start_chat(engine, tenant, "admin")
    written = await chat.skills.manage(
        chat.claims,
        ManageRequest(action="create", name="policy", scope="org", content=md("policy")),
    )
    assert written.proposed
    with pytest.raises(ApprovalError, match="only the org owner"):
        await chat.decide("admin")
    assert await chat.decide("owner") == "approved"
    await chat.applier.apply_decided()
    assert (await chat.rows(ReviewProposal))[0].status == "applied"


async def test_a_workspace_admin_writes_a_workspace_skill_at_once(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    chat = await start_chat(engine, tenant, "admin")
    written = await chat.skills.manage(
        chat.claims,
        ManageRequest(action="create", name="deploy", scope="workspace", content=md("deploy")),
    )
    assert not written.proposed
    assert await chat.rows(ReviewProposal) == []


# -- soul --------------------------------------------------------------------------------------


async def test_a_soul_of_an_org_profile_needs_the_org_owner(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    chat = await start_chat(engine, tenant)
    text = await chat.souls.propose(chat.claims, "You are brief.", "be brief")
    assert "Only the org owner must approve it first" in text
    assert "next session" in text
    assert await chat.soul() == "You are a test agent."
    (approval,) = await chat.rows(Approval)
    (proposal,) = await chat.rows(ReviewProposal)
    assert (proposal.kind, proposal.scope, proposal.origin) == ("soul", "org", "chat")
    assert approval.tool_name == "change_propose"
    assert proposal.payload["base_digest"] == soul_digest("You are a test agent.")
    assert proposal.acting_user_id == tenant.ids["mia"]
    with pytest.raises(ApprovalError, match="only the org owner"):
        await chat.decide("admin")
    with pytest.raises(ApprovalError, match="only the org owner"):
        await chat.decide("lead")
    assert await chat.decide("owner") == "approved"
    assert await chat.applier.apply_decided() >= 1
    assert await chat.soul() == "You are brief."
    (proposal,) = await chat.rows(ReviewProposal)
    assert proposal.status == "applied"
    async with engine.connect() as connection:
        details = (
            await connection.execute(
                select(AuditEntry.details, AuditEntry.actor_user_id).where(
                    AuditEntry.org_id == tenant.org, AuditEntry.action == "profile.updated"
                )
            )
        ).one()
    assert details.details["fields"] == ["soul"]
    assert details.details["proposal_id"] == str(proposal.id)
    assert details.actor_user_id == tenant.ids["owner"]


async def test_a_soul_of_a_workspace_profile_needs_a_workspace_admin(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    chat = await start_chat(engine, tenant, "admin")
    await set_profile(engine, tenant, workspace_id=tenant.ids["ws"])
    text = await chat.souls.propose(chat.claims, "You are kind.", "be kind")
    assert "A workspace admin or the org owner must approve it first" in text
    (proposal,) = await chat.rows(ReviewProposal)
    assert proposal.scope == "workspace"
    assert await chat.soul() == "You are a test agent.", "an admin still proposes a soul"
    with pytest.raises(ApprovalError, match="workspace admin or the org owner"):
        await chat.decide("mia")
    assert await chat.decide("admin") == "approved"
    await chat.applier.apply_decided()
    assert await chat.soul() == "You are kind."


async def test_a_soul_that_changed_after_the_proposal_fails(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    chat = await start_chat(engine, tenant)
    await chat.souls.propose(chat.claims, "You are brief.", "be brief")
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == tenant.ids["profile"])
            .values(soul="Someone else changed it.")
        )
    await chat.decide("owner")
    await chat.applier.apply_decided()
    (proposal,) = await chat.rows(ReviewProposal)
    assert proposal.status == "failed"
    assert "the soul changed after the proposal" in proposal.error
    assert await chat.soul() == "Someone else changed it."


async def test_a_rejected_soul_is_discarded(engine: AsyncEngine, tenant: Tenant) -> None:
    chat = await start_chat(engine, tenant)
    await chat.souls.propose(chat.claims, "You are brief.", "be brief")
    assert await chat.decide("owner", approve=False) == "rejected"
    await chat.applier.apply_decided()
    assert (await chat.rows(ReviewProposal))[0].status == "discarded"
    assert await chat.soul() == "You are a test agent."


async def test_a_soul_proposal_is_checked(engine: AsyncEngine, tenant: Tenant) -> None:
    chat = await start_chat(engine, tenant)
    with pytest.raises(SoulError, match="full new soul"):
        await chat.souls.propose(chat.claims, "  ", "x")
    with pytest.raises(SoulError, match="summary"):
        await chat.souls.propose(chat.claims, "You are brief.", " ")
    with pytest.raises(SoulError, match="at most"):
        await chat.souls.propose(chat.claims, "x" * 20001, "long")
    secret = "soul-secret-" + "abcdefghij" * 4
    default_redactor().add(secret)
    try:
        with pytest.raises(SoulError, match="secret"):
            await chat.souls.propose(chat.claims, f"Use {secret}.", "secret")
    finally:
        default_redactor().forget(secret)
    unchanged = await chat.souls.propose(chat.claims, "You are a test agent.", "same")
    assert "already this text" in unchanged
    async with engine.connect() as connection:
        count = await connection.scalar(
            select(func.count())
            .select_from(ReviewProposal)
            .where(ReviewProposal.org_id == tenant.org)
        )
    assert count == 0


async def test_an_unattended_run_cannot_propose_a_soul(engine: AsyncEngine, tenant: Tenant) -> None:
    chat = await start_chat(engine, tenant)
    async with engine.begin() as connection:
        await connection.execute(
            update(Run).where(Run.id == chat.run.run_id).values(unattended=True)
        )
    with pytest.raises(SoulError, match="person in it"):
        await chat.souls.propose(chat.claims, "You are brief.", "be brief")
