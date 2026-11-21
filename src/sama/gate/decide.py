"""The verdict of the tool gate for one tool call (docs/architecture.md, Section 16.1).

``decide`` is a pure function. ``GateService`` (``sama.gate.service``) loads the context from
Postgres, calls ``decide``, and records the verdict.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Literal, Protocol

from sama.gate.hardline import hardline_problem

Action = Literal["allow", "ask", "deny"]

SAMA_MCP_PREFIX: Final = "mcp__sama__"
"""The Claude CLI names a tool of the sama MCP server ``mcp__sama__<tool>``."""

SUBAGENT_TOOLS: Final = frozenset({"Task", "Agent"})
"""The native tool that starts a Claude sub-agent. The CLI calls it ``Task`` in old versions and
``Agent`` in new versions. The toolset ``subagents`` has it (Section 13.1)."""

TOOLSETS: Final[Mapping[str, frozenset[str]]] = {
    "core": frozenset(
        {"Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep", "WebFetch"}
    ),
    "todo": frozenset({"todo", "TodoWrite"}),
    "web": frozenset({"web_search", "web_extract"}),
    "clarify": frozenset({"clarify"}),
    "github": frozenset({"repo_clone", "pr_create", "pr_comment", "repo_push_approval"}),
    "memory": frozenset({"memory"}),
    "skills": frozenset({"skills_list", "skill_view", "skill_manage", "skill_import"}),
    "sessions": frozenset({"session_search", "session_read"}),
    "media": frozenset({"vision_analyze", "image_generate", "text_to_speech"}),
    "tool_search": frozenset({"tool_search", "tool_describe", "tool_call"}),
    "subagents": SUBAGENT_TOOLS,
    "cron": frozenset({"cronjob_manage"}),
    "workflows": frozenset({"workflow_manage", "workflow_run"}),
    "self": frozenset({"change_propose"}),
    "connections": frozenset({"connections_list", "connection_request", "connection_test"}),
    "integrations": frozenset(),
    "delegation": frozenset({"delegate_task", "delegate_status"}),
    "tasks": frozenset({"task_create", "task_list", "task_complete", "task_block", "task_comment"}),
    "code_exec": frozenset({"execute_code"}),
}
"""The tools of each toolset that sama has today (Section 10.2). A toolset that is not here
has no tools yet. ``integrations`` has no fixed tools: the connector tools are deferred tools
that the catalog of the tool server adds (Section 10.3)."""

ToolAccess = Literal["read", "write", "destructive"]
"""What a deferred tool does, for its default policy (Section 10.3): ``read`` gets ``allow``,
``write`` and ``destructive`` get ``ask``. ``destructive`` has a stronger reason on the card."""

TOOL_SEARCH: Final = "tool_search"
TOOL_DESCRIBE: Final = "tool_describe"
TOOL_CALL: Final = "tool_call"
"""``tool_call`` runs a deferred tool of the sama tool server (Section 10.3). The gate does not
judge ``tool_call``. It judges the tool that ``tool_call`` names, with the arguments that it
gives, in the same way as a direct call (``effective_call``)."""
META_TOOLS: Final = frozenset({TOOL_SEARCH, TOOL_DESCRIBE, TOOL_CALL})

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
        "memory",
        "skills_list",
        "skill_view",
        "skill_manage",
        "change_propose",
        "session_search",
        "session_read",
        "vision_analyze",
        "image_generate",
        "text_to_speech",
        "tool_search",
        "tool_describe",
        "Task",
        "Agent",
        "connections_list",
        "connection_request",
        "connection_test",
        "delegate_task",
        "delegate_status",
        "task_create",
        "task_list",
        "task_complete",
        "task_block",
        "task_comment",
        "cronjob_manage",
        "workflow_manage",
        "workflow_run",
    }
)
"""Tools that only read, ask the user, or copy public data into the sandbox. Without a policy
rule, they are allowed. Every other tool gets ``ask``. ``skill_manage`` is here because the
skill store checks the role of the user for the scope of each write (Section 9.2), and every
write is a version that can be reverted. ``change_propose`` is here because it only stores a
proposal: a person with the role decides it on an approval card (M3.2). ``connection_request``
only sends a link, and the agent cannot give credentials with it (Section 10.3).
The ``delegation`` and ``tasks`` tools are here because they write only sama state of the same
workspace and audience, and each child run or task run passes the gate again for every tool
that it calls (Sections 12.4 and 13.2). ``cronjob_manage`` is here because a job that the
agent creates or widens waits for the approval of its owner before it fires (Section 12.1).
``workflow_manage`` and ``workflow_run`` are here for the same reason: a workflow version that
writes waits for the approval of its owner before it runs (Section 12.3)."""


DELEGATION_LINEAGE: Final = "delegation"
CHILD_BLOCKED_TOOLSETS: Final = frozenset(
    {"delegation", "tasks", "memory", "clarify", "cron", "workflows", "connections", "integrations"}
)
"""The toolsets that a delegated child never has (Section 13.2). ``delegation`` keeps the depth
at 1, and ``tasks`` stops a child that makes a task card that delegates again. ``memory``
keeps the shared notes free of child writes. ``clarify`` waits for a person, and a child has
none. ``cron`` and ``workflows`` stop a child that outlives its parent. ``connections`` and
``integrations`` are out for the reason of sub-agents (Section 13.1): a connection acts with the
authority of the user, and only a run that the user watches asks the user to connect
(Section 10.3)."""

NEEDS_A_HUMAN: Final = frozenset({"clarify", "repo_push_approval"})
"""Tools that wait for a human. Unattended runs cannot use them."""

ALWAYS_ASK: Final = frozenset({"repo_push_approval"})
"""Tools that always need the approval of a person for each call. An ``allow`` rule cannot
remove the ask, and "always allow" writes no rule for them. A ``deny`` rule still wins. The
approval of ``repo_push_approval`` is the approval of a push that changes a protected path
(Section 15.1). ``always_asks`` also covers the ``execute`` tool of each database connection."""

_DATABASE_EXECUTE: Final = re.compile(r"^db_[a-z0-9_]+_execute$")
"""``db_<conn>_execute`` (Section 10.4): each call runs other SQL, so each call needs a person
who reads that SQL."""


def always_asks(tool_name: str) -> bool:
    """True for a tool that needs the approval of a person for each call: a tool in
    ``ALWAYS_ASK`` or ``db_<conn>_execute``. ``tool_name`` can have the sama MCP prefix."""
    name = canonical_name(tool_name)
    return name in ALWAYS_ASK or _DATABASE_EXECUTE.match(name) is not None


SHARED_ASK: Final = frozenset({"memory", "cronjob_manage", "workflow_manage", "workflow_run"})
"""Tools that are in ``DEFAULT_ALLOW`` but get ``ask`` in a shared session (Section 11.3). Every
call of ``memory`` writes a note that all later sessions of the profile or workspace read, and
every member of a group can steer the run. A job runs as its owner without a human, so a group
member must not create one in the name of the owner. A workflow is the same (Section 12.3).
A policy rule still wins over this
default."""

WORKFLOW_TOOLSET: Final = "workflows"
"""The toolset of ``workflow_manage`` and ``workflow_run``. The run of an agent step of a
workflow has neither this toolset nor ``cron`` (Section 12.3)."""

JOB_TOOLSET: Final = "cron"
"""The toolset of ``cronjob_manage``. A job run does not have it: a job run cannot create jobs
(Section 12.1)."""

MCP_PREFIX: Final = "mcp__"


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
class Preapproval:
    """A tool that a human pre-approved for one job version (Section 12.1).

    ``connection`` is the MCP server of a connector tool (``mcp__<connection>__<tool>``), or
    ``None`` for a tool of sama or of the runtime. ``limits`` maps an argument name to the
    values that it can have. A call fits when each limited argument has one of its values.
    """

    tool: str
    connection: str | None = None
    limits: tuple[tuple[str, tuple[Any, ...]], ...] = ()

    def covers(self, name: str, arguments: Mapping[str, Any] | None) -> bool:
        """``arguments`` is ``None`` when the gate knows only the digest of the call. Then a
        pre-approval with limits does not cover the call: it fails closed."""
        if connection_of(name) != self.connection or name != self.tool:
            return False
        if not self.limits:
            return True
        if arguments is None:
            return False
        return all(
            key in arguments and _allowed(arguments[key], allowed) for key, allowed in self.limits
        )


def _allowed(value: Any, allowed: tuple[Any, ...]) -> bool:
    """The value is one of the allowed values, with the same type: ``True`` is not ``1``, and a
    list or an object is never a limited value."""
    if not isinstance(value, str | int | float | bool):
        return False
    return any(type(value) is type(item) and value == item for item in allowed)


def connection_of(name: str) -> str | None:
    """The connection of a tool: the MCP server of ``mcp__<server>__<tool>``, other than the
    sama server. A sama tool and a native tool have no connection."""
    if not name.startswith(MCP_PREFIX) or name.startswith(SAMA_MCP_PREFIX):
        return None
    server, _, tool = name[len(MCP_PREFIX) :].partition("__")
    return server if server and tool else None


class ToolsetSource(Protocol):
    """More toolsets than the fixed table ``TOOLSETS``, for example the deferred tools that
    products and connectors register (Section 10.3). The answer can change while the process
    runs, so a new tool is usable at once."""

    def toolsets(self) -> Mapping[str, frozenset[str]]: ...

    def access(self) -> Mapping[str, ToolAccess]:
        """What each extra tool does, for its default policy. A tool that is not here gets the
        default of an unknown tool: ``ask``."""
        ...


@dataclass(frozen=True, slots=True)
class GateContext:
    toolsets: frozenset[str]
    rules: tuple[PolicyRule, ...] = ()
    unattended: bool = False
    preapproved: tuple[Preapproval, ...] = ()
    """The tools that a human pre-approved for this job version (Section 12.1). It is empty
    for a run that is not a job fire, and for a job version without an approval."""
    shared: bool = False
    """The run is a chat run in a shared session. ``SHARED_ASK`` tools get ``ask``. A background
    review is not a chat run: its shared changes are proposals that a person decides."""
    recorded: Recorded | None = None
    revoked: str | None = None
    """Why the run has no authority now (Section 5.1), or ``None``. See
    ``sama.tenancy.authority``."""
    catalog: Mapping[str, frozenset[str]] = field(default_factory=lambda: TOOLSETS)
    """The tools of each toolset. The default is the fixed table."""
    access: Mapping[str, ToolAccess] = field(default_factory=dict)
    """What each deferred tool does, for its default policy (``ToolsetSource.access``)."""


def canonical_name(tool_name: str) -> str:
    """The sama name of a tool: ``mcp__sama__todo`` becomes ``todo``. Other names stay."""
    if tool_name.startswith(SAMA_MCP_PREFIX):
        return tool_name[len(SAMA_MCP_PREFIX) :]
    return tool_name


def tools_of(
    toolsets: Iterable[str], catalog: Mapping[str, frozenset[str]] = TOOLSETS
) -> frozenset[str]:
    tools: set[str] = set()
    for name in toolsets:
        tools |= catalog.get(name, frozenset())
    return frozenset(tools)


def merged_catalog(extra: Mapping[str, frozenset[str]]) -> Mapping[str, frozenset[str]]:
    """The fixed table and the extra toolsets. A toolset of both has the tools of both."""
    merged = dict(TOOLSETS)
    for name, tools in extra.items():
        merged[name] = merged.get(name, frozenset()) | tools
    return merged


def wrapped_call(arguments: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The tool name and the arguments that a ``tool_call`` gives, or ``None`` if the
    arguments are not valid."""
    name = arguments.get("name")
    inner = arguments.get("arguments", {})
    if inner is None:
        inner = {}
    if not isinstance(name, str) or not name or not isinstance(inner, Mapping):
        return None
    return canonical_name(name), {str(key): value for key, value in inner.items()}


def effective_call(tool_name: str, arguments: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    """The call that the gate records and that a human approves. For ``tool_call`` it is the
    deferred tool and its arguments: a card shows the real tool, a policy rule for the tool
    holds, and a ticket matches the tool and not the wrapper. For another tool, and for a
    ``tool_call`` with arguments that are not valid, it is the call itself."""
    name = canonical_name(tool_name)
    if name == TOOL_CALL and (wrapped := wrapped_call(arguments)) is not None:
        return wrapped
    return name, arguments


def effective_toolsets(
    toolsets: Iterable[str],
    group_toolsets: Iterable[str],
    *,
    audience: str,
    kind: str = "chat",
    lineage: str | None = None,
    run_toolsets: Iterable[str] | None = None,
) -> frozenset[str]:
    """The toolsets of a run (Section 10.2). A chat run in a shared session has the group
    toolsets of the profile, and not its toolsets. A private session has the toolsets. A
    background review has the toolsets of the profile that the review clamp keeps (Section
    9.4): it has no person in it, and its changes of shared state are proposals that a person
    decides, so the group toolsets do not apply to it. A delegated child (lineage
    ``delegation``) loses ``CHILD_BLOCKED_TOOLSETS`` (Section 13.2).

    ``run_toolsets`` is the limit of the run itself (``runs.toolsets``), for example the
    toolsets of an event trigger (Section 12.2). It can only remove toolsets."""
    if audience == "shared" and kind != "review":
        found = frozenset(group_toolsets)
    else:
        found = frozenset(toolsets)
    if run_toolsets is not None:
        found &= frozenset(run_toolsets)
    if lineage == DELEGATION_LINEAGE:
        return found - CHILD_BLOCKED_TOOLSETS
    return found


def run_limit(value: Any) -> list[str] | None:
    """The toolset limit of a run from the JSON column ``runs.toolsets`` (Section 12.2). Null
    means no limit. A value that is not a list allows no toolset."""
    if value is None:
        return None
    return [str(item) for item in value] if isinstance(value, list) else []


def narrow_toolsets(*limits: Any) -> list[str] | None:
    """The toolset limit that obeys all the limits (``runs.toolsets`` values). Null means no
    limit. A run that another run starts (a child, a steer, a task card) keeps the limit of
    the run that started it, so a trigger run cannot get more toolsets that way."""
    out: list[str] | None = None
    for value in limits:
        limit = run_limit(value)
        if limit is None:
            continue
        out = limit if out is None else [name for name in out if name in limit]
    return out


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
    if name == TOOL_CALL:
        return _decide_wrapped(context, arguments)
    early = _authority_and_toolset(context, name)
    if early is not None:
        return early
    problem = hardline_problem(name, arguments)
    if problem is not None:
        return Verdict("deny", GateStep.HARDLINE, f"the command is on the hardline list: {problem}")
    return _after_hardline(context, name, arguments_digest(arguments), arguments)


def _decide_wrapped(context: GateContext, arguments: Mapping[str, Any]) -> Verdict:
    """``tool_call`` has no verdict of its own. The run needs the toolset of ``tool_call``,
    and no ``deny`` rule can name ``tool_call``. Then the named tool gets the full gate with
    its own arguments: its toolset, the hardline list, its policy, and the unattended rule.
    So a deferred call can never get more than a direct call of the same tool."""
    early = _authority_and_toolset(context, TOOL_CALL)
    if early is not None:
        return early
    own = policy_verdict(context.rules, TOOL_CALL, shared=context.shared, access=context.access)
    if own.action == "deny":
        return own
    wrapped = wrapped_call(arguments)
    if wrapped is None:
        return Verdict(
            "deny", GateStep.TOOLSET, "tool_call needs the name of a tool and an arguments object"
        )
    name, inner = wrapped
    if name in META_TOOLS or name in TOOL_LOADERS:
        return Verdict("deny", GateStep.TOOLSET, f"{name} cannot be called through tool_call")
    return decide(context, name, inner)


def decide_recorded(context: GateContext, tool_name: str, digest: str) -> Verdict:
    """The verdict again for a call that a human decided, before the run resumes. It has
    all the steps of ``decide`` except the hardline step. That step ran at the first check of
    the call, and its result depends only on the tool name and the arguments. The arguments
    are not known here, so a pre-approval with argument limits does not apply."""
    name = canonical_name(tool_name)
    return _authority_and_toolset(context, name) or _after_hardline(context, name, digest, None)


def _authority_and_toolset(context: GateContext, name: str) -> Verdict | None:
    if context.revoked is not None:
        return Verdict("deny", GateStep.AUTHORITY, context.revoked)
    if name not in TOOL_LOADERS and name not in tools_of(context.toolsets, context.catalog):
        return Verdict("deny", GateStep.TOOLSET, f"{name} is not in the toolsets of this run")
    return None


def _after_hardline(
    context: GateContext, name: str, digest: str, arguments: Mapping[str, Any] | None
) -> Verdict:
    # A deny rule wins over a recorded approval: a newer deny applies at once (Section 5.1).
    policy = policy_verdict(context.rules, name, shared=context.shared, access=context.access)
    if policy.action == "deny":
        return policy
    verdict = _recorded(context.recorded, name, digest) or policy
    return _in_context(context, name, verdict, arguments)


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


def policy_verdict(
    rules: Iterable[PolicyRule],
    name: str,
    *,
    shared: bool = False,
    access: Mapping[str, ToolAccess] | None = None,
) -> Verdict:
    """Step 4. A ``deny`` rule at any level wins. A tool that ``always_asks`` gets ``ask``. An
    org or profile ``ask`` rule cannot be removed by a user rule. Otherwise a user rule wins,
    then the profile rule, then the org rule. At one level an exact name wins over a prefix.
    Without a rule, the built-in default applies, and the default for unknown tools is
    ``ask``. In a shared session, the tools in ``SHARED_ASK`` also get ``ask`` by default. A
    deferred tool with an ``access`` gets the connector default: ``read`` is ``allow``,
    ``write`` and ``destructive`` are ``ask`` (Section 10.3)."""
    matching = [rule for rule in rules if rule.matches(name)]
    if any(rule.action == "deny" for rule in matching):
        return Verdict("deny", GateStep.POLICY, f"a policy rule denies {name}")
    if always_asks(name):
        return Verdict("ask", GateStep.POLICY, f"{name} needs an approval for each call")
    admin = _most_specific(r for r in matching if r.level is not RuleLevel.USER)
    user = _most_specific(r for r in matching if r.level is RuleLevel.USER)
    if admin is not None and admin.action == "ask":
        return Verdict("ask", GateStep.POLICY, f"the {admin.level} policy asks for {name}")
    chosen = user or admin
    if chosen is not None:
        return Verdict(
            chosen.action, GateStep.POLICY, f"the {chosen.level} policy: {chosen.action}"
        )
    if shared and name in SHARED_ASK:
        return Verdict("ask", GateStep.POLICY, f"{name} needs an approval in a shared session")
    if name in DEFAULT_ALLOW:
        return Verdict("allow", GateStep.POLICY, f"{name} is allowed by default")
    match (access or {}).get(name):
        case "read":
            return Verdict("allow", GateStep.POLICY, f"{name} only reads, so it is allowed")
        case "destructive":
            return Verdict(
                "ask",
                GateStep.POLICY,
                f"{name} can delete or change data that cannot be restored. It needs an approval",
            )
        case _:
            return Verdict("ask", GateStep.POLICY, f"{name} needs an approval")


def _most_specific(rules: Iterable[PolicyRule]) -> PolicyRule | None:
    rank = {RuleLevel.ORG: 0, RuleLevel.PROFILE: 1, RuleLevel.USER: 2}
    return max(rules, key=lambda rule: (rank[rule.level], rule.exact), default=None)


def _in_context(
    context: GateContext, name: str, verdict: Verdict, arguments: Mapping[str, Any] | None
) -> Verdict:
    """Step 5. An unattended run has no human to ask. An ``ask`` call is allowed only when a
    pre-approval of the job version covers the tool, its connection, and its arguments."""
    if not context.unattended or verdict.action == "deny":
        return verdict
    if name in NEEDS_A_HUMAN:
        return Verdict(
            "deny", GateStep.CONTEXT, f"{name} needs a human, and this run is unattended"
        )
    if verdict.action != "ask":
        return verdict
    if any(item.covers(name, arguments) for item in context.preapproved):
        return Verdict("allow", GateStep.CONTEXT, "pre-approved for this job version")
    return Verdict(
        "deny", GateStep.CONTEXT, f"{name} needs an approval, and this run is unattended"
    )
