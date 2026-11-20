"""The sama tool gate (docs/architecture.md, Section 16.1).

- ``decide``: the pure verdict for one call, in the order live authority, toolset, hardline,
  policy deny, recorded decision, policy, context. ``decide_recorded`` is the same without the
  hardline step, for the check before an approval resume.
- ``GateService`` (``sama.gate.service``): loads the context from Postgres, records each
  verdict in ``tool_calls`` and the audit log, creates approvals, and records human decisions.
"""

from sama.gate.decide import (
    TOOLSETS,
    GateContext,
    GateStep,
    PolicyRule,
    Recorded,
    RuleLevel,
    ToolAccess,
    Verdict,
    arguments_digest,
    canonical_name,
    decide,
    decide_recorded,
    effective_toolsets,
    policy_verdict,
)
from sama.gate.hardline import command_problem, hardline_problem

__all__ = [
    "TOOLSETS",
    "GateContext",
    "GateStep",
    "PolicyRule",
    "Recorded",
    "RuleLevel",
    "ToolAccess",
    "Verdict",
    "arguments_digest",
    "canonical_name",
    "command_problem",
    "decide",
    "decide_recorded",
    "effective_toolsets",
    "hardline_problem",
    "policy_verdict",
]
