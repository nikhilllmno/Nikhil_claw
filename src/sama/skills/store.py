"""The skill stores of the tool server: ``SkillStore`` (the interface), ``PostgresSkillStore``,
and ``MemorySkillStore`` for tests and local runs (docs/architecture.md, Section 9.3).

Every write is a new version. It locks the rows of the skill name, so two writes at the same
time make two versions in sequence.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import AuditEntry, Skill, SkillVersion
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

    def __init__(self, engine: AsyncEngine, builtin: Mapping[str, SkillDoc]) -> None:
        self._engine = engine
        self._builtin = builtin

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
                # The lock stops a second write to this name until this one commits.
                await connection.execute(
                    select(Skill.id)
                    .where(scope_filter(actor), Skill.name == request.name)
                    .with_for_update()
                )
                docs = await load_docs(connection, actor, names=[request.name], full=True)
                builtin = self._builtin.get(request.name)
                plan = plan_write([*docs, *([builtin] if builtin else [])], actor, request)
                return await self._save(connection, actor, plan, request)
        except IntegrityError:
            raise SkillError(
                f'A skill "{request.name}" was created at the same time. Try again.'
            ) from None

    async def _save(
        self, connection: AsyncConnection, actor: Actor, plan: WritePlan, request: ManageRequest
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
                author_kind="agent",
                author_user_id=actor.user_id,
                session_id=actor.session_id,
                run_id=actor.run_id,
            )
        )
        await connection.execute(
            insert(AuditEntry).values(
                org_id=actor.org_id,
                workspace_id=actor.workspace_id,
                actor_kind="agent",
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
