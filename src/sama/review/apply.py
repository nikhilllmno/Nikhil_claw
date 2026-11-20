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
- ``FOR UPDATE SKIP LOCKED`` gives each proposal to one caller. If the process stops after the
  change but before the mark, the next call applies the change again. A memory change with the
  same entries and a skill patch that no longer matches are no-ops or a recorded failure, so
  the delivery is at-least-once and a repeat is safe.
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
from sama.memory.store import MemoryStore, Writer
from sama.skills.errors import SkillError
from sama.skills.library import Actor, ManageRequest
from sama.skills.store import PostgresSkillStore
from sama.tenancy.access import Grants, load_grants
from sama.vault.redact import redact

log: Final = logging.getLogger("sama.review")

BATCH: Final = 20
MAX_ERROR_CHARS: Final = 500
DISCARDED: Final = {"rejected": "a person rejected it", "expired": "the approval timed out"}


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
        async with self._engine.begin() as connection:
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
                .join(
                    Run,
                    and_(Run.org_id == ReviewProposal.org_id, Run.id == ReviewProposal.run_id),
                )
                .where(ReviewProposal.status == "pending", Approval.status != "pending")
                .order_by(ReviewProposal.created_at)
                .limit(BATCH)
                .with_for_update(of=ReviewProposal, skip_locked=True)
            )
            if org_id is not None and run_id is not None:
                query = query.where(
                    ReviewProposal.org_id == org_id, ReviewProposal.run_id == run_id
                )
            for row in (await connection.execute(query)).all():
                try:
                    await self._handle(connection, row)
                except Exception:
                    # The proposal stays pending. The next call tries it again.
                    log.exception("a review proposal could not be handled proposal=%s", row.id)
                    continue
                handled += 1
        return handled

    async def _handle(self, connection: AsyncConnection, row: Any) -> None:
        if row.decision != "approved":
            await self._mark(connection, row, "discarded", DISCARDED.get(row.decision, ""))
            return
        try:
            await self._apply(connection, row)
        except (MemoryChangeError, MemoryDeniedError, SkillError, ValueError, KeyError) as error:
            await self._mark(connection, row, "failed", redact(str(error))[:MAX_ERROR_CHARS])
        else:
            await self._mark(connection, row, "applied", None)

    async def _apply(self, connection: AsyncConnection, row: Any) -> None:
        decider = row.decided_by_user_id
        if decider is None:
            raise MemoryDeniedError("nobody approved the proposal")
        grants = await load_grants(connection, row.org_id, decider)
        if row.kind == "memory":
            await self._apply_memory(row, grants)
        else:
            await self._apply_skill(row, grants)

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

        await self._memory.change(
            scope,
            change,
            writer=Writer("agent", grants.user_id, row.session_id, row.run_id),
            action=action_of(operations),
            reason=row.payload.get("reason") or "approved proposal of a background review",
        )

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
        await self._skills.apply(actor, ManageRequest(**row.payload))

    @staticmethod
    async def _mark(connection: AsyncConnection, row: Any, status: str, error: str | None) -> None:
        await connection.execute(
            update(ReviewProposal)
            .where(ReviewProposal.org_id == row.org_id, ReviewProposal.id == row.id)
            .values(
                status=status,
                error=error or None,
                applied_at=func.now() if status == "applied" else None,
            )
        )
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
