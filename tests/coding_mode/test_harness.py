"""``HarnessHooks``: coding mode for the runtimes ``claude_code`` and ``codex`` (Section 15.5)."""

from __future__ import annotations

import uuid
from typing import Any

from sama.coding_mode.harness import HarnessHooks, plan_mode_for
from sama.coding_mode.prepare import CodingPreparer
from sama.coding_mode.service import CodingService
from sama.tenancy.run_token import RunTokenSigner
from tests.coding_mode.test_prepare import KEY, FakeCloner, _run
from tests.orchestrator.conftest import Tenant


class Snapshots:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    async def snapshot(
        self, *, org_id: uuid.UUID, session_id: uuid.UUID, run_id: str, tool: str
    ) -> bool:
        self.calls.append((run_id, tool))
        if self.fail:
            raise RuntimeError("no sandbox")
        return True


def _hooks(coding: CodingService, snapshots: Any = None) -> HarnessHooks:
    preparer = CodingPreparer(coding, FakeCloner(), RunTokenSigner(KEY))  # type: ignore[arg-type]
    return HarnessHooks(preparer, coding, snapshots)


async def test_before_clones_notes_and_takes_a_checkpoint(
    coding: CodingService, tenant: Tenant
) -> None:
    org, session = tenant.org, tenant.ids["mine"]
    await coding.attach(org_id=org, session_id=session, user_id=None, repo="team/app")
    snapshots = Snapshots()
    hooks = _hooks(coding, snapshots)
    scope, token = await _run(coding, tenant)
    assert not await hooks.planning(scope)
    prep = await hooks.before(object(), scope, token, "codex", False)  # type: ignore[arg-type]
    assert "https://github.com/team/app" in prep.note
    assert (prep.plan_mode, prep.plan_args) == (False, ())
    assert snapshots.calls == []
    await hooks.checkpoint(scope, "codex")
    assert snapshots.calls == [(scope.run_id, "codex")]


async def test_plan_mode_gives_the_cli_flags_and_stores_the_plan(
    coding: CodingService, tenant: Tenant
) -> None:
    org, session = tenant.org, tenant.ids["mine"]
    await coding.set_plan_mode(org_id=org, session_id=session, user_id=None, on=True)
    hooks = _hooks(coding, Snapshots(fail=True))
    scope, token = await _run(coding, tenant)
    assert await hooks.planning(scope)
    prep = await hooks.before(object(), scope, token, "claude_code", True)  # type: ignore[arg-type]
    assert prep.plan_args == ("--permission-mode", "plan")
    assert "ExitPlanMode" in prep.note
    codex = await hooks.before(object(), scope, token, "codex", True)  # type: ignore[arg-type]
    assert codex.plan_args == ()
    assert "whole answer" in codex.note
    await hooks.checkpoint(scope, "codex")  # a failed checkpoint only logs
    line = await hooks.plan(scope, "1. a\n2. b")
    assert line.startswith("Plan version 1 is saved.")
    state = await coding.state(org_id=org, session_id=session)
    assert state.plan is not None
    assert (state.plan.body, str(state.plan.run_id)) == ("1. a\n2. b", scope.run_id)
    assert state.plan_mode


async def test_a_plan_that_cannot_be_stored_gives_a_line(
    coding: CodingService, tenant: Tenant
) -> None:
    hooks = _hooks(coding)
    scope, _token = await _run(coding, tenant)
    broken = scope.model_copy(update={"session_id": "not-a-uuid"})
    assert "could not save the plan" in await hooks.plan(broken, "1. a")
    assert not await hooks.planning(broken)


def test_codex_has_no_cli_plan_flags() -> None:
    assert plan_mode_for("codex").cli_args == ()
    assert plan_mode_for("claude_code").native_plan_tool == "ExitPlanMode"


async def test_the_plan_note_follows_the_judged_plan_mode(
    coding: CodingService, tenant: Tenant
) -> None:
    """Plan mode went off during the approval: the run that the gate judged as a plan run
    still gets the plan rules."""
    hooks = _hooks(coding)
    scope, token = await _run(coding, tenant)
    assert not await hooks.planning(scope)
    prep = await hooks.before(object(), scope, token, "codex", True)  # type: ignore[arg-type]
    assert prep.plan_mode
    assert "whole answer" in prep.note
    off = await hooks.before(object(), scope, token, "codex", False)  # type: ignore[arg-type]
    assert "Plan mode" not in off.note


class EmptyPreparer:
    """A preparer whose reads failed: it gives no note."""

    async def prepare(self, *args: Any, **kwargs: Any) -> str:
        return ""


async def test_the_plan_rules_stay_when_the_preparer_gives_nothing(
    coding: CodingService, tenant: Tenant
) -> None:
    hooks = HarnessHooks(EmptyPreparer(), coding)
    scope, token = await _run(coding, tenant)
    prep = await hooks.before(object(), scope, token, "codex", True)  # type: ignore[arg-type]
    assert prep.note.startswith("[sama coding mode] Plan mode is on.")
    assert "whole answer" in prep.note
    plain = await hooks.before(object(), scope, token, "codex", False)  # type: ignore[arg-type]
    assert plain.note == ""
