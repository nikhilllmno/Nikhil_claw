"""The sama tool gate (docs/architecture.md, Section 16.1).

- ``decide``: the pure verdict for one call, in the order toolset, hardline, recorded
  decision, policy, context.
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
    Verdict,
    arguments_digest,
    canonical_name,
    decide,
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
    "Verdict",
    "arguments_digest",
    "canonical_name",
    "command_problem",
    "decide",
    "hardline_problem",
    "policy_verdict",
]
