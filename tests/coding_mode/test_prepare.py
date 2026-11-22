"""The coding step before a run (Section 15.5): clone the attached repositories and give the
note to the run."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.coding_mode.prepare import PLAN_MODE_NOTE, CodingPreparer
from sama.coding_mode.service import CodingService
from sama.orchestrator.queue import RunQueue
from sama.runtime.contract import Audience, RunScope
from sama.sandbox.base import Sandbox
from sama.tenancy.run_token import RunTokenClaims, RunTokenSigner
from sama.tools.repos import Cloned, RepoError
from tests.orchestrator.conftest import Tenant

KEY = b"k" * 32


class FakeCloner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def clone(
        self,
        claims: RunTokenClaims,
        repo: str,
        *,
        ref: str | None = None,
        folder: str | None = None,
        token: str | None = None,
        sandbox: Sandbox | None = None,
    ) -> Cloned:
        self.calls.append({"repo": repo, "ref": ref, "folder": folder, "token": token})
        if repo.endswith("/missing"):
            raise RepoError("octo/missing does not exist or is not public")
        return Cloned(
            url=f"https://github.com/{repo}",
            path=f"/home/user/work/{folder}",
            commit="c" * 40,
            proxied=repo.startswith("team/"),
            branch=ref or "main",
            instructions=(),
        )


async def _run(coding: CodingService, tenant: Tenant) -> tuple[RunScope, str]:
    run_id = await coding.queue.enqueue(
        org_id=tenant.org, session_id=tenant.ids["mine"], text="go", user_id=None
    )
    scope = RunScope(
        org_id=str(tenant.org),
        workspace_id=str(tenant.ids["ws"]),
        session_id=str(tenant.ids["mine"]),
        run_id=str(run_id),
        audience=Audience.PRIVATE,
        user_id=str(tenant.ids["mia"]),
    )
    return scope, RunTokenSigner(KEY).issue(scope, ttl_s=60)


async def test_the_step_clones_pending_repositories_once(
    coding: CodingService, tenant: Tenant
) -> None:
    org, session = tenant.org, tenant.ids["mine"]
    await coding.attach(org_id=org, session_id=session, user_id=None, repo="team/app", ref="dev")
    await coding.attach(org_id=org, session_id=session, user_id=None, repo="octo/missing")
    cloner = FakeCloner()
    preparer = CodingPreparer(coding, cloner, RunTokenSigner(KEY))  # type: ignore[arg-type]
    scope, token = await _run(coding, tenant)
    note = await preparer.prepare(object(), scope, token)  # type: ignore[arg-type]
    assert [call["repo"] for call in cloner.calls] == ["team/app", "octo/missing"]
    assert cloner.calls[0]["token"] == token
    assert cloner.calls[0]["ref"] == "dev"
    assert "https://github.com/team/app (branch dev" in note
    assert "/home/user/work/app" in note
    assert "push sama/*" in note
    assert "could not clone https://github.com/octo/missing" in note
    assert PLAN_MODE_NOTE not in note
    state = await coding.state(org_id=org, session_id=session)
    assert [(r.status, r.proxied) for r in state.repos] == [("ready", True), ("failed", None)]

    # The next run has nothing to clone; plan mode adds its note.
    await coding.set_plan_mode(org_id=org, session_id=session, user_id=None, on=True)
    scope, token = await _run(coding, tenant)
    assert await preparer.prepare(object(), scope, token) == PLAN_MODE_NOTE  # type: ignore[arg-type]
    assert len(cloner.calls) == 2


async def test_the_step_skips_other_runs_and_never_raises(
    coding: CodingService, tenant: Tenant
) -> None:
    preparer = CodingPreparer(coding, FakeCloner(), RunTokenSigner(KEY))  # type: ignore[arg-type]
    await coding.set_plan_mode(
        org_id=tenant.org, session_id=tenant.ids["mine"], user_id=None, on=True
    )
    scope, token = await _run(coding, tenant)
    unknown = scope.model_copy(update={"run_id": str(uuid.uuid4())})
    assert await preparer.prepare(object(), unknown, token) == ""  # type: ignore[arg-type]
    broken = scope.model_copy(update={"org_id": "not-a-uuid"})
    assert await preparer.prepare(object(), broken, token) == ""  # type: ignore[arg-type]


async def test_a_queue_is_shared(coding: CodingService) -> None:
    assert isinstance(coding.queue, RunQueue)


async def test_the_plan_tool_stores_the_plan(coding: CodingService, tenant: Tenant) -> None:
    from sama.coding_mode.tool import PlanTool
    from sama.tools.plan import PlanError

    scope, token = await _run(coding, tenant)
    claims = RunTokenSigner(KEY).verify(token)
    answer = await PlanTool(coding).propose(claims, "1. add the route", "Health route")
    assert answer.startswith("The plan is saved as version 1")
    state = await coding.state(org_id=tenant.org, session_id=tenant.ids["mine"])
    assert state.plan_mode
    assert state.plan is not None
    assert state.plan.body == "# Health route\n\n1. add the route"
    assert str(state.plan.run_id) == scope.run_id
    with pytest.raises(PlanError, match="20000"):
        await PlanTool(coding).propose(claims, "x" * 20_001, None)


class SlowOrBrokenCloner(FakeCloner):
    async def clone(self, claims: RunTokenClaims, repo: str, **kwargs: Any) -> Cloned:
        if repo.endswith("/slow"):
            await asyncio.sleep(5)
        if repo.endswith("/broken"):
            raise RuntimeError("the sandbox went away")
        return await super().clone(claims, repo, **kwargs)


async def test_a_slow_or_broken_clone_fails_only_its_repository(
    coding: CodingService, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sama.coding_mode import prepare as module

    monkeypatch.setattr(module, "CLONE_STEP_TIMEOUT_S", 0.05)
    org, session = tenant.org, tenant.ids["mine"]
    for name in ("octo/slow", "octo/broken", "octo/fine"):
        await coding.attach(org_id=org, session_id=session, user_id=None, repo=name)
    await coding.set_plan_mode(org_id=org, session_id=session, user_id=None, on=True)
    preparer = CodingPreparer(coding, SlowOrBrokenCloner(), RunTokenSigner(KEY))  # type: ignore[arg-type]
    scope, token = await _run(coding, tenant)
    note = await preparer.prepare(object(), scope, token)  # type: ignore[arg-type]
    assert "could not clone https://github.com/octo/slow: the clone took longer" in note
    assert "could not clone https://github.com/octo/broken: the clone failed" in note
    assert "https://github.com/octo/fine (branch main" in note
    assert note.endswith(PLAN_MODE_NOTE)
    state = await coding.state(org_id=org, session_id=session)
    assert [r.status for r in state.repos] == ["failed", "failed", "ready"]


async def test_a_run_after_plan_mode_off_gets_one_note(
    coding: CodingService, tenant: Tenant
) -> None:
    from sama.coding_mode.prepare import PLAN_OFF_NOTE

    org, session, mia = tenant.org, tenant.ids["mine"], tenant.ids["mia"]
    preparer = CodingPreparer(coding, FakeCloner(), RunTokenSigner(KEY))  # type: ignore[arg-type]

    async def note(scope: Any, token: str, **kwargs: Any) -> str:
        return await preparer.prepare(object(), scope, token, **kwargs)  # type: ignore[arg-type]

    # A run queued before the change, and two runs queued after it.
    early, early_token = await _run(coding, tenant)
    assert await note(early, early_token) == ""
    await coding.set_plan_mode(org_id=org, session_id=session, user_id=mia, on=True)
    await coding.set_plan_mode(org_id=org, session_id=session, user_id=mia, on=False)
    first, first_token = await _run(coding, tenant)
    second, second_token = await _run(coding, tenant)
    # The first run that prepares gets the note, even when it was queued earlier.
    assert await note(early, early_token) == PLAN_OFF_NOTE
    assert await note(second, second_token) == ""
    assert await note(first, first_token) == ""
    # A retry of the run that got it gets it again.
    assert await note(early, early_token) == PLAN_OFF_NOTE

    # A harness run that the gate judged as a plan run does not claim the note.
    await coding.set_plan_mode(org_id=org, session_id=session, user_id=mia, on=True)
    await coding.set_plan_mode(org_id=org, session_id=session, user_id=mia, on=False)
    planned, planned_token = await _run(coding, tenant)
    assert PLAN_OFF_NOTE not in await note(planned, planned_token, plan=False, off_note=False)
    after, after_token = await _run(coding, tenant)
    assert await note(after, after_token) == PLAN_OFF_NOTE


async def test_two_runs_that_prepare_at_once_give_one_note(
    coding: CodingService, tenant: Tenant
) -> None:
    import asyncio

    from sama.coding_mode.prepare import PLAN_OFF_NOTE

    org, session, mia = tenant.org, tenant.ids["mine"], tenant.ids["mia"]
    preparer = CodingPreparer(coding, FakeCloner(), RunTokenSigner(KEY))  # type: ignore[arg-type]
    await coding.set_plan_mode(org_id=org, session_id=session, user_id=mia, on=True)
    await coding.set_plan_mode(org_id=org, session_id=session, user_id=mia, on=False)
    runs = [await _run(coding, tenant) for _ in range(3)]
    notes = await asyncio.gather(
        *(preparer.prepare(object(), scope, token) for scope, token in runs)  # type: ignore[arg-type]
    )
    assert notes.count(PLAN_OFF_NOTE) == 1


async def test_a_compact_run_does_not_take_the_off_note(
    coding: CodingService, engine: AsyncEngine, tenant: Tenant
) -> None:
    from sqlalchemy import update

    from sama.coding_mode.prepare import PLAN_OFF_NOTE
    from sama.db.models import Run

    org, session, mia = tenant.org, tenant.ids["mine"], tenant.ids["mia"]
    preparer = CodingPreparer(coding, FakeCloner(), RunTokenSigner(KEY))  # type: ignore[arg-type]
    await coding.set_plan_mode(org_id=org, session_id=session, user_id=mia, on=True)
    await coding.set_plan_mode(org_id=org, session_id=session, user_id=mia, on=False)
    compact, compact_token = await _run(coding, tenant)
    async with engine.begin() as connection:
        await connection.execute(
            update(Run).where(Run.id == uuid.UUID(compact.run_id)).values(compact=True)
        )
    assert await preparer.prepare(object(), compact, compact_token) == ""  # type: ignore[arg-type]
    scope, token = await _run(coding, tenant)
    assert await preparer.prepare(object(), scope, token) == PLAN_OFF_NOTE  # type: ignore[arg-type]


async def test_an_approval_does_not_give_the_off_note(
    coding: CodingService, tenant: Tenant
) -> None:
    org, session = tenant.org, tenant.ids["mine"]
    plan = await coding.propose(org_id=org, session_id=session, run_id=None, body="1. a")
    await coding.approve(org_id=org, session_id=session, user_id=None, plan_id=plan.id)
    preparer = CodingPreparer(coding, FakeCloner(), RunTokenSigner(KEY))  # type: ignore[arg-type]
    scope, token = await _run(coding, tenant)
    assert await preparer.prepare(object(), scope, token) == ""  # type: ignore[arg-type]


class Folders:
    """A sandbox whose ``test -e`` finds only the given paths."""

    def __init__(self, present: set[str], broken: bool = False, code: int = 1) -> None:
        self.present = present
        self.broken = broken
        self.code = code
        self.looked: list[str] = []

    async def exec(self, argv: list[str], **kwargs: Any) -> Any:
        from sama.sandbox.base import ExecResult

        if self.broken:
            raise RuntimeError("the sandbox is busy")
        assert argv[:2] == ["test", "-e"]
        self.looked.append(argv[2])
        code = 0 if argv[2] in self.present else self.code
        return ExecResult(exit_code=code, stdout="", stderr="")


async def test_a_lost_folder_clones_again(coding: CodingService, tenant: Tenant) -> None:
    """A new sandbox (after a pause, a crash, or a deploy) has an empty work folder. The run
    clones each ready repository whose folder is gone, and leaves the others."""
    org, session = tenant.org, tenant.ids["mine"]
    gone = await coding.attach(org_id=org, session_id=session, user_id=None, repo="team/gone")
    kept = await coding.attach(org_id=org, session_id=session, user_id=None, repo="team/kept")
    for repo in (gone, kept):
        await coding.cloned(
            org_id=org,
            repo_id=repo.id,
            path=f"/home/user/work/{repo.folder}",
            commit="a" * 40,
            branch="main",
            proxied=True,
        )
    cloner = FakeCloner()
    preparer = CodingPreparer(coding, cloner, RunTokenSigner(KEY))  # type: ignore[arg-type]
    sandbox = Folders({"/home/user/work/kept"})
    scope, token = await _run(coding, tenant)
    note = await preparer.prepare(sandbox, scope, token)  # type: ignore[arg-type]
    assert [call["repo"] for call in cloner.calls] == ["team/gone"]
    assert "https://github.com/team/gone" in note
    assert "team/kept" not in note
    state = await coding.state(org_id=org, session_id=session)
    assert [(r.folder, r.status, r.commit) for r in state.repos] == [
        ("gone", "ready", "c" * 40),
        ("kept", "ready", "a" * 40),
    ]
    assert set(sandbox.looked) == {"/home/user/work/gone", "/home/user/work/kept"}


async def test_a_failed_folder_check_keeps_the_repository(
    coding: CodingService, tenant: Tenant
) -> None:
    org, session = tenant.org, tenant.ids["mine"]
    repo = await coding.attach(org_id=org, session_id=session, user_id=None, repo="team/app")
    await coding.cloned(
        org_id=org,
        repo_id=repo.id,
        path="/home/user/work/app",
        commit="a" * 40,
        branch="main",
        proxied=True,
    )
    cloner = FakeCloner()
    preparer = CodingPreparer(coding, cloner, RunTokenSigner(KEY))  # type: ignore[arg-type]
    scope, token = await _run(coding, tenant)
    assert await preparer.prepare(Folders(set(), broken=True), scope, token) == ""  # type: ignore[arg-type]
    assert cloner.calls == []
    (kept,) = (await coding.state(org_id=org, session_id=session)).repos
    assert (kept.status, kept.commit) == ("ready", "a" * 40)
    # A second reopen of the same repository does nothing.
    assert await coding.reopen(org_id=org, repo_id=repo.id) is not None
    assert await coding.reopen(org_id=org, repo_id=repo.id) is None
    assert kept.updated_at is not None


class SlowCloner(FakeCloner):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.go = asyncio.Event()

    async def clone(self, claims: RunTokenClaims, repo: str, **kwargs: Any) -> Cloned:
        self.started.set()
        await self.go.wait()
        return await super().clone(claims, repo, **kwargs)


async def _ready(coding: CodingService, tenant: Tenant, name: str) -> Any:
    org, session = tenant.org, tenant.ids["mine"]
    repo = await coding.attach(org_id=org, session_id=session, user_id=None, repo=name)
    await coding.cloned(
        org_id=org,
        repo_id=repo.id,
        path=f"/home/user/work/{repo.folder}",
        commit="a" * 40,
        branch="main",
        proxied=True,
    )
    return repo


async def test_two_runs_recover_a_lost_folder_once(coding: CodingService, tenant: Tenant) -> None:
    """Two runs start at once in a new sandbox: one run clones the repository again; the
    other does not clone it and does not mark it failed."""
    await _ready(coding, tenant, "team/app")
    cloner = SlowCloner()
    preparer = CodingPreparer(coding, cloner, RunTokenSigner(KEY))  # type: ignore[arg-type]
    first, first_token = await _run(coding, tenant)
    second, second_token = await _run(coding, tenant)
    one = asyncio.ensure_future(preparer.prepare(Folders(set()), first, first_token))  # type: ignore[arg-type]
    await asyncio.wait_for(cloner.started.wait(), timeout=10)
    other = await preparer.prepare(Folders(set()), second, second_token)  # type: ignore[arg-type]
    cloner.go.set()
    note = await asyncio.wait_for(one, timeout=10)
    assert len(cloner.calls) == 1
    assert "https://github.com/team/app" in note
    assert "could not clone" not in other
    (repo,) = (await coding.state(org_id=tenant.org, session_id=tenant.ids["mine"])).repos
    assert (repo.status, repo.commit) == ("ready", "c" * 40)


async def test_a_stale_check_does_not_reopen_a_recovered_repository(
    coding: CodingService, tenant: Tenant
) -> None:
    org = tenant.org
    repo = await _ready(coding, tenant, "team/app")
    (seen,) = await coding.ready_repos(org_id=org, session_id=tenant.ids["mine"])
    reopened = await coding.reopen(org_id=org, repo_id=repo.id, seen=seen.updated_at)
    assert reopened is not None
    claim = await coding.claim(org_id=org, repo_id=repo.id)
    assert claim is not None
    # While the lease runs, a second claim loses.
    assert await coding.claim(org_id=org, repo_id=repo.id) is None
    # A failure stored with a lost claim changes nothing.
    await coding.clone_failed(org_id=org, repo_id=repo.id, error="x", claim=seen.updated_at)
    await coding.cloned(
        org_id=org,
        repo_id=repo.id,
        path="/w/app",
        commit="b" * 40,
        branch="main",
        proxied=True,
        claim=claim,
    )
    # The old observation of the folder (before the recovery) does not reopen it.
    assert await coding.reopen(org_id=org, repo_id=repo.id, seen=seen.updated_at) is None
    (now,) = (await coding.state(org_id=org, session_id=tenant.ids["mine"])).repos
    assert (now.status, now.commit) == ("ready", "b" * 40)


async def test_a_check_that_cannot_run_keeps_the_repository(
    coding: CodingService, tenant: Tenant
) -> None:
    await _ready(coding, tenant, "team/app")
    cloner = FakeCloner()
    preparer = CodingPreparer(coding, cloner, RunTokenSigner(KEY))  # type: ignore[arg-type]
    scope, token = await _run(coding, tenant)
    assert await preparer.prepare(Folders(set(), code=127), scope, token) == ""  # type: ignore[arg-type]
    assert cloner.calls == []
    (kept,) = (await coding.state(org_id=tenant.org, session_id=tenant.ids["mine"])).repos
    assert kept.status == "ready"


async def test_an_ended_lease_can_be_claimed_again(
    coding: CodingService, engine: AsyncEngine, tenant: Tenant
) -> None:
    from sqlalchemy import text as sql

    org = tenant.org
    repo = await coding.attach(
        org_id=org, session_id=tenant.ids["mine"], user_id=None, repo="team/app"
    )
    first = await coding.claim(org_id=org, repo_id=repo.id)
    assert first is not None
    assert await coding.claim(org_id=org, repo_id=repo.id) is None
    async with engine.begin() as connection:  # the run that held it crashed long ago
        await connection.execute(
            sql("UPDATE session_repos SET updated_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": repo.id},
        )
    assert await coding.claim(org_id=org, repo_id=repo.id) is not None
    # The crashed run's late result changes nothing.
    await coding.cloned(
        org_id=org,
        repo_id=repo.id,
        path="/w",
        commit="d" * 40,
        branch=None,
        proxied=False,
        claim=first,
    )
    (still,) = (await coding.state(org_id=org, session_id=tenant.ids["mine"])).repos
    assert still.status == "pending"


async def test_a_failed_claim_skips_only_that_repository(
    coding: CodingService, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sama.coding_mode.prepare import PLAN_MODE_NOTE

    org, session = tenant.org, tenant.ids["mine"]
    broken = await coding.attach(org_id=org, session_id=session, user_id=None, repo="team/one")
    await coding.attach(org_id=org, session_id=session, user_id=None, repo="team/two")
    await coding.set_plan_mode(org_id=org, session_id=session, user_id=None, on=True)
    claim = coding.claim

    async def flaky(*, org_id: uuid.UUID, repo_id: uuid.UUID) -> Any:
        if repo_id == broken.id:
            raise RuntimeError("the database went away")
        return await claim(org_id=org_id, repo_id=repo_id)

    monkeypatch.setattr(coding, "claim", flaky)
    cloner = FakeCloner()
    preparer = CodingPreparer(coding, cloner, RunTokenSigner(KEY))  # type: ignore[arg-type]
    scope, token = await _run(coding, tenant)
    note = await preparer.prepare(object(), scope, token)  # type: ignore[arg-type]
    assert [call["repo"] for call in cloner.calls] == ["team/two"]
    assert "https://github.com/team/two" in note
    assert note.endswith(PLAN_MODE_NOTE)
    one, two = (await coding.state(org_id=org, session_id=session)).repos
    assert (one.status, two.status) == ("pending", "ready")


# -- the first read (Section 4.4) ---------------------------------------------------------------


class CountingCoding:
    """Counts the calls of a ``CodingService``, so a test sees which reads the step made."""

    def __init__(self, coding: CodingService) -> None:
        self._coding = coding
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._coding, name)
        if not callable(target):
            return target

        async def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return await target(*args, **kwargs)

        return call


async def test_a_plain_run_needs_only_the_first_read(coding: CodingService, tenant: Tenant) -> None:
    counting = CountingCoding(coding)
    preparer = CodingPreparer(counting, FakeCloner(), RunTokenSigner(KEY))  # type: ignore[arg-type]
    scope, token = await _run(coding, tenant)
    look = await coding.look(
        org_id=tenant.org, session_id=tenant.ids["mine"], run_id=uuid.UUID(scope.run_id)
    )
    assert look.idle
    assert look.runtime is not None
    assert await preparer.prepare(object(), scope, token) == ""  # type: ignore[arg-type]
    assert counting.calls == ["look"]


async def test_the_first_read_sees_repositories_and_plan_mode(
    coding: CodingService, tenant: Tenant
) -> None:
    org, session = tenant.org, tenant.ids["mine"]
    scope, _ = await _run(coding, tenant)
    run_id = uuid.UUID(scope.run_id)
    await coding.attach(org_id=org, session_id=session, user_id=None, repo="team/app")
    look = await coding.look(org_id=org, session_id=session, run_id=run_id)
    assert (look.repos, look.plan_mode, look.plan_changes, look.idle) == (True, False, False, False)
    await coding.set_plan_mode(org_id=org, session_id=session, user_id=None, on=True)
    look = await coding.look(org_id=org, session_id=session, run_id=run_id)
    assert (look.plan_mode, look.plan_changes) == (True, True)
    # A run of another session, or an unknown run, is not a chat run of this session.
    other = await coding.look(org_id=org, session_id=tenant.ids["s1"], run_id=run_id)
    assert other.runtime is None
    assert other.idle


async def test_the_note_that_plan_mode_is_off_still_comes_after_the_first_read(
    coding: CodingService, tenant: Tenant
) -> None:
    org, session = tenant.org, tenant.ids["mine"]
    await coding.set_plan_mode(org_id=org, session_id=session, user_id=None, on=True)
    await coding.set_plan_mode(org_id=org, session_id=session, user_id=None, on=False)
    preparer = CodingPreparer(coding, FakeCloner(), RunTokenSigner(KEY))  # type: ignore[arg-type]
    scope, token = await _run(coding, tenant)
    note = await preparer.prepare(object(), scope, token)  # type: ignore[arg-type]
    assert "Plan mode is off now" in note
