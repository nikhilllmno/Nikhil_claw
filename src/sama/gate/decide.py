"""The verdict of the tool gate for one tool call (docs/architecture.md, Section 16.1).

``decide`` is a pure function. ``GateService`` (``sama.gate.service``) loads the context from
Postgres, calls ``decide``, and records the verdict.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Literal

from sama.gate.hardline import hardline_problem

Action = Literal["allow", "ask", "deny"]

SAMA_MCP_PREFIX: Final = "mcp__sama__"
"""The Claude CLI names a tool of the sama MCP server ``mcp__sama__<tool>``."""

TOOLSETS: Final[Mapping[str, frozenset[str]]] = {
    "core": frozenset(
        {"Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep", "WebFetch"}
    ),
    "todo": frozenset({"todo", "TodoWrite"}),
    "web": frozenset({"web_search", "web_extract"}),
    "clarify": frozenset({"clarify"}),
    "github": frozenset({"repo_clone"}),
}
"""The tools of each toolset that sama has today (Section 10.2). A toolset that is not here
has no tools yet."""

TOOL_LOADERS: Final = frozenset({"ToolSearch"})
"""Native Claude tools that only load the schema of another tool of the run. Every run has
them, in no toolset. A tool that a loader loads passes the gate again when the model calls it.
The Claude CLI can defer MCP tools even with ``ENABLE_TOOL_SEARCH=false``; then the model must
call ``ToolSearch`` before it can call a sama tool."""

DEFAULT_ALLOW: Final = frozenset(
    {
        "ToolSearch",
        "Read",
        "Glob",
        "Grep",
        "todo",
        "TodoWrite",
        "web_search",
        "web_extract",
        "clarify",
        "repo_clone",
    }
)
"""Tools that only read, ask the user, or copy public data into the sandbox. Without a policy
rule, they are allowed. Every other tool gets ``ask``."""

NEEDS_A_HUMAN: Final = frozenset({"clarify"})
"""Tools that wait for a human. Unattended runs cannot use them."""


class GateStep(StrEnum):
    AUTHORITY = "authority"
    TOOLSET = "toolset"
    HARDLINE = "hardline"
    RECORDED = "recorded"
    POLICY = "policy"
    CONTEXT = "context"


class RuleLevel(StrEnum):
    ORG = "org"
    PROFILE = "profile"
    USER = "user"


@dataclass(frozen=True, slots=True)
class Verdict:
    action: Action
    step: GateStep
    reason: str


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """One rule. ``tool`` is an exact name, or a prefix that ends with ``*``."""

    tool: str
    action: Action
    level: RuleLevel

    def matches(self, tool_name: str) -> bool:
        if self.tool.endswith("*"):
            return tool_name.startswith(self.tool[:-1])
        return tool_name == self.tool

    @property
    def exact(self) -> bool:
        return not self.tool.endswith("*")


@dataclass(frozen=True, slots=True)
class Recorded:
    """The approval of this tool call id, if a human was asked."""

    status: Literal["pending", "approved", "rejected", "expired"]
    tool_name: str
    arguments_digest: str


@dataclass(frozen=True, slots=True)
class GateContext:
    toolsets: frozenset[str]
    rules: tuple[PolicyRule, ...] = ()
    unattended: bool = False
    preapproved: frozenset[str] = field(default_factory=frozenset)
    """Tools that a human pre-approved for this job version (Section 12.1). Jobs come in M3,
    so today the set is always empty."""
    recorded: Recorded | None = None
    revoked: str | None = None
    """Why the run has no authority now (Section 5.1), or ``None``. See
    ``sama.tenancy.authority``."""


def canonical_name(tool_name: str) -> str:
    """The sama name of a tool: ``mcp__sama__todo`` becomes ``todo``. Other names stay."""
    if tool_name.startswith(SAMA_MCP_PREFIX):
        return tool_name[len(SAMA_MCP_PREFIX) :]
    return tool_name


def tools_of(toolsets: Iterable[str]) -> frozenset[str]:
    tools: set[str] = set()
    for name in toolsets:
        tools |= TOOLSETS.get(name, frozenset())
    return frozenset(tools)


def json_default(value: Any) -> Any:
    """The ``default`` of ``json.dumps`` for arguments. A read-only mapping (the arguments of a
    runtime event) becomes a dict, so it gives the same JSON as a plain dict."""
    return dict(value) if isinstance(value, Mapping) else str(value)


def arguments_digest(arguments: Mapping[str, Any]) -> str:
    """A stable SHA-256 hex digest of the arguments."""
    canonical = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=json_default
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def decide(context: GateContext, tool_name: str, arguments: Mapping[str, Any]) -> Verdict:
    """The verdict for one call. The steps run in the order of Section 16.1."""
    name = canonical_name(tool_name)
    early = _authority_and_toolset(context, name)
    if early is not None:
        return early
    problem = hardline_problem(name, arguments)
    if problem is not None:
        return Verdict("deny", GateStep.HARDLINE, f"the command is on the hardline list: {problem}")
    return _after_hardline(context, name, arguments_digest(arguments))


def decide_recorded(context: GateContext, tool_name: str, digest: str) -> Verdict:
    """The verdict again for a call that a human decided, before the run resumes. It has
    all the steps of ``decide`` except the hardline step. That step ran at the first check of
    the call, and its result depends only on the tool name and the arguments."""
    name = canonical_name(tool_name)
    return _authority_and_toolset(context, name) or _after_hardline(context, name, digest)


def _authority_and_toolset(context: GateContext, name: str) -> Verdict | None:
    if context.revoked is not None:
        return Verdict("deny", GateStep.AUTHORITY, context.revoked)
    if name not in TOOL_LOADERS and name not in tools_of(context.toolsets):
        return Verdict("deny", GateStep.TOOLSET, f"{name} is not in the toolsets of this run")
    return None


def _after_hardline(context: GateContext, name: str, digest: str) -> Verdict:
    # A deny rule wins over a recorded approval: a newer deny applies at once (Section 5.1).
    policy = policy_verdict(context.rules, name)
    if policy.action == "deny":
        return policy
    verdict = _recorded(context.recorded, name, digest) or policy
    return _in_context(context, name, verdict)


def _recorded(recorded: Recorded | None, name: str, digest: str) -> Verdict | None:
    if recorded is None:
        return None
    if canonical_name(recorded.tool_name) != name or recorded.arguments_digest != digest:
        return Verdict("deny", GateStep.RECORDED, "the approval is for another call")
    match recorded.status:
        case "approved":
            return Verdict("allow", GateStep.RECORDED, "approved by a user")
        case "rejected":
            return Verdict("deny", GateStep.RECORDED, "rejected by a user")
        case "expired":
            return Verdict("deny", GateStep.RECORDED, "the approval timed out")
        case "pending":
            return Verdict("ask", GateStep.RECORDED, "the approval is still open")


def policy_verdict(rules: Iterable[PolicyRule], name: str) -> Verdict:
    """Step 4. A ``deny`` rule at any level wins. An org or profile ``ask`` rule cannot be
    removed by a user rule. Otherwise a user rule wins, then the profile rule, then the org
    rule. At one level an exact name wins over a prefix. Without a rule, the built-in
    default applies, and the default for unknown tools is ``ask``."""
    matching = [rule for rule in rules if rule.matches(name)]
    if any(rule.action == "deny" for rule in matching):
        return Verdict("deny", GateStep.POLICY, f"a policy rule denies {name}")
    admin = _most_specific(r for r in matching if r.level is not RuleLevel.USER)
    user = _most_specific(r for r in matching if r.level is RuleLevel.USER)
    if admin is not None and admin.action == "ask":
        return Verdict("ask", GateStep.POLICY, f"the {admin.level} policy asks for {name}")
    chosen = user or admin
    if chosen is not None:
        return Verdict(
            chosen.action, GateStep.POLICY, f"the {chosen.level} policy: {chosen.action}"
        )
    if name in DEFAULT_ALLOW:
        return Verdict("allow", GateStep.POLICY, f"{name} is allowed by default")
    return Verdict("ask", GateStep.POLICY, f"{name} needs an approval")


def _most_specific(rules: Iterable[PolicyRule]) -> PolicyRule | None:
    rank = {RuleLevel.ORG: 0, RuleLevel.PROFILE: 1, RuleLevel.USER: 2}
    return max(rules, key=lambda rule: (rank[rule.level], rule.exact), default=None)


def _in_context(context: GateContext, name: str, verdict: Verdict) -> Verdict:
    """Step 5. An unattended run has no human to ask."""
    if not context.unattended or verdict.action == "deny":
        return verdict
    if name in NEEDS_A_HUMAN:
        return Verdict(
            "deny", GateStep.CONTEXT, f"{name} needs a human, and this run is unattended"
        )
    if verdict.action == "ask" and name not in context.preapproved:
        return Verdict(
            "deny", GateStep.CONTEXT, f"{name} needs an approval, and this run is unattended"
        )
    return verdict
