"""The skill editor routes of the web API (docs/architecture.md, Sections 9.2 and 11.4).

A person reads the skills of a workspace as a private session of the person sees them, and
writes a skill with the same rules as ``skill_manage``: a user skill is the own skill of the
person, a workspace skill needs a workspace admin or the org owner, and an org skill needs the
org owner. Builtin skills are read-only. The reads run as the user, so RLS applies. The writes
use ``PostgresSkillStore.edit``, which writes as the table owner after ``authorize``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api.auth import Principal
from sama.db.engine import as_user
from sama.db.models import Skill, SkillVersion, User, Workspace
from sama.skills.errors import SkillError
from sama.skills.library import Actor, ManageRequest, SkillDoc, authorize, resolve
from sama.skills.lint import (
    KNOWN_TOOLSETS,
    MAX_FILE_CHARS,
    MAX_NAME_CHARS,
    MAX_PATH_CHARS,
    MAX_SKILL_MD_CHARS,
    MAX_SUMMARY_CHARS,
)
from sama.skills.service import SkillLibrary
from sama.skills.sql import load_docs
from sama.skills.store import PostgresSkillStore
from sama.tenancy.access import Grants, load_grants

WritableScope = Literal["user", "workspace", "org"]
ReadScope = Literal["user", "workspace", "org", "builtin"]
WriteAction = Literal["create", "edit", "write_file", "remove_file", "archive"]
WRITABLE_SCOPES: Final = ("user", "workspace", "org")


class SkillWrite(BaseModel):
    """One change of a skill from the web app. ``scope`` is required, so a write never lands
    in a scope that the person did not choose."""

    model_config = ConfigDict(extra="forbid")

    action: WriteAction
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    scope: WritableScope
    content: str | None = Field(default=None, max_length=MAX_SKILL_MD_CHARS)
    """The whole ``SKILL.md`` for ``create`` and ``edit``."""
    file_path: str | None = Field(default=None, min_length=1, max_length=MAX_PATH_CHARS)
    file_content: str | None = Field(default=None, max_length=MAX_FILE_CHARS)
    summary: str | None = Field(default=None, max_length=MAX_SUMMARY_CHARS)


class SkillSummary(BaseModel):
    id: uuid.UUID | None
    name: str
    description: str
    scope: str
    version: str
    tags: list[str]
    can_edit: bool


class SkillCatalog(BaseModel):
    skills: list[SkillSummary]
    writable_scopes: list[str]
    """The scopes in which the person can create a skill in this workspace."""


class SkillDetail(SkillSummary):
    requires_toolsets: list[str]
    skill_md: str
    files: dict[str, str]
    other_scopes: list[str]
    """The other scopes that have a skill with this name. The narrowest scope wins."""


class SkillVersionDetail(BaseModel):
    skill_id: uuid.UUID
    version: int
    name: str
    description: str
    change: str
    summary: str | None
    author_kind: str
    author_user_id: uuid.UUID | None
    created_at: datetime
    skill_md: str
    files: dict[str, str]


class SkillWritten(BaseModel):
    name: str
    scope: str
    version: int
    change: str


def _not_found(what: str = "the skill does not exist") -> HTTPException:
    return HTTPException(status_code=404, detail=what)


class SkillEditor:
    """Reads and writes of skills for the web app."""

    def __init__(
        self, engine: AsyncEngine, library: SkillLibrary, store: PostgresSkillStore
    ) -> None:
        self._engine = engine
        self._library = library
        self._store = store

    async def _actor(self, principal: Principal, workspace_id: uuid.UUID) -> tuple[Actor, Grants]:
        """The person as a skill actor in a private session of the workspace. Raises ``404``
        if the person cannot see the workspace."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            org_id = (
                await connection.execute(
                    select(Workspace.org_id).where(Workspace.id == workspace_id)
                )
            ).scalar_one_or_none()
            user_id = (
                None
                if org_id is None
                else (
                    await connection.execute(
                        select(User.id).where(
                            User.org_id == org_id, User.auth_user_id == principal.auth_user_id
                        )
                    )
                ).scalar_one_or_none()
            )
            grants = (
                None
                if org_id is None or user_id is None
                else await load_grants(connection, org_id, user_id)
            )
        if org_id is None or grants is None or not grants.sees_workspace(workspace_id):
            raise _not_found("the workspace does not exist")
        actor = Actor(
            org_id=org_id,
            workspace_id=workspace_id,
            user_id=grants.user_id,
            private=True,
            toolsets=frozenset(KNOWN_TOOLSETS),
            workspace_admin=grants.admins_workspace(workspace_id),
            org_owner=grants.org_owner,
        )
        return actor, grants

    @staticmethod
    def _can_edit(scope: str, actor: Actor) -> bool:
        try:
            authorize(scope, actor)
        except SkillError:
            return False
        return True

    async def catalog(self, principal: Principal, workspace_id: uuid.UUID) -> SkillCatalog:
        actor, grants = await self._actor(principal, workspace_id)
        async with as_user(self._engine, principal.auth_user_id) as connection:
            docs = await self._library.catalog(
                connection,
                org_id=actor.org_id,
                workspace_id=workspace_id,
                user_id=grants.user_id,
            )
        return SkillCatalog(
            skills=[
                SkillSummary(
                    id=doc.skill_id,
                    name=doc.name,
                    description=doc.description,
                    scope=doc.scope,
                    version=doc.version,
                    tags=list(doc.tags),
                    can_edit=self._can_edit(doc.scope, actor),
                )
                for doc in docs
            ],
            writable_scopes=[s for s in WRITABLE_SCOPES if self._can_edit(s, actor)],
        )

    async def detail(
        self,
        principal: Principal,
        workspace_id: uuid.UUID,
        name: str,
        scope: ReadScope | None,
    ) -> SkillDetail:
        """One skill with its text and files. Without ``scope`` it is the skill that a
        session uses: the narrowest scope. Archived skills are not shown."""
        actor, _ = await self._actor(principal, workspace_id)
        async with as_user(self._engine, principal.auth_user_id) as connection:
            stored = await load_docs(connection, actor, names=[name], full=True)
        builtin = self._library.builtin.get(name)
        candidates: list[SkillDoc] = [
            doc for doc in [*stored, *([builtin] if builtin else [])] if not doc.archived
        ]
        if scope is None:
            found = resolve(candidates, actor).get(name)
        else:
            found = next((doc for doc in candidates if doc.scope == scope), None)
        if found is None:
            raise _not_found()
        return SkillDetail(
            id=found.skill_id,
            name=found.name,
            description=found.description,
            scope=found.scope,
            version=found.version,
            tags=list(found.tags),
            can_edit=self._can_edit(found.scope, actor),
            requires_toolsets=list(found.requires_toolsets),
            skill_md=found.skill_md,
            files=dict(found.files),
            other_scopes=[doc.scope for doc in candidates if doc.scope != found.scope],
        )

    async def version(
        self, principal: Principal, skill_id: uuid.UUID, version_no: int
    ) -> SkillVersionDetail:
        """One old version of a stored skill. RLS hides the skills that the user cannot
        read."""
        async with as_user(self._engine, principal.auth_user_id) as connection:
            org_id = (
                await connection.execute(select(Skill.org_id).where(Skill.id == skill_id))
            ).scalar_one_or_none()
            if org_id is None:
                raise _not_found()
            row = (
                await connection.execute(
                    select(
                        SkillVersion.skill_id,
                        SkillVersion.version_no.label("version"),
                        SkillVersion.name,
                        SkillVersion.description,
                        SkillVersion.change,
                        SkillVersion.summary,
                        SkillVersion.author_kind,
                        SkillVersion.author_user_id,
                        SkillVersion.created_at,
                        SkillVersion.skill_md,
                        SkillVersion.files,
                    ).where(
                        SkillVersion.org_id == org_id,
                        SkillVersion.skill_id == skill_id,
                        SkillVersion.version_no == version_no,
                    )
                )
            ).one_or_none()
        if row is None:
            raise _not_found("the version does not exist")
        return SkillVersionDetail.model_validate(row, from_attributes=True)

    async def write(
        self, principal: Principal, workspace_id: uuid.UUID, body: SkillWrite
    ) -> SkillWritten:
        """Writes one change. ``403`` if the person has no role for the scope, ``422`` for a
        change that the skill rules refuse."""
        actor, _ = await self._actor(principal, workspace_id)
        try:
            authorize(body.scope, actor)
        except SkillError as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        request = ManageRequest(
            action=body.action,
            name=body.name,
            scope=body.scope,
            content=body.content,
            file_path=body.file_path,
            file_content=body.file_content,
            summary=body.summary,
        )
        try:
            written = await self._store.edit(actor, request)
        except SkillError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return SkillWritten(
            name=written.name, scope=written.scope, version=written.version, change=written.change
        )
