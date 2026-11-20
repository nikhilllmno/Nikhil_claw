"""The skill stores of the tool server: ``SkillStore`` (the interface), ``PostgresSkillStore``,
and ``MemorySkillStore`` for tests and local runs (docs/architecture.md, Section 9.3).

Every write is a new version. It locks the rows of the skill name, so two writes at the same
time make two versions in sequence.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Protocol, cast

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import AuditEntry, Skill, SkillVersion
from sama.review.proposals import NewProposal, ProposalOrigin, ProposalSink
from sama.skills.errors import SkillError
from sama.skills.library import Actor, ManageRequest, SkillDoc, WritePlan, index, plan_write
from sama.skills.sql import load_actor, load_docs, scope_filter
from sama.tenancy.run_token import RunTokenClaims


@dataclass(frozen=True, slots=True)
class Written:
    """The result of a write."""

    name: str
    scope: str
    version: int
    change: str
    proposed: bool = False
    """True when a background review proposed the change and a person must approve it first
    (Section 9.4). ``version`` is 0 then, and ``change`` is the action."""


@dataclass(frozen=True, slots=True)
class Expected:
    """The skill that a person approved: the row and the version that the background review
    saw (``None`` for a new skill). A write to anything else is refused (Section 9.4)."""

    skill_id: uuid.UUID | None
    version: int | None


class SkillStore(Protocol):
    async def list(self, claims: RunTokenClaims, query: str | None) -> list[SkillDoc]: ...

    async def view(self, claims: RunTokenClaims, name: str) -> SkillDoc: ...

    async def manage(self, claims: RunTokenClaims, request: ManageRequest) -> Written: ...


async def _actor(connection: AsyncConnection, claims: RunTokenClaims) -> Actor:
    return await load_actor(
        connection,
        org_id=uuid.UUID(claims.org_id),
        session_id=uuid.UUID(claims.session_id),
        run_id=uuid.UUID(claims.run_id),
        token_user=claims.user_id,
    )


def _matches(skill: SkillDoc, query: str | None) -> bool:
    if not query or not query.strip():
        return True
    needle = query.strip().lower()
    return needle in " ".join((skill.name, skill.description, *skill.tags)).lower()


def _find(docs: list[SkillDoc], actor: Actor, name: str) -> SkillDoc:
    for doc in index(docs, actor):
        if doc.name == name:
            return doc
    raise SkillError(f'There is no skill "{name}". Use skills_list to see the skills.')


class PostgresSkillStore:
    """``skills`` and ``skill_versions``, written as the table owner."""

    def __init__(
        self,
        engine: AsyncEngine,
        builtin: Mapping[str, SkillDoc],
        proposals: ProposalSink | None = None,
    ) -> None:
        self._engine = engine
        self._builtin = builtin
        self._proposals = proposals

    async def list(self, claims: RunTokenClaims, query: str | None) -> list[SkillDoc]:
        async with self._engine.connect() as connection:
            actor = await _actor(connection, claims)
            docs = [*await load_docs(connection, actor, full=False), *self._builtin.values()]
        return [doc for doc in index(docs, actor) if _matches(doc, query)]

    async def view(self, claims: RunTokenClaims, name: str) -> SkillDoc:
        async with self._engine.connect() as connection:
            actor = await _actor(connection, claims)
            docs = await load_docs(connection, actor, names=[name], full=True)
        builtin = self._builtin.get(name)
        return _find([*docs, *([builtin] if builtin else [])], actor, name)

    async def manage(self, claims: RunTokenClaims, request: ManageRequest) -> Written:
        try:
            async with self._engine.begin() as connection:
                actor = await _actor(connection, claims)
                if actor.review:
                    return await self._review(connection, actor, request)
                if actor.attended and self._proposals is not None:
                    proposed = await self._chat_proposal(connection, actor, request)
                    if proposed is not None:
                        return proposed
                return await self._write(connection, actor, request)
        except IntegrityError:
            raise SkillError(
                f'A skill "{request.name}" was created at the same time. Try again.'
            ) from None

    async def apply(
        self,
        actor: Actor,
        request: ManageRequest,
        *,
        expected: Expected,
        after: Callable[[AsyncConnection], Awaitable[None]] | None = None,
    ) -> Written:
        """Writes an approved proposal of a background review (Section 9.4). ``actor`` has
        the roles of the person who approved it, so ``authorize`` checks them again.
        ``request.scope`` is the scope that the person approved, and ``expected`` is the skill
        and the version. The write is refused if the skill is another one or changed. ``after``
        runs in the same transaction, after the write. If it raises, nothing is written."""
        try:
            async with self._engine.begin() as connection:
                written = await self._write(
                    connection, replace(actor, review=False), request, expected
                )
                if after is not None:
                    await after(connection)
                return written
        except IntegrityError:
            raise SkillError(
                f'A skill "{request.name}" was created at the same time. Try again.'
            ) from None

    async def edit(self, actor: Actor, request: ManageRequest) -> Written:
        """Writes a change that a person made in the web app (Section 11.4). ``actor`` has the
        roles of the person, so ``authorize`` checks them. The version and the audit entry name
        a ``user`` as the author. Nothing is proposed: a person without the role gets an
        error."""
        try:
            async with self._engine.begin() as connection:
                return await self._write(
                    connection, replace(actor, review=False), request, author_kind="user"
                )
        except IntegrityError:
            raise SkillError(
                f'A skill "{request.name}" was created at the same time. Try again.'
            ) from None

    async def _write(
        self,
        connection: AsyncConnection,
        actor: Actor,
        request: ManageRequest,
        expected: Expected | None = None,
        *,
        author_kind: str = "agent",
    ) -> Written:
        # The lock stops a second write to this name until this one commits.
        await connection.execute(
            select(Skill.id)
            .where(scope_filter(actor), Skill.name == request.name)
            .with_for_update()
        )
        docs = await load_docs(connection, actor, names=[request.name], full=True)
        builtin = self._builtin.get(request.name)
        plan = plan_write([*docs, *([builtin] if builtin else [])], actor, request)
        if expected is not None:
            found = plan.target
            actual = None if found is None else (found.skill_id, int(found.version))
            wanted = None if expected.skill_id is None else (expected.skill_id, expected.version)
            if actual != wanted:
                raise SkillError(
                    f'The skill "{request.name}" changed after the background review proposed'
                    " the change. Ask for a new proposal."
                )
        return await self._save(connection, actor, plan, request, author_kind)

    async def _plan_as_owner(
        self, connection: AsyncConnection, actor: Actor, request: ManageRequest
    ) -> WritePlan:
        """The plan with the roles of an org owner, to check the request and to learn the
        scope. The real check runs again when a person approves the proposal."""
        docs = await load_docs(connection, actor, names=[request.name], full=True)
        builtin = self._builtin.get(request.name)
        candidates = [*docs, *([builtin] if builtin else [])]
        return plan_write(candidates, replace(actor, workspace_admin=True, org_owner=True), request)

    async def _review(
        self, connection: AsyncConnection, actor: Actor, request: ManageRequest
    ) -> Written:
        """A background review writes a user skill itself. For any other scope it proposes
        the change, and a person approves it (Section 9.4)."""
        plan = await self._plan_as_owner(connection, actor, request)
        if plan.scope == "user":
            return await self._write(connection, actor, request)
        if self._proposals is None or actor.session_id is None or actor.run_id is None:
            raise SkillError("A background review cannot change a shared skill here.")
        if plan.scope not in ("workspace", "org"):
            raise SkillError(f"A background review cannot change a {plan.scope} skill.")
        return await self._propose(actor, request, plan, "review")

    async def _chat_proposal(
        self, connection: AsyncConnection, actor: Actor, request: ManageRequest
    ) -> Written | None:
        """A chat run whose user lacks the role for a workspace or org skill proposes the
        change. A workspace admin or the org owner approves a workspace skill, and the org owner
        approves an org skill (M3.2). Returns None when the user has the role, or when the
        request is not a shared write: the normal write then runs and gives its own answer."""
        try:
            plan = await self._plan_as_owner(connection, actor, request)
        except SkillError:
            return None
        has_role = actor.org_owner or (plan.scope == "workspace" and actor.workspace_admin)
        if has_role or plan.scope not in ("workspace", "org"):
            return None
        if actor.session_id is None or actor.run_id is None:
            return None
        return await self._propose(actor, request, plan, "chat")

    async def _propose(
        self, actor: Actor, request: ManageRequest, plan: WritePlan, origin: ProposalOrigin
    ) -> Written:
        if self._proposals is None or actor.session_id is None or actor.run_id is None:
            raise SkillError("This process cannot propose a shared skill.")
        target = plan.target
        await self._proposals.propose(
            NewProposal(
                org_id=actor.org_id,
                workspace_id=actor.workspace_id,
                session_id=actor.session_id,
                run_id=actor.run_id,
                acting_user_id=actor.user_id,
                kind="skill",
                scope="workspace" if plan.scope == "workspace" else "org",
                payload={
                    **asdict(request),
                    # The scope and the skill that the review saw. The approval is for them.
                    "scope": plan.scope,
                    "target_skill_id": None if target is None else str(target.skill_id),
                    "target_version": None if target is None else int(target.version),
                },
                summary=f"{request.action} the {plan.scope} skill {request.name}"
                + (f": {request.summary}" if request.summary else ""),
                origin=origin,
            )
        )
        return Written(request.name, plan.scope, 0, request.action, proposed=True)

    async def _save(
        self,
        connection: AsyncConnection,
        actor: Actor,
        plan: WritePlan,
        request: ManageRequest,
        author_kind: str,
    ) -> Written:
        target = plan.target
        if target is None:
            skill_id = (
                await connection.execute(
                    insert(Skill)
                    .values(
                        org_id=actor.org_id,
                        scope=plan.scope,
                        workspace_id=actor.workspace_id if plan.scope == "workspace" else None,
                        user_id=actor.user_id if plan.scope == "user" else None,
                        name=request.name,
                        current_version=1,
                    )
                    .returning(Skill.id)
                )
            ).scalar_one()
            version = 1
        else:
            skill_id, version = cast(uuid.UUID, target.skill_id), int(target.version) + 1
            await connection.execute(
                update(Skill)
                .where(Skill.org_id == actor.org_id, Skill.id == skill_id)
                .values(
                    current_version=version,
                    status="archived" if plan.change == "archive" else "active",
                    updated_at=func.now(),
                )
            )
        await connection.execute(
            insert(SkillVersion).values(
                org_id=actor.org_id,
                skill_id=skill_id,
                version_no=version,
                name=request.name,
                description=plan.description,
                skill_md=plan.skill_md,
                files=dict(plan.files),
                tags=list(plan.tags),
                requires_toolsets=list(plan.requires_toolsets),
                change=plan.change,
                summary=(request.summary or None) and request.summary[:500],
                author_kind=author_kind,
                author_user_id=actor.user_id,
                session_id=actor.session_id,
                run_id=actor.run_id,
            )
        )
        await connection.execute(
            insert(AuditEntry).values(
                org_id=actor.org_id,
                workspace_id=actor.workspace_id,
                actor_kind=author_kind,
                actor_user_id=actor.user_id,
                action=f"skill.{plan.change}",
                target_type="skill",
                target_id=str(skill_id),
                run_id=actor.run_id,
                details={"name": request.name, "scope": plan.scope, "version": version},
            )
        )
        return Written(request.name, plan.scope, version, plan.change)


@dataclass(slots=True)
class _Row:
    skill_id: uuid.UUID
    versions: list[SkillDoc]
    changes: list[str]
    archived: bool = False


class MemorySkillStore:
    """Skills in memory, for tests and for local runs without a database. The actor of every
    call comes from ``actor``, because there are no sessions or roles to read."""

    def __init__(
        self,
        actor: Actor | Callable[[RunTokenClaims], Actor],
        builtin: Mapping[str, SkillDoc] | None = None,
    ) -> None:
        self._actor = actor if callable(actor) else (lambda claims: actor)
        self._builtin = dict(builtin or {})
        self._rows: dict[tuple[str, uuid.UUID | None, uuid.UUID | None, str], _Row] = {}

    def _docs(self, actor: Actor, name: str | None = None) -> list[SkillDoc]:
        docs: list[SkillDoc] = []
        for (scope, workspace_id, user_id, row_name), row in self._rows.items():
            if name is not None and row_name != name:
                continue
            if (scope == "workspace" and workspace_id != actor.workspace_id) or (
                scope == "user" and not (actor.sees_user_skills and user_id == actor.user_id)
            ):
                continue
            current = row.versions[-1]
            docs.append(
                SkillDoc(
                    name=row_name,
                    scope=scope,
                    description=current.description,
                    version=str(len(row.versions)),
                    tags=current.tags,
                    requires_toolsets=current.requires_toolsets,
                    skill_id=row.skill_id,
                    archived=row.archived,
                    skill_md=current.skill_md,
                    files=dict(current.files),
                )
            )
        if name is None:
            return docs + list(self._builtin.values())
        builtin = self._builtin.get(name)
        return docs + ([builtin] if builtin else [])

    def history(self, scope: str, name: str) -> list[str]:
        """The changes of a skill, one per version. For tests."""
        return next(
            (row.changes for key, row in self._rows.items() if key[0] == scope and key[3] == name),
            [],
        )

    async def list(self, claims: RunTokenClaims, query: str | None) -> list[SkillDoc]:
        actor = self._actor(claims)
        return [doc for doc in index(self._docs(actor), actor) if _matches(doc, query)]

    async def view(self, claims: RunTokenClaims, name: str) -> SkillDoc:
        actor = self._actor(claims)
        return _find(self._docs(actor, name), actor, name)

    async def manage(self, claims: RunTokenClaims, request: ManageRequest) -> Written:
        actor = self._actor(claims)
        plan = plan_write(self._docs(actor, request.name), actor, request)
        key = (
            plan.scope,
            actor.workspace_id if plan.scope == "workspace" else None,
            actor.user_id if plan.scope == "user" else None,
            request.name,
        )
        doc = SkillDoc(
            name=request.name,
            scope=plan.scope,
            description=plan.description,
            version="",
            tags=plan.tags,
            requires_toolsets=plan.requires_toolsets,
            skill_md=plan.skill_md,
            files=dict(plan.files),
        )
        row = self._rows.get(key)
        if row is None:
            row = self._rows[key] = _Row(uuid.uuid4(), [], [])
        row.versions.append(doc)
        row.changes.append(plan.change)
        row.archived = plan.change == "archive"
        return Written(request.name, plan.scope, len(row.versions), plan.change)
