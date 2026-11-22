"""Plan mode in the tool gate (docs/architecture.md, Sections 15.5 and 16.1)."""

from __future__ import annotations

import pytest

from sama.admin.bootstrap import DEFAULT_TOOLSETS
from sama.gate.decide import (
    DEFAULT_ALLOW,
    TOOLSETS,
    GateContext,
    GateStep,
    PolicyRule,
    Recorded,
    RuleLevel,
    arguments_digest,
    decide,
    decide_recorded,
)
from sama.gate.plan import PLAN_MODE_REASON, plan_mode_allows, read_only_command

ALL = frozenset(TOOLSETS)


def plan(**more: object) -> GateContext:
    return GateContext(toolsets=ALL, plan_mode=True, **more)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "cat README.md",
        "grep -rn 'def main' src | head -20",
        "git status && git log --oneline -5",
        "git diff HEAD~1 -- src/app.py",
        "find . -name '*.py' -type f",
        "rg TODO; wc -l src/*.py",
        "git branch -a",
        "git log -5 --stat -- src",
        "cd /home/user/work/Hello-World && git log --oneline -5",
        "cd repo; cat README.md | head -20",
        "cd",
        "git -C /home/user/work/app status",
        "git -C repo log -3 --stat",
        "git log -3 --format=%h --date=short",
        "cd /x && git log --since=2.weeks --no-merges --reverse --first-parent",
        "git log --follow --date-order -- src/app.py",
        "git show HEAD~1:src/app.py",
        "tree -L 2",
        'echo "a > b"',
    ],
)
def test_read_commands_pass(command: str) -> None:
    assert read_only_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "",
        "rm -rf build",
        "echo hi > out.txt",
        "cat a >> b",
        "ls & rm x",
        "ls\nrm x",
        "ls $(rm x)",
        "ls `rm x`",
        "echo $HOME",
        "find . -delete",
        "find . -name x -exec rm {} ;",
        "sort -o out.txt in.txt",
        "rg --pre ./run.sh x",
        "git commit -m x",
        "git push origin main",
        "git -c core.pager=sh log",
        "git branch -D main",
        "git remote add x y",
        "git log --output=log.txt",
        "python -c 'print(1)'",
        "pytest -q",
        "npm test",
        "(ls)",
        "ls |",
        "cat 'open",
        "FOO=1 ls",
        "printenv",
        "cd repo && rm -rf build",
        "cd repo && echo x > f",
        "cd repo && git commit -m x",
        "cd -",
        "cd a b",
        "cd $(rm x)",
        "git -C repo -c core.pager=sh log",
        "git -C repo push",
        "git -C -x log",
        "git -C",
        "sort names.txt",
        "sort -ro out.txt in.txt",
        "uniq in.txt out.txt",
        "file -C -m x",
        "git grep --open-files-in-pager='sh -c x' needle",
        "git grep -O needle",
        "git log --ext-diff",
        "git diff --textconv",
        "git log --output=x",
        "git show --exec=x",
        "tree -ao out.txt",
        "tree --output=x",
        "rg --pre-glob '*' --pre ./x y",
    ],
)
def test_other_commands_fail(command: str) -> None:
    assert not read_only_command(command)


def test_plan_mode_keeps_the_verdict_of_read_tools() -> None:
    for name in ("Read", "Grep", "plan_propose", "todo", "repo_clone", "clarify"):
        assert plan_mode_allows(name, {}, None)
    assert plan_mode_allows("crm_lookup", {}, "read")
    assert not plan_mode_allows("crm_update", {}, "write")
    assert not plan_mode_allows("Write", {"file_path": "a"}, None)
    assert not plan_mode_allows("Bash", None, None)


def test_plan_mode_denies_tools_that_change_files() -> None:
    for name, arguments in (
        ("Write", {"file_path": "a.py", "content": "x"}),
        ("Edit", {"file_path": "a.py"}),
        ("Bash", {"command": "pytest -q"}),
        ("mcp__sama__execute_code", {"code": "print(1)"}),
        ("mcp__sama__pr_create", {"repo": "o/n"}),
        ("mcp__sama__memory", {"target": "user"}),
    ):
        verdict = decide(plan(), name, arguments)
        assert (verdict.action, verdict.step, verdict.reason) == (
            "deny",
            GateStep.PLAN,
            PLAN_MODE_REASON,
        ), name


def test_plan_mode_keeps_the_policy_of_allowed_calls() -> None:
    assert decide(plan(), "Read", {"file_path": "a"}).action == "allow"
    assert decide(plan(), "mcp__sama__plan_propose", {"plan": "x"}).action == "allow"
    # A read command still gets the default of Bash: ask.
    assert decide(plan(), "Bash", {"command": "ls"}).action == "ask"
    rule = PolicyRule(tool="Bash", action="allow", level=RuleLevel.PROFILE)
    assert decide(plan(rules=(rule,)), "Bash", {"command": "git status"}).action == "allow"
    # A deny rule still wins.
    deny = PolicyRule(tool="Read", action="deny", level=RuleLevel.ORG)
    assert decide(plan(rules=(deny,)), "Read", {"file_path": "a"}).action == "deny"


def test_plan_mode_applies_to_a_deferred_call() -> None:
    context = plan(
        catalog={**TOOLSETS, "integrations": frozenset({"crm_update", "crm_lookup"})},
        access={"crm_update": "write", "crm_lookup": "read"},
    )
    wrapped = {"name": "crm_update", "arguments": {}}
    assert decide(context, "mcp__sama__tool_call", wrapped).step is GateStep.PLAN
    lookup = {"name": "crm_lookup", "arguments": {}}
    assert decide(context, "mcp__sama__tool_call", lookup).action == "allow"


def test_without_plan_mode_nothing_changes() -> None:
    context = GateContext(toolsets=ALL)
    assert decide(context, "Write", {"file_path": "a"}).action == "ask"


def test_a_recorded_approval_is_not_stopped_by_plan_mode() -> None:
    digest = arguments_digest({"command": "pytest"})
    context = plan(recorded=Recorded("approved", "Bash", digest))
    assert decide_recorded(context, "Bash", digest).action == "allow"


def test_the_plan_toolset_is_known_and_on_by_default() -> None:
    assert TOOLSETS["plan"] == frozenset({"plan_propose"})
    assert "plan_propose" in DEFAULT_ALLOW
    assert "plan" in DEFAULT_TOOLSETS
    denied = decide(GateContext(toolsets=frozenset({"core"})), "mcp__sama__plan_propose", {})
    assert denied.step is GateStep.TOOLSET
