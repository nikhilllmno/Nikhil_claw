"""Coding mode: attached repositories and plans (docs/architecture.md, Section 15.5)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.coding_mode.service import (
    CodingError,
    CodingService,
    approved_message,
    describe_plan,
    pull_request_message,
)
from sama.db.models import MAX_SESSION_REPOS, AuditEntry, Message, Session
from sama.orchestrator.queue import RateLimitedError, RunQueue
from tests.orchestrator.conftest import Tenant


async def _actions(engine: AsyncEngine, tenant: Tenant) -> list[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(AuditEntry.action)
            .where(AuditEntry.org_id == tenant.org)
            .order_by(AuditEntry.created_at, AuditEntry.id)
        )
        return [row.action for row in rows]


async def test_attach_and_detach_a_repository(
    coding: CodingService, engine: AsyncEngine, tenant: Tenant
) -> None:
    ids = tenant.ids
    org, session, mia = tenant.org, ids["mine"], ids["mia"]
    repo = await coding.attach(
        org_id=org, session_id=session, user_id=mia, repo="https://github.com/octo/hello.git"
    )
    assert (repo.repo, repo.url, repo.folder, repo.status, repo.ref) == (
        "octo/hello",
        "https://github.com/octo/hello",
        "hello",
        "pending",
        None,
    )
    other = await coding.attach(
        org_id=org, session_id=session, user_id=mia, repo="octo/hello", ref="dev", folder="h2"
    )
    assert (other.ref, other.folder) == ("dev", "h2")
    with pytest.raises(CodingError, match="folder hello is in use") as taken:
        await coding.attach(org_id=org, session_id=session, user_id=mia, repo="octo/hello")
    assert taken.value.status == 409
    state = await coding.state(org_id=org, session_id=session)
    assert [r.folder for r in state.repos] == ["hello", "h2"]
    assert (state.plan_mode, state.plan) == (False, None)

    await coding.detach(org_id=org, session_id=session, user_id=mia, repo_id=other.id)
    with pytest.raises(CodingError) as missing:
        await coding.detach(org_id=org, session_id=session, user_id=mia, repo_id=other.id)
    assert missing.value.status == 404
    assert await _actions(engine, tenant) == [
        "session.repo_attached",
        "session.repo_attached",
        "session.repo_detached",
    ]


@pytest.mark.parametrize(
    ("repo", "ref", "folder"),
    [
        ("https://gitlab.com/a/b", None, None),
        ("a/b/c", None, None),
        ("a/b", "../x", None),
        ("a/b", None, ".."),
        ("a/b", None, "x/y"),
    ],
)
async def test_attach_refuses_bad_input(
    coding: CodingService, tenant: Tenant, repo: str, ref: str | None, folder: str | None
) -> None:
    with pytest.raises(CodingError) as error:
        await coding.attach(
            org_id=tenant.org,
            session_id=tenant.ids["mine"],
            user_id=None,
            repo=repo,
            ref=ref,
            folder=folder,
        )
    assert error.value.status == 422


async def test_a_session_has_at_most_five_repositories(
    coding: CodingService, engine: AsyncEngine, tenant: Tenant
) -> None:
    session = tenant.ids["mine"]
    for number in range(MAX_SESSION_REPOS):
        await coding.attach(
            org_id=tenant.org, session_id=session, user_id=None, repo=f"octo/r{number}"
        )
    with pytest.raises(CodingError, match="at most 5"):
        await coding.attach(org_id=tenant.org, session_id=session, user_id=None, repo="octo/x")
    async with engine.begin() as connection:
        await connection.execute(
            update(Session).where(Session.id == tenant.ids["s1"]).values(status="archived")
        )
    with pytest.raises(CodingError, match="not active"):
        await coding.attach(
            org_id=tenant.org, session_id=tenant.ids["s1"], user_id=None, repo="octo/x"
        )


async def test_the_clone_result_updates_a_pending_repository(
    coding: CodingService, tenant: Tenant
) -> None:
    org, session = tenant.org, tenant.ids["mine"]
    one = await coding.attach(org_id=org, session_id=session, user_id=None, repo="octo/one")
    two = await coding.attach(org_id=org, session_id=session, user_id=None, repo="octo/two")
    assert [r.id for r in await coding.pending_repos(org_id=org, session_id=session)] == [
        one.id,
        two.id,
    ]
    await coding.cloned(
        org_id=org, repo_id=one.id, path="/w/one", commit="a" * 40, branch="main", proxied=True
    )
    await coding.clone_failed(org_id=org, repo_id=two.id, error="does not exist or is not public")
    state = await coding.state(org_id=org, session_id=session)
    first, second = state.repos
    assert (first.status, first.path, first.proxied, first.branch) == (
        "ready",
        "/w/one",
        True,
        "main",
    )
    assert (second.status, second.error) == ("failed", "does not exist or is not public")
    assert await coding.pending_repos(org_id=org, session_id=session) == []


async def test_the_plan_flow(coding: CodingService, engine: AsyncEngine, tenant: Tenant) -> None:
    org, session, mia = tenant.org, tenant.ids["mine"], tenant.ids["mia"]
    first = await coding.propose(org_id=org, session_id=session, run_id=None, body=" 1. a\n")
    assert (first.version, first.status, first.body) == (1, "proposed", "1. a")
    assert await coding.plan_mode(org_id=org, session_id=session)
    second = await coding.propose(org_id=org, session_id=session, run_id=None, body="1. b")
    assert second.version == 2
    listed = await coding.plans(org_id=org, session_id=session)
    assert [(p.version, p.status) for p in listed] == [(2, "proposed"), (1, "superseded")]
    with pytest.raises(CodingError, match="superseded"):
        await coding.approve(org_id=org, session_id=session, user_id=mia, plan_id=first.id)

    decided = await coding.approve(
        org_id=org, session_id=session, user_id=mia, plan_id=second.id, body="1. b\n2. c"
    )
    assert (decided.plan.status, decided.plan.edited, decided.plan.body) == (
        "approved",
        True,
        "1. b\n2. c",
    )
    assert decided.plan.proposed_body == "1. b"
    assert decided.message is not None
    assert decided.message.endswith("1. b\n2. c")
    assert "edited the plan" in decided.message
    assert not await coding.plan_mode(org_id=org, session_id=session)
    with pytest.raises(CodingError, match="approved"):
        await coding.reject(org_id=org, session_id=session, user_id=mia, plan_id=second.id)

    third = await coding.propose(org_id=org, session_id=session, run_id=None, body="1. d")
    rejected = await coding.reject(
        org_id=org, session_id=session, user_id=mia, plan_id=third.id, feedback="add tests"
    )
    assert (rejected.plan.status, rejected.plan.feedback) == ("rejected", "add tests")
    assert rejected.message is not None
    assert "add tests" in rejected.message
    assert await coding.plan_mode(org_id=org, session_id=session)
    fourth = await coding.propose(org_id=org, session_id=session, run_id=None, body="1. e")
    silent = await coding.reject(org_id=org, session_id=session, user_id=mia, plan_id=fourth.id)
    assert silent.message is None
    actions = await _actions(engine, tenant)
    assert actions.count("plan.proposed") == 4
    assert {"plan.approved", "plan.rejected"} <= set(actions)


async def test_an_approve_without_an_edit_is_not_edited(
    coding: CodingService, tenant: Tenant
) -> None:
    org, session = tenant.org, tenant.ids["mine"]
    plan = await coding.propose(org_id=org, session_id=session, run_id=None, body="1. a")
    same = await coding.approve(
        org_id=org, session_id=session, user_id=None, plan_id=plan.id, body="1. a"
    )
    assert not same.plan.edited
    assert same.message == approved_message(same.plan)
    assert "edited" not in same.message


async def test_plan_checks(coding: CodingService, tenant: Tenant) -> None:
    org, session = tenant.org, tenant.ids["mine"]
    with pytest.raises(CodingError, match="empty"):
        await coding.propose(org_id=org, session_id=session, run_id=None, body="  ")
    with pytest.raises(CodingError, match="20000"):
        await coding.propose(org_id=org, session_id=session, run_id=None, body="x" * 20_001)
    with pytest.raises(CodingError) as missing:
        await coding.approve(org_id=org, session_id=session, user_id=None, plan_id=uuid.uuid4())
    assert missing.value.status == 404
    plan = await coding.propose(org_id=org, session_id=session, run_id=None, body="1. a")
    with pytest.raises(CodingError, match="empty"):
        await coding.approve(
            org_id=org, session_id=session, user_id=None, plan_id=plan.id, body=" "
        )


async def test_start_plan_turns_plan_mode_on_and_sends_the_task(
    coding: CodingService, engine: AsyncEngine, tenant: Tenant
) -> None:
    org, session, mia = tenant.org, tenant.ids["mine"], tenant.ids["mia"]
    run_id = await coding.start_plan(org_id=org, session_id=session, user_id=mia, task="fix bug")
    assert isinstance(run_id, uuid.UUID)
    assert await coding.plan_mode(org_id=org, session_id=session)
    async with engine.connect() as connection:
        texts = (
            (await connection.execute(select(Message.content).where(Message.session_id == session)))
            .scalars()
            .all()
        )
    assert "fix bug" in [str(text) for text in texts] or any("fix bug" in str(t) for t in texts)
    assert await coding.chat_run(org_id=org, run_id=run_id) == "fake"
    assert await coding.chat_run(org_id=org, run_id=uuid.uuid4()) is None
    await coding.set_plan_mode(org_id=org, session_id=session, user_id=mia, on=False)
    await coding.set_plan_mode(org_id=org, session_id=session, user_id=mia, on=False)
    assert (await _actions(engine, tenant)).count("session.plan_mode") == 2


def test_texts() -> None:
    assert describe_plan(None, False).startswith("Plan mode is off.")
    text = pull_request_message(repo="o/n", title="Fix", base="main", draft=True)
    assert "o/n" in text
    assert "pr_create" in text
    assert "Fix" in text
    assert "draft" in text
    assert "sama/<short-name>" in pull_request_message(
        repo=None, title=None, base=None, draft=False
    )


class FullQueue(RunQueue):
    async def enqueue(self, **_: Any) -> uuid.UUID:
        raise RateLimitedError("too many messages", 30)


async def test_a_refused_message_undoes_the_decision(engine: AsyncEngine, tenant: Tenant) -> None:
    coding = CodingService(engine, FullQueue(engine))
    org, session, mia = tenant.org, tenant.ids["mine"], tenant.ids["mia"]
    plan = await coding.propose(org_id=org, session_id=session, run_id=None, body="1. a")
    with pytest.raises(RateLimitedError):
        await coding.decide(
            org_id=org, session_id=session, user_id=mia, plan_id=plan.id, approve=True, text="1. b"
        )
    (back,) = await coding.plans(org_id=org, session_id=session)
    assert (back.status, back.body, back.edited, back.decided_at) == (
        "proposed",
        "1. a",
        False,
        None,
    )
    assert await coding.plan_mode(org_id=org, session_id=session)
    with pytest.raises(RateLimitedError):
        await coding.decide(
            org_id=org, session_id=session, user_id=mia, plan_id=plan.id, approve=False, text="no"
        )
    (back,) = await coding.plans(org_id=org, session_id=session)
    assert (back.status, back.feedback) == ("proposed", None)
    # A reject without feedback sends nothing, so it stays.
    decided, run_id = await coding.decide(
        org_id=org, session_id=session, user_id=mia, plan_id=plan.id, approve=False
    )
    assert (decided.plan.status, run_id) == ("rejected", None)
    assert (await _actions(engine, tenant)).count("plan.decision_undone") == 2


class SlowFullQueue(RunQueue):
    """Refuses the message, after a newer plan came in the meantime."""

    def __init__(self, engine: AsyncEngine, coding: list[CodingService]) -> None:
        super().__init__(engine)
        self.coding = coding

    async def enqueue(self, *, org_id: uuid.UUID, session_id: uuid.UUID, **_: Any) -> uuid.UUID:
        await self.coding[0].propose(org_id=org_id, session_id=session_id, run_id=None, body="v2")
        raise RateLimitedError("too many messages", 30)


async def test_the_undo_keeps_a_newer_plan(engine: AsyncEngine, tenant: Tenant) -> None:
    holder: list[CodingService] = []
    coding = CodingService(engine, SlowFullQueue(engine, holder))
    holder.append(coding)
    org, session = tenant.org, tenant.ids["mine"]
    plan = await coding.propose(org_id=org, session_id=session, run_id=None, body="v1")
    with pytest.raises(RateLimitedError):
        await coding.decide(
            org_id=org, session_id=session, user_id=None, plan_id=plan.id, approve=True
        )
    newest, old = await coding.plans(org_id=org, session_id=session)
    assert (newest.version, newest.status) == (2, "proposed")
    assert (old.version, old.status) == (1, "approved")
