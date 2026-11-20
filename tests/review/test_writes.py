"""What a background review run may write, and how a person approves the rest
(docs/architecture.md, Sections 9.4 and 16.1)."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import (
    Approval,
    AuditEntry,
    Membership,
    ReviewProposal,
    Skill,
    SkillVersion,
    ToolPolicy,
)
from sama.gate import GateStep
from sama.gate.service import ApprovalError, ApprovalExpiredError, GateService
from sama.memory import (
    MemoryChangeError,
    MemoryDeniedError,
    MemoryScope,
    MemoryService,
    Operation,
    PostgresMemoryStore,
    Target,
    Writer,
)
from sama.memory.core import Action
from sama.orchestrator.queue import ClaimedRun, RunQueue
from sama.review.apply import ReviewApplier
from sama.review.proposals import PostgresProposalStore
from sama.skills import Actor, ManageRequest, PostgresSkillStore
from sama.skills.errors import SkillError
from sama.skills.store import Expected
from sama.tenancy.run_token import RunTokenClaims
from sama.vault.redact import default_redactor
from tests.orchestrator.conftest import Tenant
from tests.review.support import OWNER, add_member, complete_chat, set_counters, set_profile
from tests.skills.helpers import md

pytestmark = pytest.mark.usefixtures("tenant")

TOOLSETS = ["core", "todo", "web", "clarify", "github", "memory", "skills", "sessions"]


def add(content: str) -> Operation:
    return Operation(action=Action.ADD, content=content)


class Review:
    """A running review run, and the services of the control plane around it."""

    def __init__(self, engine: AsyncEngine, tenant: Tenant, run: ClaimedRun, session: str) -> None:
        self.engine = engine
        self.tenant = tenant
        self.run = run
        self.claims = RunTokenClaims(
            org_id=str(tenant.org),
            workspace_id=str(tenant.ids["ws"]),
            session_id=str(tenant.ids[session]),
            run_id=str(run.run_id),
            user_id=str(tenant.ids["mia"]) if session == "mine" else None,
            token_id="t-1",
            expires_at=int(time.time()) + 600,
        )
        self.memory_store = PostgresMemoryStore(engine)
        self.proposals = PostgresProposalStore(engine)
        self.memory = MemoryService(self.memory_store, self.proposals)
        self.skills = PostgresSkillStore(engine, {}, self.proposals)
        self.applier = ReviewApplier(engine, self.memory_store, self.skills)
        self.gate = GateService(engine)

    def scope(self, target: Target) -> MemoryScope:
        tenant = self.tenant
        return MemoryScope(
            tenant.org,
            target,
            workspace_id=None if target is Target.USER else tenant.ids["ws"],
            agent_profile_id=tenant.ids["profile"] if target is Target.AGENT else None,
            user_id=tenant.ids["mia"] if target is Target.USER else None,
        )

    async def entries(self, target: Target) -> tuple[str, ...]:
        return (await self.memory_store.read(self.scope(target))).entries

    async def approvals(self) -> list[Any]:
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                select(Approval).where(Approval.org_id == self.tenant.org).order_by(Approval.id)
            )
            return list(rows)

    async def proposal_rows(self) -> list[Any]:
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                select(ReviewProposal)
                .where(ReviewProposal.org_id == self.tenant.org)
                .order_by(ReviewProposal.created_at)
            )
            return list(rows)

    async def decide(
        self, user: str, *, approve: bool = True, always_allow: bool = False, gate: Any = None
    ) -> str:
        (approval,) = [a for a in await self.approvals() if a.status == "pending"]
        return str(
            await (gate or self.gate).decide_approval(
                org_id=self.tenant.org,
                run_id=self.run.run_id,
                call_id=approval.call_id,
                user_id=self.tenant.ids[user],
                approve=approve,
                always_allow=always_allow,
            )
        )


async def start_review(engine: AsyncEngine, tenant: Tenant, session: str = "s1") -> Review:
    """Ends ten turns in the session, and claims the review run that this queues."""
    await set_profile(engine, tenant, toolsets=TOOLSETS)
    await set_counters(engine, tenant, session, turns=9)
    await complete_chat(engine, tenant, session)
    run = await RunQueue(engine).claim(OWNER)
    assert run is not None
    assert run.kind == "review"
    await add_member(engine, tenant, "admin", workspace_role="admin")
    await add_member(engine, tenant, "owner", org_role="owner", workspace_role=None)
    return Review(engine, tenant, run, session)


# -- user scope: applies -----------------------------------------------------------------------


async def test_a_review_writes_user_memory_at_once(engine: AsyncEngine, tenant: Tenant) -> None:
    review = await start_review(engine, tenant, "mine")
    text = await review.memory.write(
        review.claims, Target.USER, [add("prefers short answers")], "from the review"
    )
    assert text.startswith("Saved user memory")
    assert await review.entries(Target.USER) == ("prefers short answers",)
    assert await review.approvals() == []
    assert await review.proposal_rows() == []


async def test_a_review_in_a_shared_session_has_no_user_memory(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    review = await start_review(engine, tenant, "s1")
    with pytest.raises(MemoryDeniedError, match="private session"):
        await review.memory.write(review.claims, Target.USER, [add("x")], None)


async def test_a_review_writes_a_user_skill_at_once(engine: AsyncEngine, tenant: Tenant) -> None:
    review = await start_review(engine, tenant, "mine")
    written = await review.skills.manage(
        review.claims, ManageRequest(action="create", name="my-flow", content=md("my-flow"))
    )
    assert (written.scope, written.proposed, written.version) == ("user", False, 1)
    assert await review.approvals() == []


# -- shared scopes: a proposal -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "scope"), [(Target.AGENT, "agent"), (Target.WORKSPACE, "workspace")]
)
async def test_shared_memory_becomes_a_proposal_and_an_approval(
    engine: AsyncEngine, tenant: Tenant, target: Target, scope: str
) -> None:
    review = await start_review(engine, tenant)
    text = await review.memory.write(review.claims, target, [add("builds with make")], "why")
    assert "Proposed a change of" in text
    assert await review.entries(target) == (), "nothing is saved yet"
    (approval,) = await review.approvals()
    (proposal,) = await review.proposal_rows()
    assert (approval.status, approval.tool_name) == ("pending", "memory")
    assert approval.call_id.startswith("review:")
    assert approval.run_id == review.run.run_id
    assert approval.reason is not None
    assert approval.reason.startswith("the background review proposes a change of")
    assert (proposal.status, proposal.kind, proposal.scope) == ("pending", "memory", scope)
    assert proposal.approval_id == approval.id
    assert proposal.payload["operations"][0]["content"] == "builds with make"
    assert approval.arguments["target"] == target.value
    async with engine.connect() as connection:
        actions = (
            await connection.execute(
                select(AuditEntry.action).where(AuditEntry.org_id == tenant.org)
            )
        ).scalars()
        assert "review.proposed" in set(actions)


@pytest.mark.parametrize(
    "secret",
    [
        "legacy-token-" + "abcdefghij" * 9,
        'legacy "quoted"\nline\ttab ' + "0123456789" * 9,
    ],
    ids=["long", "escaped"],
)
async def test_a_proposal_summary_never_shows_a_known_secret(
    engine: AsyncEngine, tenant: Tenant, secret: str
) -> None:
    review = await start_review(engine, tenant)
    default_redactor().add(secret)
    try:
        await review.memory_store.change(
            review.scope(Target.WORKSPACE),
            lambda entries: [secret],
            writer=Writer("agent", None, uuid.uuid4(), uuid.uuid4()),
            action="add",
        )
        for operation in (
            Operation(action=Action.REMOVE, old_text=secret),
            Operation(action=Action.PATCH, old_text=secret, content="gone"),
        ):
            await review.memory.write(review.claims, Target.WORKSPACE, [operation], "why")
        async with engine.connect() as connection:
            texts = [
                str(value)
                for query in (
                    select(Approval.reason, Approval.arguments),
                    select(AuditEntry.details).where(AuditEntry.org_id == tenant.org),
                    select(ReviewProposal.payload),
                )
                for row in (await connection.execute(query)).all()
                for value in row
            ]
        blob = "\n".join(texts)
        assert len(await review.approvals()) == 2
        assert secret[:12] not in blob
        assert secret[-12:] not in blob
        assert "REDACTED" in blob.upper()
    finally:
        default_redactor().forget(secret)


async def test_a_proposal_that_changes_nothing_is_not_stored(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    review = await start_review(engine, tenant)
    await review.memory_store.change(
        review.scope(Target.WORKSPACE),
        lambda entries: ["known"],
        writer=Writer("agent", None, uuid.uuid4(), uuid.uuid4()),
        action="add",
    )
    text = await review.memory.write(review.claims, Target.WORKSPACE, [add("known")], None)
    assert "no change" in text
    assert await review.approvals() == []


async def test_a_bad_proposal_is_refused_at_once(engine: AsyncEngine, tenant: Tenant) -> None:
    review = await start_review(engine, tenant)
    with pytest.raises(MemoryChangeError):
        await review.memory.write(
            review.claims,
            Target.WORKSPACE,
            [Operation(action=Action.REMOVE, content="absent")],
            None,
        )
    assert await review.approvals() == []


async def test_without_a_proposal_sink_a_review_cannot_change_shared_memory(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    review = await start_review(engine, tenant)
    memory = MemoryService(review.memory_store)
    with pytest.raises(MemoryDeniedError):
        await memory.write(review.claims, Target.AGENT, [add("x")], None)


async def test_an_unattended_run_that_is_not_a_review_still_cannot_write(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    queue = RunQueue(engine)
    await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids["mine"], text="cron", unattended=True
    )
    run = await queue.claim(OWNER)
    assert run is not None
    claims = RunTokenClaims(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(tenant.ids["mine"]),
        run_id=str(run.run_id),
        user_id=str(tenant.ids["mia"]),
        token_id="t",
        expires_at=int(time.time()) + 600,
    )
    memory = MemoryService(PostgresMemoryStore(engine), PostgresProposalStore(engine))
    with pytest.raises(MemoryDeniedError, match="no person"):
        await memory.write(claims, Target.USER, [add("x")], None)


@pytest.mark.parametrize("scope", ["workspace", "org"])
async def test_a_shared_skill_becomes_a_proposal(
    engine: AsyncEngine, tenant: Tenant, scope: str
) -> None:
    review = await start_review(engine, tenant)
    written = await review.skills.manage(
        review.claims,
        ManageRequest(action="create", name="deploy", scope=scope, content=md("deploy")),
    )
    assert (written.proposed, written.scope) == (True, scope)
    (approval,) = await review.approvals()
    (proposal,) = await review.proposal_rows()
    assert (approval.tool_name, proposal.kind, proposal.scope) == ("skill_manage", "skill", scope)
    async with engine.connect() as connection:
        count = (
            await connection.execute(
                select(func.count()).select_from(Skill).where(Skill.name == "deploy")
            )
        ).scalar_one()
    assert count == 0


async def test_without_a_proposal_sink_a_review_cannot_change_a_shared_skill(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    review = await start_review(engine, tenant)
    skills = PostgresSkillStore(engine, {})
    with pytest.raises(SkillError, match="background review"):
        await skills.manage(
            review.claims,
            ManageRequest(action="create", name="deploy", scope="workspace", content=md("deploy")),
        )


# -- the decision ------------------------------------------------------------------------------


async def test_an_approved_memory_proposal_is_applied(engine: AsyncEngine, tenant: Tenant) -> None:
    review = await start_review(engine, tenant)
    await review.memory.write(review.claims, Target.WORKSPACE, [add("builds with make")], "why")
    assert await review.decide("admin") == "approved"
    assert await review.entries(Target.WORKSPACE) == ()
    assert await review.applier.apply_decided() >= 1
    assert await review.entries(Target.WORKSPACE) == ("builds with make",)
    (proposal,) = await review.proposal_rows()
    assert (proposal.status, proposal.error) == ("applied", None)
    assert proposal.applied_at is not None
    assert await review.applier.apply_decided() == 0, "a handled proposal is not handled again"
    assert await review.entries(Target.WORKSPACE) == ("builds with make",)
    revisions = await review.memory_store.revisions(review.scope(Target.WORKSPACE), 10)
    assert revisions[0].author_user_id == tenant.ids["admin"]


async def test_an_approved_agent_memory_proposal_is_applied(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    review = await start_review(engine, tenant)
    await review.memory.write(review.claims, Target.AGENT, [add("be brief")], None)
    await review.decide("admin")
    assert await review.applier.apply_decided(org_id=tenant.org, run_id=review.run.run_id) == 1
    assert await review.entries(Target.AGENT) == ("be brief",)


async def test_a_rejected_proposal_is_discarded(engine: AsyncEngine, tenant: Tenant) -> None:
    review = await start_review(engine, tenant)
    await review.memory.write(review.claims, Target.WORKSPACE, [add("nope")], None)
    assert await review.decide("admin", approve=False) == "rejected"
    await review.applier.apply_decided()
    (proposal,) = await review.proposal_rows()
    assert proposal.status == "discarded"
    assert await review.entries(Target.WORKSPACE) == ()


async def test_an_expired_proposal_is_discarded(engine: AsyncEngine, tenant: Tenant) -> None:
    review = await start_review(engine, tenant)
    await review.memory.write(review.claims, Target.WORKSPACE, [add("late")], None)
    later = datetime.now(UTC) + timedelta(days=2)
    gate = GateService(engine, clock=lambda: later)
    assert await gate.expire() >= 1
    await review.applier.apply_decided()
    (proposal,) = await review.proposal_rows()
    assert (proposal.status, proposal.error) == ("discarded", "the approval timed out")
    assert await review.entries(Target.WORKSPACE) == ()


async def test_a_late_decision_expires_the_proposal(engine: AsyncEngine, tenant: Tenant) -> None:
    review = await start_review(engine, tenant)
    await review.memory.write(review.claims, Target.WORKSPACE, [add("late")], None)
    later = datetime.now(UTC) + timedelta(days=2)
    with pytest.raises(ApprovalExpiredError):
        await review.decide("admin", gate=GateService(engine, clock=lambda: later))
    await review.applier.apply_decided()
    assert (await review.proposal_rows())[0].status == "discarded"


async def test_a_member_cannot_decide_a_proposal(engine: AsyncEngine, tenant: Tenant) -> None:
    review = await start_review(engine, tenant)
    await review.memory.write(review.claims, Target.WORKSPACE, [add("x")], None)
    with pytest.raises(ApprovalError, match="workspace admin"):
        await review.decide("mia")
    assert (await review.approvals())[0].status == "pending"


async def test_an_org_skill_needs_an_org_owner(engine: AsyncEngine, tenant: Tenant) -> None:
    review = await start_review(engine, tenant)
    await review.skills.manage(
        review.claims,
        ManageRequest(action="create", name="policy", scope="org", content=md("policy")),
    )
    with pytest.raises(ApprovalError, match="org owner"):
        await review.decide("admin")
    assert await review.decide("owner") == "approved"
    await review.applier.apply_decided()
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                select(Skill.scope, Skill.workspace_id).where(
                    Skill.org_id == tenant.org, Skill.name == "policy"
                )
            )
        ).one()
    assert tuple(row) == ("org", None)
    assert (await review.proposal_rows())[0].status == "applied"


async def test_an_approved_workspace_skill_is_written(engine: AsyncEngine, tenant: Tenant) -> None:
    review = await start_review(engine, tenant)
    await review.skills.manage(
        review.claims,
        ManageRequest(action="create", name="deploy", scope="workspace", content=md("deploy")),
    )
    await review.decide("admin")
    await review.applier.apply_decided()
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                select(Skill.scope, Skill.workspace_id).where(
                    Skill.org_id == tenant.org, Skill.name == "deploy"
                )
            )
        ).one()
    assert tuple(row) == ("workspace", tenant.ids["ws"])


async def test_a_proposal_that_no_longer_fits_fails_with_a_reason(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    review = await start_review(engine, tenant)
    request = ManageRequest(action="create", name="deploy", scope="workspace", content=md("deploy"))
    await review.skills.manage(review.claims, request)
    # The skill appears before the person approves the proposal.
    await review.skills.apply(
        Actor(
            org_id=tenant.org,
            workspace_id=tenant.ids["ws"],
            user_id=tenant.ids["admin"],
            private=False,
            workspace_admin=True,
        ),
        request,
        expected=Expected(None, None),
    )
    await review.decide("admin")
    await review.applier.apply_decided()
    (proposal,) = await review.proposal_rows()
    assert proposal.status == "failed"
    assert proposal.error


# -- atomic apply and a stale target ------------------------------------------------------------


def _fail_once(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The status mark of the applier fails once, after the domain write in the same
    transaction. Returns the list of the calls."""
    original = ReviewApplier._mark
    calls: list[int] = []

    async def mark(connection: Any, row: Any, status: str, error: str | None) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the process stops before the commit")
        await original(connection, row, status, error)

    monkeypatch.setattr(ReviewApplier, "_mark", staticmethod(mark))
    return calls


async def _legacy_memory(review: Review, text: str) -> None:
    await review.memory_store.change(
        review.scope(Target.WORKSPACE),
        lambda entries: [text],
        writer=Writer("agent", None, uuid.uuid4(), uuid.uuid4()),
        action="add",
    )


async def test_a_failure_after_the_memory_write_rolls_the_write_back(
    engine: AsyncEngine, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = await start_review(engine, tenant)
    await _legacy_memory(review, "region=us")
    scope = review.scope(Target.WORKSPACE)
    await review.memory.write(
        review.claims,
        Target.WORKSPACE,
        [Operation(action=Action.PATCH, old_text="region=us", content="region=us-east")],
        None,
    )
    await review.decide("admin")
    revisions = len(await review.memory_store.revisions(scope, 50))
    calls = _fail_once(monkeypatch)
    assert await review.applier.apply_decided() == 0
    assert await review.entries(Target.WORKSPACE) == ("region=us",)
    assert len(await review.memory_store.revisions(scope, 50)) == revisions
    assert (await review.proposal_rows())[0].status == "pending"
    assert await review.applier.apply_decided() == 1
    assert len(calls) == 2
    assert await review.entries(Target.WORKSPACE) == ("region=us-east",), "applied exactly once"
    assert len(await review.memory_store.revisions(scope, 50)) == revisions + 1
    (proposal,) = await review.proposal_rows()
    assert (proposal.status, proposal.error) == ("applied", None)
    assert await review.applier.apply_decided() == 0
    assert await review.entries(Target.WORKSPACE) == ("region=us-east",)


async def _versions(engine: AsyncEngine, tenant: Tenant, name: str) -> list[tuple[str, int]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(Skill.scope, SkillVersion.version_no)
            .join(SkillVersion, SkillVersion.skill_id == Skill.id)
            .where(Skill.org_id == tenant.org, Skill.name == name)
            .order_by(Skill.scope, SkillVersion.version_no)
        )
        return [(row.scope, row.version_no) for row in rows]


async def test_a_failure_after_the_skill_write_rolls_the_write_back(
    engine: AsyncEngine, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = await start_review(engine, tenant)
    await review.skills.manage(
        review.claims,
        ManageRequest(action="create", name="deploy", scope="workspace", content=md("deploy")),
    )
    await review.decide("admin")
    _fail_once(monkeypatch)
    assert await review.applier.apply_decided() == 0
    assert await _versions(engine, tenant, "deploy") == []
    assert (await review.proposal_rows())[0].status == "pending"
    assert await review.applier.apply_decided() == 1
    assert await _versions(engine, tenant, "deploy") == [("workspace", 1)]
    assert (await review.proposal_rows())[0].status == "applied"
    assert await review.applier.apply_decided() == 0
    assert await _versions(engine, tenant, "deploy") == [("workspace", 1)]


async def test_two_appliers_apply_a_proposal_once(engine: AsyncEngine, tenant: Tenant) -> None:
    review = await start_review(engine, tenant)
    await _legacy_memory(review, "region=us")
    await review.memory.write(
        review.claims,
        Target.WORKSPACE,
        [Operation(action=Action.PATCH, old_text="region=us", content="region=us-east")],
        None,
    )
    await review.decide("admin")
    other = ReviewApplier(engine, review.memory_store, review.skills)
    results = await asyncio.gather(review.applier.apply_decided(), other.apply_decided())
    assert sum(results) == 1
    assert await review.entries(Target.WORKSPACE) == ("region=us-east",)
    async with engine.connect() as connection:
        applied = (
            await connection.execute(
                select(func.count())
                .select_from(AuditEntry)
                .where(AuditEntry.org_id == tenant.org, AuditEntry.action == "review.applied")
            )
        ).scalar_one()
    assert applied == 1


def _admin(tenant: Tenant, *, owner: bool = False) -> Actor:
    return Actor(
        org_id=tenant.org,
        workspace_id=tenant.ids["ws"],
        user_id=tenant.ids["owner" if owner else "admin"],
        private=False,
        workspace_admin=True,
        org_owner=owner,
    )


async def _skill_id(engine: AsyncEngine, tenant: Tenant, scope: str, name: str) -> uuid.UUID:
    async with engine.connect() as connection:
        return (
            await connection.execute(
                select(Skill.id).where(
                    Skill.org_id == tenant.org, Skill.scope == scope, Skill.name == name
                )
            )
        ).scalar_one()


async def test_a_stale_skill_target_fails_and_does_not_reach_another_scope(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    review = await start_review(engine, tenant)
    # A workspace skill exists. The review proposes an edit of it.
    await review.skills.apply(
        _admin(tenant),
        ManageRequest(action="create", name="deploy", scope="workspace", content=md("deploy")),
        expected=Expected(None, None),
    )
    await review.skills.manage(
        review.claims,
        ManageRequest(
            action="edit",
            name="deploy",
            content=md("deploy", body="Do it the new way."),
        ),
    )
    (proposal,) = await review.proposal_rows()
    assert proposal.payload["scope"] == "workspace"
    assert proposal.payload["target_version"] == 1
    # Before the approval: the workspace skill is archived, and an org skill has the name.
    workspace_id = await _skill_id(engine, tenant, "workspace", "deploy")
    await review.skills.apply(
        _admin(tenant),
        ManageRequest(action="archive", name="deploy", scope="workspace"),
        expected=Expected(workspace_id, 1),
    )
    await review.skills.apply(
        _admin(tenant, owner=True),
        ManageRequest(action="create", name="deploy", scope="org", content=md("deploy")),
        expected=Expected(None, None),
    )
    await review.decide("admin")
    await review.applier.apply_decided()
    (proposal,) = await review.proposal_rows()
    assert proposal.status == "failed"
    assert proposal.error
    assert await _versions(engine, tenant, "deploy") == [
        ("org", 1),
        ("workspace", 1),
        ("workspace", 2),
    ]


async def test_a_skill_that_changed_since_the_proposal_fails(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    review = await start_review(engine, tenant)
    admin = _admin(tenant)
    await review.skills.apply(
        admin,
        ManageRequest(action="create", name="deploy", scope="workspace", content=md("deploy")),
        expected=Expected(None, None),
    )
    await review.skills.manage(
        review.claims,
        ManageRequest(action="edit", name="deploy", content=md("deploy", body="Review text.")),
    )
    skill = await _skill_id(engine, tenant, "workspace", "deploy")
    await review.skills.apply(
        admin,
        ManageRequest(
            action="edit", name="deploy", scope="workspace", content=md("deploy", body="A person.")
        ),
        expected=Expected(skill, 1),
    )
    await review.decide("admin")
    await review.applier.apply_decided()
    (proposal,) = await review.proposal_rows()
    assert proposal.status == "failed"
    assert "changed after" in (proposal.error or "")
    assert await _versions(engine, tenant, "deploy") == [("workspace", 1), ("workspace", 2)]


async def test_a_proposal_for_an_unchanged_state_still_applies(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    review = await start_review(engine, tenant)
    admin = _admin(tenant)
    await review.skills.apply(
        admin,
        ManageRequest(action="create", name="deploy", scope="workspace", content=md("deploy")),
        expected=Expected(None, None),
    )
    await review.skills.manage(
        review.claims,
        ManageRequest(action="edit", name="deploy", content=md("deploy", body="Review text.")),
    )
    await review.decide("admin")
    await review.applier.apply_decided()
    assert (await review.proposal_rows())[0].status == "applied"
    assert await _versions(engine, tenant, "deploy") == [("workspace", 1), ("workspace", 2)]


async def test_the_role_is_checked_again_when_the_proposal_is_applied(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    review = await start_review(engine, tenant)
    await review.memory.write(review.claims, Target.WORKSPACE, [add("x")], None)
    await review.decide("admin")
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership)
            .where(Membership.user_id == tenant.ids["admin"], Membership.workspace_id.is_not(None))
            .values(role="member")
        )
    await review.applier.apply_decided()
    (proposal,) = await review.proposal_rows()
    assert proposal.status == "failed"
    assert "not a workspace admin" in (proposal.error or "")
    assert await review.entries(Target.WORKSPACE) == ()


async def test_always_allow_is_ignored_for_a_proposal(engine: AsyncEngine, tenant: Tenant) -> None:
    review = await start_review(engine, tenant)
    await review.memory.write(review.claims, Target.WORKSPACE, [add("x")], None)
    await review.decide("admin", always_allow=True)
    async with engine.connect() as connection:
        count = (
            await connection.execute(
                select(func.count()).select_from(ToolPolicy).where(ToolPolicy.org_id == tenant.org)
            )
        ).scalar_one()
    assert count == 0


# -- the tools of a review run -----------------------------------------------------------------


@pytest.mark.parametrize(
    "tool",
    [
        "Bash",
        "Read",
        "WebFetch",
        "mcp__sama__todo",
        "mcp__sama__session_search",
        "mcp__sama__clarify",
        "mcp__sama__vision_analyze",
        "mcp__github__create_pr",
    ],
)
async def test_a_review_run_has_no_tool_but_memory_and_skills(
    engine: AsyncEngine, tenant: Tenant, tool: str
) -> None:
    review = await start_review(engine, tenant)
    verdict = await review.gate.check(review.claims, f"call-{tool}", tool, {})
    assert (verdict.action, verdict.step) == ("deny", GateStep.TOOLSET)


@pytest.mark.parametrize(
    "tool",
    [
        "mcp__sama__memory",
        "mcp__sama__skills_list",
        "mcp__sama__skill_view",
        "mcp__sama__skill_manage",
    ],
)
async def test_a_review_run_may_use_the_memory_and_skill_tools(
    engine: AsyncEngine, tenant: Tenant, tool: str
) -> None:
    review = await start_review(engine, tenant)
    verdict = await review.gate.check(review.claims, f"call-{tool}", tool, {})
    assert verdict.action == "allow"


async def test_a_chat_run_keeps_all_its_toolsets(engine: AsyncEngine, tenant: Tenant) -> None:
    await set_profile(engine, tenant, toolsets=TOOLSETS)
    queue = RunQueue(engine)
    await queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids["s2"], text="hi", user_id=tenant.ids["mia"]
    )
    run = await queue.claim(OWNER)
    assert run is not None
    claims = RunTokenClaims(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(tenant.ids["s2"]),
        run_id=str(run.run_id),
        token_id="t",
        expires_at=int(time.time()) + 600,
    )
    verdict = await GateService(engine).check(claims, "c1", "mcp__sama__todo", {})
    assert verdict.step is not GateStep.TOOLSET or verdict.action == "allow"
