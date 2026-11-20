"""Proposals of a new soul for an agent profile (docs/architecture.md, Sections 9.4 and 10.2).

The ``change_propose`` tool (toolset ``self``) lets the agent propose a new soul for the profile
of the run. A soul changes how the agent behaves for every user of the profile, so a change is
always a proposal, also when the user of the run is an admin:

- A profile of one workspace: a workspace admin or the org owner approves it.
- A profile of the whole org (no workspace): only the org owner approves it.

The applier (``sama.review.apply``) writes the approved soul with ``set_soul``. The proposal
keeps a digest of the soul that the agent saw. If the soul changed since then, the proposal
fails and the agent must propose again. A session keeps the soul that it froze, so the new soul
applies from the next session.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import AgentProfile, AuditEntry, Run, Session
from sama.profiles import SOUL_MAX_CHARS
from sama.review.proposals import NewProposal, ProposalScope, ProposalSink
from sama.tenancy.run_token import RunTokenClaims
from sama.vault.redact import redact

SUMMARY_MAX_CHARS: Final = 200


class SoulError(ValueError):
    """The proposal or its apply step is not valid. The text is safe to show."""


def soul_digest(soul: str) -> str:
    return hashlib.sha256(soul.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _RunProfile:
    workspace_id: uuid.UUID
    profile_id: uuid.UUID
    profile_workspace_id: uuid.UUID | None
    profile_name: str
    soul: str
    kind: str
    unattended: bool
    lineage: str | None
    acting_user_id: uuid.UUID | None


class SoulProposer:
    """The body of ``change_propose`` for the target ``soul``."""

    def __init__(self, engine: AsyncEngine, proposals: ProposalSink) -> None:
        self._engine = engine
        self._proposals = proposals

    async def propose(self, claims: RunTokenClaims, soul: str, summary: str) -> str:
        """Stores the proposal and returns the text for the agent. Raises ``SoulError``."""
        soul = soul.strip()
        summary = " ".join(summary.split())[:SUMMARY_MAX_CHARS]
        if not soul:
            raise SoulError("give the full new soul")
        if not summary:
            raise SoulError("give a summary of the change")
        if len(soul) > SOUL_MAX_CHARS:
            raise SoulError(f"the soul has at most {SOUL_MAX_CHARS} characters")
        if redact(soul) != soul:
            raise SoulError("the soul contains a secret. Remove it")
        run = await self._load(claims)
        if run.kind != "chat" or run.unattended:
            raise SoulError("only a chat run with a person in it can propose a new soul")
        if run.lineage == "delegation":
            raise SoulError("a delegated child run cannot propose a new soul. Ask the parent run")
        if soul == run.soul:
            return "The soul is already this text. Nothing to propose."
        scope: ProposalScope = "workspace" if run.profile_workspace_id is not None else "org"
        await self._proposals.propose(
            NewProposal(
                org_id=uuid.UUID(claims.org_id),
                workspace_id=run.workspace_id,
                session_id=uuid.UUID(claims.session_id),
                run_id=uuid.UUID(claims.run_id),
                acting_user_id=run.acting_user_id,
                kind="soul",
                scope=scope,
                payload={
                    "profile_id": str(run.profile_id),
                    "profile_name": run.profile_name,
                    "soul": soul,
                    "summary": summary,
                    "base_digest": soul_digest(run.soul),
                },
                summary=f"set the soul of the agent profile {run.profile_name}: {summary}",
                origin="chat",
            )
        )
        who = "A workspace admin or the org owner" if scope == "workspace" else "Only the org owner"
        return (
            f"Proposed a new soul for the profile {run.profile_name}. It is not saved. {who}"
            " must approve it first. An approval card shows the change. After the approval,"
            " the new soul applies from the next session. Do not propose it again."
        )

    async def _load(self, claims: RunTokenClaims) -> _RunProfile:
        org_id = uuid.UUID(claims.org_id)
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(
                        Session.workspace_id,
                        Session.lineage,
                        Session.audience,
                        Session.owner_user_id,
                        Run.agent_profile_id,
                        Run.kind,
                        Run.unattended,
                        Run.triggered_by_user_id,
                        AgentProfile.workspace_id.label("profile_workspace_id"),
                        AgentProfile.name,
                        AgentProfile.soul,
                    )
                    .join(Run, and_(Run.org_id == Session.org_id, Run.session_id == Session.id))
                    .join(
                        AgentProfile,
                        and_(
                            AgentProfile.org_id == Run.org_id,
                            AgentProfile.id == Run.agent_profile_id,
                        ),
                    )
                    .where(
                        Session.org_id == org_id,
                        Session.id == uuid.UUID(claims.session_id),
                        Run.id == uuid.UUID(claims.run_id),
                    )
                )
            ).one_or_none()
        if row is None:
            raise SoulError("the run is not known")
        acting = row.triggered_by_user_id or (
            row.owner_user_id if row.audience == "private" else None
        )
        return _RunProfile(
            workspace_id=row.workspace_id,
            profile_id=row.agent_profile_id,
            profile_workspace_id=row.profile_workspace_id,
            profile_name=row.name,
            soul=row.soul,
            kind=row.kind,
            unattended=bool(row.unattended),
            lineage=row.lineage,
            acting_user_id=acting,
        )


async def set_soul(
    connection: AsyncConnection,
    *,
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    soul: str,
    base_digest: str,
    actor_user_id: uuid.UUID,
    proposal_id: uuid.UUID,
) -> uuid.UUID | None:
    """Writes an approved soul on ``connection`` (in the transaction of the caller) and the
    audit entry. Returns the workspace of the profile. Raises ``SoulError`` when the profile
    does not exist, or when its soul changed after the proposal."""
    row = (
        await connection.execute(
            select(AgentProfile.soul, AgentProfile.workspace_id)
            .where(AgentProfile.org_id == org_id, AgentProfile.id == profile_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise SoulError("the agent profile does not exist now")
    if soul_digest(row.soul) != base_digest:
        raise SoulError("the soul changed after the proposal. Ask for a new proposal")
    if len(soul) > SOUL_MAX_CHARS or redact(soul) != soul:
        raise SoulError("the soul is not valid now")
    await connection.execute(
        update(AgentProfile)
        .where(AgentProfile.org_id == org_id, AgentProfile.id == profile_id)
        .values(soul=soul, updated_at=func.now())
    )
    await connection.execute(
        insert(AuditEntry).values(
            org_id=org_id,
            workspace_id=row.workspace_id,
            actor_kind="user",
            actor_user_id=actor_user_id,
            action="profile.updated",
            target_type="agent_profile",
            target_id=str(profile_id),
            details={"fields": ["soul"], "subagents": [], "proposal_id": str(proposal_id)},
        )
    )
    workspace_id: uuid.UUID | None = row.workspace_id
    return workspace_id
