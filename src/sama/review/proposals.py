"""The proposals of a background review (docs/architecture.md, Section 9.4).

A review run can change user memory and user skills at once. It cannot change shared memory
or a shared skill alone: no person is in the run. It proposes the change instead. A proposal
is one ``approvals`` row (the human decision, shown with the other approvals of the session)
and one ``review_proposals`` row (the full change). ``sama.review.apply`` applies the change
after a person with the right role approves it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final, Literal, Protocol

from sqlalchemy import func, insert
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Approval, AuditEntry, ReviewProposal
from sama.gate.decide import arguments_digest
from sama.gate.service import APPROVAL_TTL, stored_arguments
from sama.review.policy import PROPOSAL_SUMMARY_MAX_CHARS
from sama.vault.redact import default_redactor, redact

CALL_PREFIX: Final = "review:"
"""The ``call_id`` of the approval of a proposal starts with this text."""

ProposalKind = Literal["memory", "skill"]
ProposalScope = Literal["agent", "workspace", "org"]


@dataclass(frozen=True, slots=True)
class NewProposal:
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    session_id: uuid.UUID
    run_id: uuid.UUID
    acting_user_id: uuid.UUID | None
    """The user of the session when the review proposed the change, if there is one."""
    kind: ProposalKind
    scope: ProposalScope
    payload: Mapping[str, Any]
    """The full change. For memory: ``target``, ``operations``, ``reason``. For a skill: the
    fields of ``ManageRequest``."""
    summary: str
    """One line for the person who decides."""


class ProposalSink(Protocol):
    """Where a review run sends the changes that it cannot apply itself."""

    async def propose(self, proposal: NewProposal) -> uuid.UUID:
        """Stores the proposal and its approval. Returns the id of the proposal."""
        ...


class PostgresProposalStore:
    """Writes ``approvals`` and ``review_proposals`` as the table owner."""

    def __init__(self, engine: AsyncEngine, *, ttl: timedelta = APPROVAL_TTL) -> None:
        self._engine = engine
        self._ttl = ttl

    async def propose(self, proposal: NewProposal) -> uuid.UUID:
        payload = default_redactor().json(dict(proposal.payload))
        summary = redact(proposal.summary)[:PROPOSAL_SUMMARY_MAX_CHARS]
        proposal_id = uuid.uuid4()
        tool_name = "memory" if proposal.kind == "memory" else "skill_manage"
        async with self._engine.begin() as connection:
            approval_id = (
                await connection.execute(
                    insert(Approval)
                    .values(
                        org_id=proposal.org_id,
                        session_id=proposal.session_id,
                        run_id=proposal.run_id,
                        call_id=f"{CALL_PREFIX}{proposal_id}",
                        tool_name=tool_name,
                        arguments=stored_arguments(payload),
                        arguments_digest=arguments_digest(payload),
                        reason=f"the background review proposes a change of {proposal.scope}"
                        f" {proposal.kind}: {summary}",
                        expires_at=func.now() + self._ttl,
                    )
                    .returning(Approval.id)
                )
            ).scalar_one()
            await connection.execute(
                insert(ReviewProposal).values(
                    id=proposal_id,
                    org_id=proposal.org_id,
                    session_id=proposal.session_id,
                    run_id=proposal.run_id,
                    approval_id=approval_id,
                    acting_user_id=proposal.acting_user_id,
                    kind=proposal.kind,
                    scope=proposal.scope,
                    payload=payload,
                )
            )
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=proposal.org_id,
                    workspace_id=proposal.workspace_id,
                    actor_kind="agent",
                    action="review.proposed",
                    target_type="review_proposal",
                    target_id=str(proposal_id),
                    run_id=proposal.run_id,
                    details={
                        "session_id": str(proposal.session_id),
                        "kind": proposal.kind,
                        "scope": proposal.scope,
                        "summary": summary,
                    },
                )
            )
        return proposal_id
