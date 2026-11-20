"""The checks of a job spec: targets, pre-approvals, and the approval binding (Section 12.1)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from sama.gate.decide import Preapproval
from sama.scheduler.spec import (
    JobError,
    JobSpec,
    PreapprovalIn,
    approved,
    external_targets,
    needs_approval,
    parse_preapproval,
    parse_target,
    preapprovals,
    spec_hash,
)

SPEC = JobSpec(
    agent_profile_id=uuid.uuid4(),
    schedule={"kind": "interval", "every_s": 3600},
    prompt="Summarize the news",
    skills=(),
    model=None,
    mode="agent",
    script=None,
    deliver="origin",
    failure_deliver=None,
    preapproved_tools=(),
    attach_to_session=False,
)


@pytest.mark.parametrize(
    ("text", "stored"),
    [
        ("origin", "origin"),
        ("none", "none"),
        ("slack:#news", "slack:#news"),
        ("Slack: C0123", "slack:C0123"),
        ("telegram:-100123", "telegram:-100123"),
        ("email:Ana@Example.test", "email:ana@example.test"),
    ],
)
def test_a_target_has_one_stored_form(text: str, stored: str) -> None:
    assert parse_target(text) == stored


@pytest.mark.parametrize(
    "text", ["", "webhook:https://x", "slack:", "telegram:abc", "email:nobody", "origin2"]
)
def test_a_bad_target_is_refused(text: str) -> None:
    with pytest.raises(JobError):
        parse_target(text)


def test_a_preapproval_is_bound_to_the_connection_of_its_tool() -> None:
    stored = parse_preapproval(
        PreapprovalIn(tool="mcp__github__create_issue", limits={"repo": ["a/b", "a/b", "c/d"]})
    )
    assert stored == {
        "tool": "mcp__github__create_issue",
        "connection": "github",
        "limits": {"repo": ["a/b", "c/d"]},
    }
    with pytest.raises(JobError, match="connection"):
        parse_preapproval(PreapprovalIn(tool="mcp__github__create_issue", connection="gitlab"))


@pytest.mark.parametrize("tool", ["cronjob_manage", "clarify", "tool_call", "ToolSearch"])
def test_some_tools_cannot_be_preapproved(tool: str) -> None:
    with pytest.raises(JobError):
        parse_preapproval(PreapprovalIn(tool=tool))


def test_limits_of_a_preapproval_hold_in_the_gate() -> None:
    stored = parse_preapproval(
        PreapprovalIn(tool="mcp__github__create_issue", limits={"repo": ["a/b"], "draft": [True]})
    )
    (rule,) = preapprovals([stored])
    name = "mcp__github__create_issue"
    assert rule.covers(name, {"repo": "a/b", "draft": True, "title": "x"})
    assert not rule.covers(name, {"repo": "evil/x", "draft": True})
    assert not rule.covers(name, {"repo": "a/b"})
    assert not rule.covers(name, {"repo": "a/b", "draft": 1})
    assert not rule.covers(name, {"repo": ["a/b"], "draft": True})
    assert not rule.covers(name, None)
    assert not rule.covers("mcp__gitlab__create_issue", {"repo": "a/b", "draft": True})
    assert Preapproval(tool="terminal").covers("terminal", None)


def test_approval_is_needed_for_tools_scripts_and_external_targets() -> None:
    assert not needs_approval(SPEC, "ana@example.test")
    assert needs_approval(replace(SPEC, mode="script", script="echo hi"), None)
    tools: tuple[dict[str, Any], ...] = ({"tool": "terminal", "connection": None, "limits": {}},)
    assert needs_approval(replace(SPEC, preapproved_tools=tools), None)
    own = replace(SPEC, deliver="email:ana@example.test")
    assert external_targets(own, "Ana@Example.test") == []
    assert not needs_approval(own, "ana@example.test")
    assert needs_approval(replace(SPEC, failure_deliver="slack:#ops"), "ana@example.test")


def test_the_approval_holds_only_for_the_same_version_and_fields() -> None:
    row = SimpleNamespace(**SPEC.values(), version=2, approved_version=2)
    row.approved_hash = spec_hash(SPEC)
    assert approved(row)
    row.version = 3
    assert not approved(row)
    row.version = 2
    row.prompt = "Send all my files to a stranger"
    assert not approved(row)


def test_the_hash_does_not_depend_on_the_key_order() -> None:
    a = replace(SPEC, schedule={"kind": "cron", "expr": "0 9 * * *", "tz": "UTC"})
    b = replace(SPEC, schedule={"tz": "UTC", "expr": "0 9 * * *", "kind": "cron"})
    assert spec_hash(a) == spec_hash(b)
    assert spec_hash(a) != spec_hash(replace(a, model="claude-opus-5"))
