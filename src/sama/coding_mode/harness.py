"""Coding mode for the coding agent runtimes ``claude_code`` and ``codex`` of M7.2
(docs/architecture.md, Sections 15.4 and 15.5).

These runtimes run a CLI in the session sandbox, and the runner does not start it, so the
coding step of ``SandboxedRuntime`` does not run for them. ``CodingAgentRuntime`` calls
``HarnessHooks`` instead:

- ``before``: after it has the session sandbox and before the CLI starts. It clones the
  attached repositories and gives the note (``CodingPreparer``), takes one checkpoint of the
  work folder (the CLI tools do not pass the gate of the runner, so they take no checkpoints),
  and reads plan mode. In plan mode, Claude Code starts with ``--permission-mode plan``. Codex
  gets only the plan-first prompt: its own read-only sandbox (bubblewrap) does not work in the
  E2B VM, and with it every command fails.
- ``plan``: after the CLI ended in plan mode. The plan of Claude Code (``ExitPlanMode``), else
  the answer of the CLI, becomes a proposed plan (``CodingService.propose``). A person decides
  it as any other plan.

The direct mode of M7.2 (``sessions.coding_agent``) and a one-off run (``runs.coding_agent``)
both run on these runtimes, so they get the same steps.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Final, Protocol

from sama.coding.runtime import CodingModePrep
from sama.runtime.contract import RunScope
from sama.sandbox.base import Sandbox

log: Final = logging.getLogger("sama.coding_mode.harness")


@dataclass(frozen=True, slots=True)
class HarnessPlanMode:
    cli_args: tuple[str, ...]
    """Arguments for the CLI while plan mode is on."""
    prompt: str
    """Text after the task while plan mode is on."""
    native_plan_tool: str | None
    """The tool of the CLI that ends its plan. Its input is the plan for ``propose``."""


CLAUDE_CODE: Final = "claude_code"
CODEX: Final = "codex"

_CLAUDE_PLAN: Final = (
    "Plan mode is on. Do not change files. Read the code, then write the plan: the goal, the"
    " files to change, the steps, and the tests. End with ExitPlanMode and the plan."
)
_ANSWER_PLAN: Final = (
    "Plan mode is on. Do not change files, and do not run commands that change files. Read"
    " the code, then give the plan as your whole answer: the goal, the files to change, the"
    " steps, and the tests. The user approves, edits, or rejects it."
)

HARNESS_PLAN_MODES: Final = {
    CLAUDE_CODE: HarnessPlanMode(
        cli_args=("--permission-mode", "plan"),
        prompt=_CLAUDE_PLAN,
        native_plan_tool="ExitPlanMode",
    ),
    CODEX: HarnessPlanMode(cli_args=(), prompt=_ANSWER_PLAN, native_plan_tool=None),
}


def plan_mode_for(harness: str) -> HarnessPlanMode:
    """The plan mode of a harness. A harness that is not known gets the answer prompt and no
    CLI arguments."""
    return HARNESS_PLAN_MODES.get(
        harness, HarnessPlanMode(cli_args=(), prompt=_ANSWER_PLAN, native_plan_tool=None)
    )


class _Preparer(Protocol):
    async def prepare(
        self, sandbox: Sandbox, scope: RunScope, token: str, harness: str | None = None
    ) -> str: ...


class _Plans(Protocol):
    async def plan_mode(self, *, org_id: uuid.UUID, session_id: uuid.UUID) -> bool: ...

    async def propose(
        self, *, org_id: uuid.UUID, session_id: uuid.UUID, run_id: uuid.UUID | None, body: str
    ) -> object: ...


class _Snapshots(Protocol):
    async def snapshot(
        self, *, org_id: uuid.UUID, session_id: uuid.UUID, run_id: str, tool: str
    ) -> bool: ...


class HarnessHooks:
    """``CodingModeHooks`` of ``sama.coding.runtime``. No method raises."""

    def __init__(
        self, preparer: _Preparer, plans: _Plans, snapshots: _Snapshots | None = None
    ) -> None:
        self._preparer = preparer
        self._plans = plans
        self._snapshots = snapshots

    async def planning(self, scope: RunScope) -> bool:
        try:
            return await self._plans.plan_mode(
                org_id=uuid.UUID(scope.org_id), session_id=uuid.UUID(scope.session_id)
            )
        except Exception as error:
            log.warning("plan mode was not read run=%s: %r", scope.run_id, error)
            return False

    async def before(
        self, sandbox: Sandbox, scope: RunScope, token: str, agent: str, plan: bool
    ) -> CodingModePrep:
        """``plan`` is the plan mode that the gate judged for this run."""
        note = await self._preparer.prepare(sandbox, scope, token, harness=agent)
        if self._snapshots is not None:
            try:
                await self._snapshots.snapshot(
                    org_id=uuid.UUID(scope.org_id),
                    session_id=uuid.UUID(scope.session_id),
                    run_id=scope.run_id,
                    tool=agent,
                )
            except Exception as error:
                log.warning("no checkpoint before the CLI run=%s: %r", scope.run_id, error)
        args = plan_mode_for(agent).cli_args if plan else ()
        return CodingModePrep(note=note, plan_mode=plan, plan_args=args)

    async def plan(self, scope: RunScope, text: str) -> str:
        try:
            stored = await self._plans.propose(
                org_id=uuid.UUID(scope.org_id),
                session_id=uuid.UUID(scope.session_id),
                run_id=uuid.UUID(scope.run_id),
                body=text,
            )
        except Exception as error:
            log.warning("the plan was not stored run=%s: %r", scope.run_id, error)
            return "sama could not save the plan. Send /plan to see the state of plan mode."
        version = getattr(stored, "version", "")
        return (
            f"Plan version {version} is saved. Send /plan approve, /plan edit NEW PLAN, or"
            " /plan reject FEEDBACK, or use the Code page."
        )
