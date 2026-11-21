"""What a person sees of a proposal (docs/architecture.md, Sections 9.4.1 and 9.4.2).

A proposal comes from a background review (origin ``review``) or from a chat run (origin
``chat``, M3.2). It changes memory, a skill, the soul of a profile, or the settings, the
toolsets, or the policy rules of a profile (M6.1). A change of a profile also has a short text
diff for the approval card.

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
from sama.profile_changes import change_diff
from sama.review.change import CHANGE_LABELS
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
    diff: str | None = None
    """A short text diff of a change of a profile (``sama.profile_changes.change_diff``), or
    ``None`` for memory, a skill, and a soul proposal from before M6.1."""


def shown_proposal(
    kind: str, scope: str, payload: Mapping[str, Any], origin: str = "review"
) -> ShownProposal:
    clean: dict[str, Any] = default_redactor().json(dict(payload))
    diff: str | None = None
    if kind == "soul":
        line = f"set the soul of the agent profile {clean.get('profile_name') or ''}".rstrip()
        if clean.get("summary"):
            line += f": {clean['summary']}"
        if isinstance(clean.get("before_soul"), str):
            diff = change_diff(
                "soul", {"soul": clean["before_soul"]}, {"soul": str(clean.get("soul") or "")}
            )
    elif kind in CHANGE_LABELS:
        name = clean.get("profile_name") or ""
        line = f"change the {CHANGE_LABELS[kind]} of the agent profile {name}".rstrip()
        if clean.get("summary"):
            line += f": {clean['summary']}"
        before, after = clean.get("before"), clean.get("after")
        if isinstance(before, dict) and isinstance(after, dict):
            diff = change_diff(kind, before, after)
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
        diff=diff,
    )
