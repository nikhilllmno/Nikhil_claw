"""The rules of who may write memory, and the steps of a write (Section 8, Section 9.4).

``MemoryService`` is the one place where the ``memory`` tool, the HTTP API, and revert check
a change. The store keeps the data, ``core`` keeps the text rules.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sama.memory.core import (
    Action,
    MemoryChangeError,
    MemoryDeniedError,
    Operation,
    Target,
    apply,
    clean,
    render_entries,
    usage,
    validate,
)
from sama.memory.store import (
    MemoryScope,
    MemoryState,
    MemoryStore,
    RevisionAction,
    RunContext,
    Writer,
)
from sama.tenancy.run_token import RunTokenClaims


def scope_of(
    target: Target,
    *,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    agent_profile_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
) -> MemoryScope:
    match target:
        case Target.AGENT:
            return MemoryScope(org_id, target, workspace_id, agent_profile_id)
        case Target.USER:
            return MemoryScope(org_id, target, user_id=user_id)
        case Target.WORKSPACE:
            return MemoryScope(org_id, target, workspace_id)


def action_of(operations: Sequence[Operation]) -> RevisionAction:
    """The label of the revision: the action of a single step, else ``batch``."""
    if len(operations) == 1:
        return operations[0].action.value
    return "batch"


class MemoryService:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    async def write(
        self,
        claims: RunTokenClaims,
        target: Target,
        operations: Sequence[Operation],
        reason: str | None,
    ) -> str:
        """The ``memory`` tool. Returns the text for the agent."""
        if not operations:
            raise MemoryChangeError("give an action or a list of operations")
        context = await self.store.context(claims)
        scope = self._scope(claims, context, target)

        def change(entries: list[str]) -> list[str]:
            new = apply(entries, operations)
            validate(target, entries, new)
            return new

        writer = Writer(
            "agent", context.acting_user_id, uuid.UUID(claims.session_id), uuid.UUID(claims.run_id)
        )
        state, changed = await self.store.change(
            scope, change, writer=writer, action=action_of(operations), reason=reason
        )
        return self._result(target, state, changed)

    def _scope(self, claims: RunTokenClaims, context: RunContext, target: Target) -> MemoryScope:
        if context.unattended:
            raise MemoryDeniedError("this run has no person in it, so it cannot change memory")
        if context.delegated:
            raise MemoryDeniedError(
                "a delegated child run cannot change memory. Give the facts to the parent run"
            )
        if target is Target.USER and claims.user_id is None:
            raise MemoryDeniedError(
                "user memory is only for a private session. This session is shared"
            )
        if target is Target.WORKSPACE and not context.workspace_admin:
            raise MemoryDeniedError(
                "only a workspace admin can change workspace memory. Ask an admin to make"
                " this change"
            )
        return scope_of(
            target,
            org_id=uuid.UUID(claims.org_id),
            workspace_id=uuid.UUID(claims.workspace_id),
            agent_profile_id=context.agent_profile_id,
            user_id=uuid.UUID(claims.user_id) if claims.user_id else None,
        )

    @staticmethod
    def _result(target: Target, state: MemoryState, changed: bool) -> str:
        head = (
            f"Saved {target.value} memory ({usage(target, state.entries)})."
            if changed
            else f"{target.value.capitalize()} memory has no change"
            f" ({usage(target, state.entries)})."
        )
        return (
            f"{head} The change applies from the next session. Entries:\n"
            f"{render_entries(state.entries)}"
        )

    async def edit(
        self,
        scope: MemoryScope,
        entries: Sequence[str],
        writer: Writer,
        reason: str | None,
    ) -> MemoryState:
        """A person replaces all entries (HTTP API)."""
        new = clean(entries)

        def change(old: list[str]) -> list[str]:
            validate(scope.target, old, new)
            return new

        state, _ = await self.store.change(
            scope, change, writer=writer, action="edit", reason=reason
        )
        return state

    async def revert(
        self, scope: MemoryScope, seq: int, writer: Writer, reason: str | None
    ) -> MemoryState:
        """A person sets the entries back to a revision (HTTP API). The result is a new
        revision, the history is not changed."""
        target = await self.store.revision(scope, seq)
        if target is None:
            raise MemoryChangeError(f"revision {seq} does not exist")
        new = list(target.entries)

        def change(old: list[str]) -> list[str]:
            validate(scope.target, old, new)
            return new

        state, _ = await self.store.change(
            scope, change, writer=writer, action="revert", reason=reason, reverted_to=seq
        )
        return state


__all__ = ["Action", "MemoryService", "action_of", "scope_of"]
