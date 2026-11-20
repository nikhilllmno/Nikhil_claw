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
    AfterChange,
    MemoryScope,
    MemoryState,
    MemoryStore,
    RevisionAction,
    RunContext,
    Writer,
)
from sama.review.proposals import NewProposal, ProposalOrigin, ProposalSink
from sama.tenancy.run_token import RunTokenClaims
from sama.vault.redact import redact


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


def describe_operations(target: Target, operations: Sequence[Operation]) -> str:
    """One line for the person who decides. Each text is redacted in full before it is cut,
    so a secret is never cut in the middle and shown in part. The text is not escaped
    (``repr``), because an escape would change a secret and hide it from the redaction."""
    steps = "; ".join(
        step.action.value + ' "' + _shown(step.content or step.old_text or "") + '"'
        for step in operations[:3]
    )
    more = f" and {len(operations) - 3} more" if len(operations) > 3 else ""
    return f"{target.value} memory: {steps}{more}"


def _shown(text: str, limit: int = 80) -> str:
    flat = " ".join(redact(text).split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def action_of(operations: Sequence[Operation]) -> RevisionAction:
    """The label of the revision: the action of a single step, else ``batch``."""
    if len(operations) == 1:
        return operations[0].action.value
    return "batch"


class MemoryService:
    """``proposals`` is where a background review sends its changes of shared memory
    (Section 9.4). Without it, a review run cannot change memory."""

    def __init__(self, store: MemoryStore, proposals: ProposalSink | None = None) -> None:
        self.store = store
        self.proposals = proposals

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
        if context.review and target is not Target.USER:
            return await self._propose(claims, context, target, operations, reason)
        if (
            target is Target.WORKSPACE
            and not context.workspace_admin
            and not context.unattended
            and not context.delegated
            and self.proposals is not None
        ):
            # A member who is not an admin asks for a change of workspace memory in a chat. The
            # change waits for a workspace admin or the org owner (M3.2, Section 9.4).
            return await self._propose(claims, context, target, operations, reason, origin="chat")
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

    async def _propose(
        self,
        claims: RunTokenClaims,
        context: RunContext,
        target: Target,
        operations: Sequence[Operation],
        reason: str | None,
        *,
        origin: ProposalOrigin = "review",
    ) -> str:
        """A background review does not change agent or workspace memory itself, and a chat run
        of a user who is not a workspace admin does not change workspace memory itself. The
        service checks the change against the current entries, and then it proposes the change.
        A person with the right role approves it (Section 9.4)."""
        if self.proposals is None:
            raise MemoryDeniedError("a background review cannot change shared memory here")
        scope = scope_of(
            target,
            org_id=uuid.UUID(claims.org_id),
            workspace_id=uuid.UUID(claims.workspace_id),
            agent_profile_id=context.agent_profile_id,
            user_id=None,
        )
        state = await self.store.read(scope)
        new = apply(list(state.entries), operations)
        validate(target, list(state.entries), new)
        if tuple(new) == state.entries:
            return self._result(target, state, False)
        await self.proposals.propose(
            NewProposal(
                org_id=scope.org_id,
                workspace_id=uuid.UUID(claims.workspace_id),
                session_id=uuid.UUID(claims.session_id),
                run_id=uuid.UUID(claims.run_id),
                acting_user_id=context.acting_user_id,
                kind="memory",
                scope="agent" if target is Target.AGENT else "workspace",
                payload={
                    "target": target.value,
                    "operations": [step.model_dump(mode="json") for step in operations],
                    "reason": reason,
                },
                summary=describe_operations(target, operations),
                origin=origin,
            )
        )
        who = (
            "A workspace admin or the org owner must approve it first."
            if target is Target.WORKSPACE
            else "A person must approve it first."
        )
        return (
            f"Proposed a change of {target.value} memory. It is not saved. {who} An approval"
            " card shows the change. Do not propose it again."
        )

    def _scope(self, claims: RunTokenClaims, context: RunContext, target: Target) -> MemoryScope:
        if context.unattended and not context.review:
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
        after: AfterChange | None = None,
    ) -> MemoryState:
        """A person replaces all entries (HTTP API). ``after`` runs in the transaction of the
        write (see ``AfterChange``)."""
        new = clean(entries)

        def change(old: list[str]) -> list[str]:
            validate(scope.target, old, new)
            return new

        state, _ = await self.store.change(
            scope, change, writer=writer, action="edit", reason=reason, after=after
        )
        return state

    async def revert(
        self,
        scope: MemoryScope,
        seq: int,
        writer: Writer,
        reason: str | None,
        after: AfterChange | None = None,
    ) -> MemoryState:
        """A person sets the entries back to a revision (HTTP API). The result is a new
        revision, the history is not changed. ``after`` runs in the transaction of the write."""
        target = await self.store.revision(scope, seq)
        if target is None:
            raise MemoryChangeError(f"revision {seq} does not exist")
        new = list(target.entries)

        def change(old: list[str]) -> list[str]:
            validate(scope.target, old, new)
            return new

        state, _ = await self.store.change(
            scope,
            change,
            writer=writer,
            action="revert",
            reason=reason,
            reverted_to=seq,
            after=after,
        )
        return state


__all__ = ["Action", "MemoryService", "action_of", "scope_of"]
