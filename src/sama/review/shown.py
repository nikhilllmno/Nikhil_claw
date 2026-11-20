"""What a person sees of a proposal (docs/architecture.md, Sections 9.4.1 and 9.4.2).

A proposal comes from a background review (origin ``review``) or from a chat run (origin
``chat``, M3.2). It changes memory, a skill, or the soul of a profile.

The proposal is stored with redacted text. The redactor of the process can learn a secret later
(a credential that the vault stores after the proposal), so the stored text is not enough. Every
place that shows a proposal builds the view again from the stored payload, with the redactor of
the process at that time: first the full payload is redacted, then the summary is made from the
redacted payload and cut. The stored ``approvals.reason`` and the stored arguments are not
shown, because they were cut at the time of the write, and a cut can hide the start of a secret
from a pattern that is learned later.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sama.gate.service import stored_arguments
from sama.memory.core import Operation, Target
from sama.memory.service import describe_operations
from sama.review.policy import PROPOSAL_SUMMARY_MAX_CHARS
from sama.review.proposals import proposal_reason
from sama.vault.redact import default_redactor, redact


@dataclass(frozen=True, slots=True)
class ShownProposal:
    payload: dict[str, Any]
    """The full change, redacted now."""
    summary: str
    """One line, made from the redacted payload."""
    reason: str
    """The text for an approval: a fixed start and the summary."""
    arguments: dict[str, Any]
    """The arguments of the approval, made from the redacted payload."""


def shown_proposal(
    kind: str, scope: str, payload: Mapping[str, Any], origin: str = "review"
) -> ShownProposal:
    clean: dict[str, Any] = default_redactor().json(dict(payload))
    if kind == "soul":
        line = f"set the soul of the agent profile {clean.get('profile_name') or ''}".rstrip()
        if clean.get("summary"):
            line += f": {clean['summary']}"
    elif kind == "memory":
        line = describe_operations(
            Target(clean["target"]), [Operation.model_validate(o) for o in clean["operations"]]
        )
    else:
        line = f"{clean.get('action')} the {scope} skill {clean.get('name')}"
        if clean.get("summary"):
            line += f": {clean['summary']}"
    summary = redact(line)[:PROPOSAL_SUMMARY_MAX_CHARS]
    return ShownProposal(
        payload=clean,
        summary=summary,
        reason=proposal_reason(origin, kind, scope, summary),
        arguments=stored_arguments(clean),
    )
