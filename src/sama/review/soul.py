"""Proposals of a new soul for an agent profile (docs/architecture.md, Sections 9.4 and 10.2).

The ``change_propose`` tool (toolset ``self``) lets the agent propose a new soul for the profile
of the run. A soul changes how the agent behaves for every user of the profile, so a change is
always a proposal, also when the user of the run is an admin:

- A profile of one workspace: a workspace admin or the org owner approves it.
- A profile of the whole org (no workspace): only the org owner approves it.

The applier (``sama.review.apply``) writes the approved soul with ``set_soul``. The proposal
keeps a digest of the soul that the agent saw: the soul in the frozen config of the session. If
the soul of the profile is not that soul at the apply step (a person changed it before or after
the proposal), the proposal fails and the agent must propose again. A session keeps the soul
that it froze, so the new soul applies from the next session.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Final

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import AgentProfile, AuditEntry, Run, Session
from sama.profiles import SOUL_MAX_CHARS, record_change
from sama.review.proposals import NewProposal, ProposalScope, ProposalSink
from sama.tenancy.authority import run_user
from sama.tenancy.run_token import RunTokenClaims
from sama.vault.redact import redact

SUMMARY_MAX_CHARS: Final = 200


class SoulError(ValueError):
    """The proposal or its apply step is not valid. The text is safe to show."""


def soul_digest(soul: str) -> str:
    return hashlib.sha256(soul.encode("utf-8")).hexdigest()


def seen_soul(run_config: Any, live: str) -> str:
    """The soul that the run saw: the soul in the frozen config of the session. A session
    without a frozen config uses the soul of the profile now."""
    if isinstance(run_config, dict) and isinstance(run_config.get("soul"), str):
        return str(run_config["soul"])
    return live


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
        run = await load_run(self._engine, claims)
        seen = seen_soul(run.run_config, run.soul)
        if soul == seen:
            return "The soul is already this text. Nothing to propose."
        scope: ProposalScope = "workspace" if run.profile_workspace_id is not None else "org"
        await self._proposals.propose(
            NewProposal(
                org_id=uuid.UUID(claims.org_id),
                workspace_id=run.workspace_id,
                session_id=uuid.UUID(claims.session_id),
                run_id=uuid.UUID(claims.run_id),
                acting_user_id=run_user(run.triggered_by_user_id, run.audience, run.owner_user_id),
                kind="soul",
                scope=scope,
                payload={
                    "profile_id": str(run.agent_profile_id),
                    "profile_name": run.name,
                    "soul": soul,
                    "before_soul": seen,
                    "summary": summary,
                    "base_digest": soul_digest(seen),
                },
                summary=f"set the soul of the agent profile {run.name}: {summary}",
                origin="chat",
            )
        )
        who = "A workspace admin or the org owner" if scope == "workspace" else "Only the org owner"
        return (
            f"Proposed a new soul for the profile {run.name}. It is not saved. {who}"
            " must approve it first. An approval card shows the change. After the approval,"
            " the new soul applies from the next session. Do not propose it again."
        )


async def load_run(engine: AsyncEngine, claims: RunTokenClaims) -> Any:
    """The run, its session, and the profile of the run for ``change_propose``. Raises
    ``SoulError`` when the run cannot propose a change: only a chat run with a person in it
    can, and not a delegated child run."""
    org_id = uuid.UUID(claims.org_id)
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                select(
                    Session.workspace_id,
                    Session.lineage,
                    Session.audience,
                    Session.owner_user_id,
                    Session.run_config,
                    Run.agent_profile_id,
                    Run.kind,
                    Run.unattended,
                    Run.triggered_by_user_id,
                    AgentProfile.workspace_id.label("profile_workspace_id"),
                    AgentProfile.name,
                    AgentProfile.soul,
                    AgentProfile.runtime,
                    AgentProfile.model,
                    AgentProfile.review_model,
                    AgentProfile.toolsets,
                    AgentProfile.group_toolsets,
                    AgentProfile.subagents,
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
    if row.kind != "chat" or row.unattended:
        raise SoulError("only a chat run with a person in it can propose a change")
    if row.lineage == "delegation":
        raise SoulError("a delegated child run cannot propose a change. Ask the parent run")
    return row


async def set_soul(
    connection: AsyncConnection,
    *,
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    soul: str,
    base_digest: str,
    actor_user_id: uuid.UUID,
    proposal_id: uuid.UUID,
    summary: str | None = None,
) -> None:
    """Writes an approved soul on ``connection`` (in the transaction of the caller), the
    audit entry, and the history row (``profile_changes``). Raises ``SoulError`` when the
    profile does not exist, or when its soul is not the soul that the run saw."""
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
        raise SoulError("the soul changed after the agent saw it. Ask for a new proposal")
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
    await record_change(
        connection,
        org_id=org_id,
        workspace_id=row.workspace_id,
        profile_id=profile_id,
        kind="soul",
        source="proposal",
        before={"soul": row.soul},
        after={"soul": soul},
        actor_user_id=actor_user_id,
        proposal_id=proposal_id,
        summary=summary,
    )
