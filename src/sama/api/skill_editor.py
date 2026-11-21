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
from typing import Any, Final, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api.auth import Principal
from sama.db.engine import as_user
from sama.db.models import AuditEntry, Skill, SkillVersion, Workspace
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
from sama.skills.settings import (
    MAX_SETTINGS,
    SkillSettingsStore,
    check_values,
    effective,
    read_settings,
    skill_specs,
)
from sama.skills.sql import copy_version, load_docs, scope_filter
from sama.skills.store import PostgresSkillStore
from sama.tenancy.access import Grants, load_grants
from sama.tenancy.regions import member_user_id

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
    status: str = "active"
    """``active`` or ``stale``. The curator marks an unused skill stale (Section 9.7)."""
    pinned: bool = False


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


class ArchivedSkill(BaseModel):
    """An archived skill. Sama never deletes a skill, so a person can restore it."""

    id: uuid.UUID
    name: str
    scope: str
    description: str
    version: int
    archived_at: datetime
    can_restore: bool


class SkillState(BaseModel):
    """The curator state of a stored skill after a pin, an unpin, or a restore."""

    id: uuid.UUID
    name: str
    scope: str
    status: str
    pinned: bool
    version: int


class SkillSettingsOut(BaseModel):
    """The settings of a skill for one workspace (Section 9.8). ``specs`` come from the
    ``config`` block of the skill that a session of the person uses. ``values`` are the
    stored values, ``effective`` the values that ``skill_view`` shows (a default for a value
    that is not set)."""

    name: str
    scope: str
    specs: list[dict[str, Any]]
    values: dict[str, Any]
    effective: dict[str, Any]
    can_edit: bool
    problem: str | None = None
    """Why the ``config`` block cannot be read. Then ``specs`` is empty."""


class SkillSettingsWrite(BaseModel):
    """The new values. A key with ``null`` goes back to its default. The body replaces all
    stored values."""

    model_config = ConfigDict(extra="forbid")

    values: dict[str, str | bool | int | float | None] = Field(max_length=MAX_SETTINGS)


StateAction = Literal["pin", "unpin", "restore"]
MAX_ARCHIVED: Final = 500


def _not_found(what: str = "the skill does not exist") -> HTTPException:
    return HTTPException(status_code=404, detail=what)


def _status(doc: SkillDoc) -> str:
    return "stale" if doc.stale else "active"


class SkillEditor:
    """Reads and writes of skills for the web app."""

    def __init__(
        self, engine: AsyncEngine, library: SkillLibrary, store: PostgresSkillStore
    ) -> None:
        self._engine = engine
        self._library = library
        self._store = store
        self._settings = SkillSettingsStore(engine)

    async def settings(
        self, principal: Principal, workspace_id: uuid.UUID, name: str
    ) -> SkillSettingsOut:
        """The settings of the skill that a session of the person uses in the workspace."""
        found = await self.detail(principal, workspace_id, name, None)
        actor, grants = await self._actor(principal, workspace_id)
        async with as_user(self._engine, principal.auth_user_id) as connection:
            stored = await read_settings(connection, actor.org_id, workspace_id, found.name)
        try:
            specs = skill_specs(found.skill_md)
            problem = None
        except SkillError as error:
            specs, problem = (), str(error)
        return SkillSettingsOut(
            name=found.name,
            scope=found.scope,
            specs=[spec.as_json() for spec in specs],
            values=stored,
            effective=effective(specs, stored),
            can_edit=grants.admins_workspace(workspace_id) or grants.org_owner,
            problem=problem,
        )

    async def put_settings(
        self, principal: Principal, workspace_id: uuid.UUID, name: str, body: SkillSettingsWrite
    ) -> SkillSettingsOut:
        """Replaces the values. Needs a workspace admin or the org owner."""
        current = await self.settings(principal, workspace_id, name)
        if not current.can_edit:
            raise HTTPException(
                status_code=403,
                detail="only a workspace admin or the org owner can change skill settings",
            )
        found = await self.detail(principal, workspace_id, name, None)
        actor, grants = await self._actor(principal, workspace_id)
        try:
            values = check_values(skill_specs(found.skill_md), body.values)
        except SkillError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        await self._settings.put(
            actor.org_id, workspace_id, found.name, values, actor_user_id=grants.user_id
        )
        return await self.settings(principal, workspace_id, name)

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
                else await member_user_id(connection, principal.auth_user_id, org_id)
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
                    status=_status(doc),
                    pinned=doc.pinned,
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
            status=_status(found),
            pinned=found.pinned,
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
            await member_user_id(connection, principal.auth_user_id, org_id)
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

    async def archived(self, principal: Principal, workspace_id: uuid.UUID) -> list[ArchivedSkill]:
        """The archived skills that the person sees in the workspace, the newest first."""
        actor, _ = await self._actor(principal, workspace_id)
        async with as_user(self._engine, principal.auth_user_id) as connection:
            rows = (
                await connection.execute(
                    select(
                        Skill.id,
                        Skill.name,
                        Skill.scope,
                        Skill.current_version,
                        Skill.updated_at,
                        SkillVersion.description,
                    )
                    .join(
                        SkillVersion,
                        and_(
                            SkillVersion.org_id == Skill.org_id,
                            SkillVersion.skill_id == Skill.id,
                            SkillVersion.version_no == Skill.current_version,
                        ),
                    )
                    .where(scope_filter(actor), Skill.status == "archived")
                    .order_by(Skill.updated_at.desc(), Skill.id)
                    .limit(MAX_ARCHIVED)
                )
            ).all()
        return [
            ArchivedSkill(
                id=row.id,
                name=row.name,
                scope=row.scope,
                description=row.description,
                version=row.current_version,
                archived_at=row.updated_at,
                can_restore=self._can_edit(row.scope, actor),
            )
            for row in rows
        ]

    async def change_state(
        self,
        principal: Principal,
        workspace_id: uuid.UUID,
        name: str,
        scope: WritableScope,
        action: StateAction,
    ) -> SkillState:
        """Pins, unpins, or restores a stored skill (Section 9.7). The rules of a write apply:
        ``403`` without a role for the scope, ``404`` for no skill, ``409`` for a wrong state.
        A pin makes a stale skill active. A restore makes an archived skill active with a new
        version that has the same text."""
        actor, _ = await self._actor(principal, workspace_id)
        try:
            authorize(scope, actor)
        except SkillError as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    select(Skill.id, Skill.status, Skill.pinned, Skill.current_version)
                    .where(scope_filter(actor), Skill.scope == scope, Skill.name == name)
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                raise _not_found()
            version = int(row.current_version)
            values: dict[str, Any] = {"updated_at": func.now()}
            if action == "restore":
                if row.status != "archived":
                    raise HTTPException(status_code=409, detail="the skill is not archived")
                version = await copy_version(
                    connection,
                    actor.org_id,
                    row.id,
                    version,
                    change="restore",
                    summary="A person restored the archived skill.",
                    author_kind="user",
                    author_user_id=actor.user_id,
                )
                values |= {
                    "status": "active",
                    "stale_since": None,
                    "current_version": version,
                    "last_used_at": func.now(),
                }
            else:
                if row.status == "archived":
                    raise HTTPException(
                        status_code=409, detail="the skill is archived. Restore it first."
                    )
                values["pinned"] = action == "pin"
                if action == "pin":
                    values |= {"status": "active", "stale_since": None}
            state = (
                await connection.execute(
                    update(Skill)
                    .where(Skill.org_id == actor.org_id, Skill.id == row.id)
                    .values(**values)
                    .returning(Skill.status, Skill.pinned)
                )
            ).one()
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=actor.org_id,
                    workspace_id=workspace_id,
                    actor_kind="user",
                    actor_user_id=actor.user_id,
                    action=f"skill.{action}",
                    target_type="skill",
                    target_id=str(row.id),
                    details={"name": name, "scope": scope, "version": version},
                )
            )
        return SkillState(
            id=row.id,
            name=name,
            scope=scope,
            status=state.status,
            pinned=state.pinned,
            version=version,
        )
