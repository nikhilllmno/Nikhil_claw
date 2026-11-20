"""Applies the decided proposals of background reviews (docs/architecture.md, Section 9.4).

A proposal waits for its approval. A person with the right role decides the approval in the web
UI (the API) or in any other place that uses ``GateService.decide_approval``. This module
does not depend on who decided:

- ``apply_decided`` finds the proposals whose approval is not pending any more. The worker calls
  it every maintenance round, and the API calls it after a decision, so the change applies at
  once.
- An ``approved`` proposal is applied with the roles of the person who approved it. The roles are
  read again, so a person who lost the role in the meantime does not apply a change.
- A ``rejected`` or ``expired`` approval discards the proposal.
- The change, the new status of the proposal, and the audit entry are one transaction. The
  status ``applied`` is set with ``UPDATE ... WHERE status = 'pending'`` inside the transaction
  of the change, after the write. If the process stops, or anything fails, before the commit,
  nothing is written and the proposal stays ``pending``, so the next call applies it once. If
  two callers work on one proposal, the second waits for the first, finds that the proposal is
  not ``pending``, and rolls its own change back. So a proposal changes the data at most once.
- A proposal that cannot be applied (a rule, a role, a stale target) gets the status ``failed``.
  Nothing was written then, so this mark is a second, short transaction.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Final

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import Approval, AuditEntry, ReviewProposal, Run, Session
from sama.memory.core import (
    MemoryChangeError,
    MemoryDeniedError,
    Operation,
    Target,
    apply,
    validate,
)
from sama.memory.service import action_of, scope_of
from sama.memory.store import MemoryState, MemoryStore, Writer
from sama.review.soul import SoulError, set_soul
from sama.skills.errors import SkillError
from sama.skills.library import Actor, ManageRequest
from sama.skills.store import Expected, PostgresSkillStore
from sama.tenancy.access import Grants, load_grants
from sama.vault.redact import redact

log: Final = logging.getLogger("sama.review")

BATCH: Final = 20
MAX_ERROR_CHARS: Final = 500
DISCARDED: Final = {"rejected": "a person rejected it", "expired": "the approval timed out"}


class _Settled(Exception):
    """Another caller already handled the proposal. The transaction of this caller ends."""


class ReviewApplier:
    """Applies the proposals that a person decided, and discards the others."""

    def __init__(
        self,
        engine: AsyncEngine,
        memory: MemoryStore,
        skills: PostgresSkillStore | None = None,
    ) -> None:
        self._engine = engine
        self._memory = memory
        self._skills = skills

    async def apply_decided(
        self, *, org_id: uuid.UUID | None = None, run_id: uuid.UUID | None = None
    ) -> int:
        """Handles the proposals with a decided approval. Returns how many it handled. With
        ``org_id`` and ``run_id``, it handles only the proposals of that review run."""
        handled = 0
        for row in await self._decided(org_id, run_id):
            try:
                if await self._handle(row):
                    handled += 1
            except Exception:
                # The proposal stays pending. The next call tries it again.
                log.exception("a review proposal could not be handled proposal=%s", row.id)
        return handled

    async def _decided(self, org_id: uuid.UUID | None, run_id: uuid.UUID | None) -> list[Any]:
        query = (
            select(
                ReviewProposal.id,
                ReviewProposal.org_id,
                ReviewProposal.session_id,
                ReviewProposal.run_id,
                ReviewProposal.kind,
                ReviewProposal.scope,
                ReviewProposal.payload,
                Approval.status.label("decision"),
                Approval.decided_by_user_id,
                Session.workspace_id,
                Run.agent_profile_id,
            )
            .join(
                Approval,
                and_(
                    Approval.org_id == ReviewProposal.org_id,
                    Approval.id == ReviewProposal.approval_id,
                ),
            )
            .join(
                Session,
                and_(
                    Session.org_id == ReviewProposal.org_id,
                    Session.id == ReviewProposal.session_id,
                ),
            )
            .join(Run, and_(Run.org_id == ReviewProposal.org_id, Run.id == ReviewProposal.run_id))
            .where(ReviewProposal.status == "pending", Approval.status != "pending")
            .order_by(ReviewProposal.created_at)
            .limit(BATCH)
        )
        if org_id is not None and run_id is not None:
            query = query.where(ReviewProposal.org_id == org_id, ReviewProposal.run_id == run_id)
        async with self._engine.connect() as connection:
            return list((await connection.execute(query)).all())

    async def _handle(self, row: Any) -> bool:
        """Returns False if another caller handled the proposal first."""
        if row.decision != "approved":
            return await self._finish(row, "discarded", DISCARDED.get(row.decision, ""))
        try:
            await self._apply(row)
        except _Settled:
            return False
        except (MemoryChangeError, MemoryDeniedError, SkillError, ValueError, KeyError) as error:
            return await self._finish(row, "failed", redact(str(error))[:MAX_ERROR_CHARS])
        return True

    async def _apply(self, row: Any) -> None:
        decider = row.decided_by_user_id
        if decider is None:
            raise MemoryDeniedError("nobody approved the proposal")
        async with self._engine.connect() as connection:
            grants = await load_grants(connection, row.org_id, decider)
        if row.kind == "memory":
            await self._apply_memory(row, grants)
        elif row.kind == "soul":
            await self._apply_soul(row, grants)
        else:
            await self._apply_skill(row, grants)

    async def _apply_soul(self, row: Any, grants: Grants) -> None:
        """A new soul of a profile (M3.2). The scope ``org`` needs the org owner, the scope
        ``workspace`` a workspace admin or the org owner, with the roles of now."""
        payload = row.payload
        allowed = (
            grants.org_owner if row.scope == "org" else grants.admins_workspace(row.workspace_id)
        )
        if not allowed:
            raise SoulError("the person who approved it does not have the role now")
        async with self._engine.begin() as connection:
            await set_soul(
                connection,
                org_id=row.org_id,
                profile_id=uuid.UUID(payload["profile_id"]),
                soul=str(payload["soul"]),
                base_digest=str(payload["base_digest"]),
                actor_user_id=grants.user_id,
                proposal_id=row.id,
            )
            await self._mark(connection, row, "applied", None)

    async def _apply_memory(self, row: Any, grants: Grants) -> None:
        target = Target(row.payload["target"])
        if not grants.admins_workspace(row.workspace_id):
            raise MemoryDeniedError("the person who approved it is not a workspace admin now")
        operations = [Operation.model_validate(item) for item in row.payload["operations"]]
        scope = scope_of(
            target,
            org_id=row.org_id,
            workspace_id=row.workspace_id,
            agent_profile_id=row.agent_profile_id,
            user_id=None,
        )

        def change(entries: list[str]) -> list[str]:
            new = apply(entries, operations)
            validate(target, entries, new)
            return new

        marked = False

        async def after(connection: AsyncConnection, state: MemoryState) -> None:
            nonlocal marked
            await self._mark(connection, row, "applied", None)
            marked = True

        await self._memory.change(
            scope,
            change,
            writer=Writer("agent", grants.user_id, row.session_id, row.run_id),
            action=action_of(operations),
            reason=row.payload.get("reason") or "approved proposal of a background review",
            after=after,
        )
        if not marked:
            # A store with no transaction (tests) ignores ``after``.
            await self._finish(row, "applied", None)

    async def _apply_skill(self, row: Any, grants: Grants) -> None:
        if self._skills is None:
            raise SkillError("this process cannot write skills")
        actor = Actor(
            org_id=row.org_id,
            workspace_id=row.workspace_id,
            user_id=grants.user_id,
            private=False,
            workspace_admin=grants.admins_workspace(row.workspace_id),
            org_owner=grants.org_owner,
            session_id=row.session_id,
            run_id=row.run_id,
        )
        # The proposal keeps the scope and the skill that the review saw. The request does not
        # resolve the name again, so a skill that was archived or hidden by a skill of
        # another scope since then is not replaced by that other skill.
        fields = dict(row.payload)
        target_id = fields.pop("target_skill_id")
        expected = Expected(
            skill_id=uuid.UUID(target_id) if target_id else None,
            version=fields.pop("target_version"),
        )
        request = ManageRequest(**fields)
        if request.scope != row.scope:
            raise SkillError("the scope of the proposal does not match its request")

        async def after(connection: AsyncConnection) -> None:
            await self._mark(connection, row, "applied", None)

        await self._skills.apply(actor, request, expected=expected, after=after)

    async def _finish(self, row: Any, status: str, error: str | None) -> bool:
        """Sets a status in a transaction of its own. Returns False if the proposal is not
        ``pending`` any more."""
        try:
            async with self._engine.begin() as connection:
                await self._mark(connection, row, status, error)
        except _Settled:
            return False
        return True

    @staticmethod
    async def _mark(connection: AsyncConnection, row: Any, status: str, error: str | None) -> None:
        """Sets the status of a pending proposal and writes the audit entry on ``connection``.
        Raises ``_Settled`` if the proposal is not ``pending`` (another caller was first)."""
        marked = await connection.execute(
            update(ReviewProposal)
            .where(
                ReviewProposal.org_id == row.org_id,
                ReviewProposal.id == row.id,
                ReviewProposal.status == "pending",
            )
            .values(
                status=status,
                error=error or None,
                applied_at=func.now() if status == "applied" else None,
            )
            .returning(ReviewProposal.id)
        )
        if marked.scalar_one_or_none() is None:
            raise _Settled
        await connection.execute(
            insert(AuditEntry).values(
                org_id=row.org_id,
                workspace_id=row.workspace_id,
                actor_kind="user" if row.decided_by_user_id else "system",
                actor_user_id=row.decided_by_user_id,
                action=f"review.{status}",
                target_type="review_proposal",
                target_id=str(row.id),
                run_id=row.run_id,
                details={
                    "session_id": str(row.session_id),
                    "kind": row.kind,
                    "scope": row.scope,
                    "error": error,
                },
            )
        )
