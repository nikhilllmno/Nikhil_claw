"""Coding mode (M7.9, docs/architecture.md, Section 15.5) for the runtimes ``claude_code`` and
``codex`` of M7.2: the hooks before the CLI, plan mode, and the plan at the end."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sama.coding.agents import CODING_AGENT_PLAN, CODING_AGENT_RUN, CodingAgent, cli_command
from sama.coding.events import ClaudeStream, CliPlan
from sama.coding.runtime import CodingAgentRuntime, CodingModePrep
from sama.coding.service import CodingService
from sama.gate.decide import TOOLSETS, GateContext, GateStep, decide
from sama.orchestrator.sandboxed import SandboxedRuntime
from sama.runtime.contract import RunCompleted, RunScope, RuntimeKind, TextDelta
from sama.sandbox.base import Sandbox
from sama.tenancy import RunTokenSigner
from tests.coding.conftest import calls, make_repo, session_sandbox
from tests.coding.test_runtime import FakeGate, _request, _state
from tests.support import collect


@dataclass
class FakeHooks:
    plan_mode: bool = False
    note: str = "[sama coding mode] note"
    befores: list[tuple[str, bool]] = field(default_factory=list)
    plans: list[str] = field(default_factory=list)

    async def planning(self, scope: RunScope) -> bool:
        return self.plan_mode

    async def before(
        self, sandbox: Sandbox, scope: RunScope, token: str, agent: str, plan: bool
    ) -> CodingModePrep:
        self.befores.append((agent, plan))
        args = ("--permission-mode", "plan") if plan and agent == "claude_code" else ()
        return CodingModePrep(note=self.note, plan_mode=plan, plan_args=args)

    async def plan(self, scope: RunScope, text: str) -> str:
        self.plans.append(text)
        return "Plan version 1 is saved."


def _runtime(
    sandboxes: SandboxedRuntime,
    signer: RunTokenSigner,
    hooks: FakeHooks,
    gate: FakeGate,
    kind: RuntimeKind = RuntimeKind.CLAUDE_CODE,
) -> CodingAgentRuntime:
    runtime = CodingAgentRuntime(
        kind=kind,
        sandboxes=sandboxes,
        signer=signer,
        service=CodingService(model_url="http://proxy.test"),
        gate=gate,
    )
    runtime.set_hooks(hooks)
    return runtime


async def test_the_note_goes_after_the_task_without_plan_mode(
    sandboxes: SandboxedRuntime, signer: RunTokenSigner
) -> None:
    sandbox = await session_sandbox(sandboxes)
    repo = Path(sandbox.workdir) / "proj"
    make_repo(repo)
    hooks, gate = FakeHooks(), FakeGate()
    runtime = _runtime(sandboxes, signer, hooks, gate)
    events = await collect(runtime.run(_request("make a change --pr", state=_state(str(repo)))))
    assert isinstance(events[-1], RunCompleted)
    (call,) = calls(sandbox)
    assert call["prompt"] == "make a change\n\n[sama coding mode] note"
    assert "--dangerously-skip-permissions" in call["argv"]  # type: ignore[operator]
    assert gate.seen[0][1] == CODING_AGENT_RUN
    assert gate.seen[0][2]["open_pr"] is True
    assert hooks.befores == [("claude_code", False)]
    assert hooks.plans == []


async def test_claude_code_in_plan_mode_saves_its_plan(
    sandboxes: SandboxedRuntime, signer: RunTokenSigner
) -> None:
    sandbox = await session_sandbox(sandboxes)
    repo = Path(sandbox.workdir) / "proj"
    make_repo(repo)
    hooks, gate = FakeHooks(plan_mode=True), FakeGate()
    runtime = _runtime(sandboxes, signer, hooks, gate)
    events = await collect(runtime.run(_request("add a route --pr", state=_state(str(repo)))))
    final = events[-1]
    assert isinstance(final, RunCompleted)
    assert "Plan version 1 is saved." in final.output
    assert any(isinstance(e, TextDelta) and "Plan version 1" in e.text for e in events)
    (call,) = calls(sandbox)
    argv = call["argv"]
    assert isinstance(argv, list)
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert gate.seen[0][1] == CODING_AGENT_PLAN
    assert gate.seen[0][2]["open_pr"] is False
    assert hooks.plans == ["1. Edit change.txt\n2. Run the tests"]
    assert not (repo / "change.txt").exists()


async def test_codex_in_plan_mode_saves_its_answer(
    sandboxes: SandboxedRuntime, signer: RunTokenSigner
) -> None:
    sandbox = await session_sandbox(sandboxes)
    repo = Path(sandbox.workdir) / "proj"
    make_repo(repo)
    hooks, gate = FakeHooks(plan_mode=True), FakeGate()
    runtime = _runtime(sandboxes, signer, hooks, gate, kind=RuntimeKind.CODEX)
    events = await collect(
        runtime.run(
            _request(
                "add a route",
                kind=RuntimeKind.CODEX,
                model="gpt-5.4",
                state=_state(str(repo)),
            )
        )
    )
    assert isinstance(events[-1], RunCompleted)
    assert hooks.befores == [("codex", True)]
    (plan,) = hooks.plans
    assert plan.strip()
    assert gate.seen[0][1] == CODING_AGENT_PLAN


def test_the_plan_flags_replace_skip_permissions() -> None:
    plain = cli_command(
        CodingAgent.CLAUDE_CODE, prompt="x", model="m", model_url="http://p", token="t"
    )
    planned = cli_command(
        CodingAgent.CLAUDE_CODE,
        prompt="x",
        model="m",
        model_url="http://p",
        token="t",
        plan_args=("--permission-mode", "plan"),
    )
    assert "--dangerously-skip-permissions" in plain.argv
    assert "--dangerously-skip-permissions" not in planned.argv
    assert "--permission-mode" in planned.argv


def test_the_parser_gives_the_plan_of_exit_plan_mode() -> None:
    stream = ClaudeStream("claude-sonnet-4-6")
    line = (
        '{"type": "assistant", "parent_tool_use_id": null, "message": {"content": ['
        '{"type": "tool_use", "id": "t1", "name": "ExitPlanMode", "input": {"plan": " 1. a "}}'
        "]}}"
    )
    events = stream.feed(line)
    assert CliPlan("1. a") in events


def test_plan_mode_blocks_coding_agent_run_but_not_the_plan_run() -> None:
    context = GateContext(toolsets=frozenset(TOOLSETS), plan_mode=True)
    blocked = decide(context, "mcp__sama__coding_agent_run", {"agent": "codex"})
    assert (blocked.action, blocked.step) == ("deny", GateStep.PLAN)
    planned = decide(context, CODING_AGENT_PLAN, {"agent": "codex"})
    assert planned.action == "ask"
