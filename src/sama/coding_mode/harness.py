"""Plan mode for the coding agent harnesses (docs/architecture.md, Section 15.5).

Roadmap M7.2 adds the harnesses ``claude_code`` and ``codex``: the session talks straight to
the Claude Code CLI or the Codex CLI in the session sandbox. Coding mode works for them through
the same parts as for the two SDK runtimes:

- The repositories clone before the run, and the note goes into the user message
  (``sama.coding_mode.prepare``). This needs nothing from the harness.
- The diffs, the checkpoints, ``/undo``, the commands, and the pull requests come from the
  checkpoints of the work folder and from the ``tool_call_*`` run events. A harness that
  writes these events and takes checkpoints works with the coding page as it is.
- Plan mode: the gate refuses the tools that change files (``sama.gate.plan``) when the
  harness sends its tool calls to the gate. A harness also has its own plan mode. The adapter
  of the harness reads ``CodingService.plan_mode`` before a run and starts the CLI with
  ``plan_mode_for(harness).cli_args``. When the CLI ends its plan (Claude Code
  ``ExitPlanMode``), the adapter stores the plan with ``CodingService.propose``.

This module is the seam for M7.2. It has no runtime code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class HarnessPlanMode:
    cli_args: tuple[str, ...]
    """Arguments for the CLI while plan mode is on."""
    prompt: str
    """Text for the start of the run input while plan mode is on."""
    native_plan_tool: str | None
    """The tool of the CLI that ends its plan. Its input is the plan for ``propose``."""


CLAUDE_CODE: Final = "claude_code"
CODEX: Final = "codex"

_PLAN_FIRST: Final = (
    "Plan mode is on. Do not change files. Read the code, then write the plan: the goal, the"
    " files to change, the steps, and the tests. Call plan_propose with the plan, or give the"
    " plan as your answer, and stop."
)

HARNESS_PLAN_MODES: Final = {
    CLAUDE_CODE: HarnessPlanMode(
        cli_args=("--permission-mode", "plan"),
        prompt=_PLAN_FIRST,
        native_plan_tool="ExitPlanMode",
    ),
    CODEX: HarnessPlanMode(
        # Codex has no plan mode. A read-only sandbox and a plan-first prompt do the same job.
        cli_args=("--sandbox", "read-only"),
        prompt=_PLAN_FIRST,
        native_plan_tool=None,
    ),
}


def plan_mode_for(harness: str) -> HarnessPlanMode:
    """The plan mode of a harness. A harness that is not known gets the plan-first prompt and
    no CLI arguments; the gate still refuses the tools that change files."""
    return HARNESS_PLAN_MODES.get(
        harness, HarnessPlanMode(cli_args=(), prompt=_PLAN_FIRST, native_plan_tool=None)
    )
