"""The coding step before a run (Section 15.5): clone the attached repositories and give the
note to the run."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

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
