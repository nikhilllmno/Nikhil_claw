"""Reads of the skill tables. The control plane reads as the table owner, so these queries
filter by the scopes of the actor themselves (the same rule as the RLS policy)."""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable

from sqlalchemy import ColumnElement, and_, func, insert, literal, or_, select, update
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncConnection

from sama.db.models import AgentProfile, Membership, Run, Session, Skill, SkillVersion
from sama.gate.decide import effective_toolsets, run_limit
from sama.skills.errors import SkillError
from sama.skills.library import Actor, SkillDoc
from sama.tenancy.authority import run_user


def scope_filter(actor: Actor) -> ColumnElement[bool]:
    """The rows of ``skills`` that the actor can reach: the org, the workspace of the session,
    and, in a private session, the skills of the user."""
    options = [
        Skill.scope == "org",
        and_(Skill.scope == "workspace", Skill.workspace_id == actor.workspace_id),
    ]
    if actor.sees_user_skills:
        options.append(and_(Skill.scope == "user", Skill.user_id == actor.user_id))
    return and_(Skill.org_id == actor.org_id, or_(*options))


async def load_docs(
    connection: AsyncConnection, actor: Actor, *, names: Collection[str] | None = None, full: bool
) -> list[SkillDoc]:
    """The stored skills of the actor, archived skills included. Without ``full``, the text
    and the files are empty."""
    columns = [
        Skill.id,
        Skill.scope,
        Skill.name,
        Skill.status,
        Skill.pinned,
        SkillVersion.version_no,
        SkillVersion.description,
        SkillVersion.tags,
        SkillVersion.requires_toolsets,
    ]
    if full:
        columns += [SkillVersion.skill_md, SkillVersion.files]
    query = (
        select(*columns)
        .join(
            SkillVersion,
            and_(
                SkillVersion.org_id == Skill.org_id,
                SkillVersion.skill_id == Skill.id,
                SkillVersion.version_no == Skill.current_version,
            ),
        )
        .where(scope_filter(actor))
    )
    if names is not None:
        query = query.where(Skill.name.in_(names))
    docs = []
    for row in (await connection.execute(query)).mappings():
        docs.append(
            SkillDoc(
                name=row["name"],
                scope=row["scope"],
                description=row["description"],
                version=str(row["version_no"]),
                tags=tuple(row["tags"]),
                requires_toolsets=tuple(row["requires_toolsets"]),
                skill_id=row["id"],
                archived=row["status"] == "archived",
                stale=row["status"] == "stale",
                pinned=row["pinned"],
                skill_md=row["skill_md"] if full else "",
                files=dict(row["files"]) if full else {},
            )
        )
    return docs


USAGE = {"view": Skill.view_count, "use": Skill.use_count}


async def count_usage(
    connection: AsyncConnection, org_id: uuid.UUID, skill_id: uuid.UUID, kind: str
) -> None:
    """Counts a view or a use of a stored skill (Section 9.7). A stale skill is active again.
    An archived skill does not change."""
    column = USAGE[kind]
    await connection.execute(
        update(Skill)
        .where(Skill.org_id == org_id, Skill.id == skill_id, Skill.status != "archived")
        .values(
            {
                column: column + 1,
                Skill.last_used_at: func.now(),
                Skill.status: "active",
                Skill.stale_since: None,
            }
        )
    )


async def profile_limits(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    *,
    audience: str = "private",
    kind: str = "chat",
    run_toolsets: Iterable[str] | None = None,
) -> tuple[frozenset[str], frozenset[str]]:
    """The toolsets and the skill list of an agent profile. The toolsets are the toolsets of the
    run: the group toolsets for a chat run in a shared session (Section 11.3), else the toolsets
    of the profile. So the skills of a run need the toolsets that the gate lets the run use."""
    row = (
        await connection.execute(
            select(AgentProfile.toolsets, AgentProfile.group_toolsets, AgentProfile.skills).where(
                AgentProfile.org_id == org_id, AgentProfile.id == profile_id
            )
        )
    ).one_or_none()
    if row is None:
        return frozenset(), frozenset()
    toolsets = effective_toolsets(
        map(str, row.toolsets),
        map(str, row.group_toolsets),
        audience=audience,
        kind=kind,
        run_toolsets=run_toolsets,
    )
    return toolsets, frozenset(map(str, row.skills))


async def load_actor(
    connection: AsyncConnection,
    *,
    org_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    token_user: str | None,
) -> Actor:
    """The actor of a run: the user of the run, its roles, and the limits of its profile.
    ``token_user`` is the user of the run token. It is the last choice for the user."""
    row = (
        await connection.execute(
            select(
                Session.workspace_id,
                Session.audience,
                Session.owner_user_id,
                Run.agent_profile_id,
                Run.triggered_by_user_id,
                Run.kind,
                Run.unattended,
                Session.lineage,
                Run.toolsets,
            )
            .join(Run, and_(Run.org_id == Session.org_id, Run.session_id == Session.id))
            .where(Session.org_id == org_id, Session.id == session_id, Run.id == run_id)
        )
    ).one_or_none()
    if row is None:
        raise SkillError("The run is not known.")
    workspace_id, audience, owner, profile_id, triggered_by, kind, unattended, lineage, limit = row
    user_id = run_user(triggered_by, audience, owner) or (
        uuid.UUID(token_user) if token_user else None
    )
    toolsets, allowed = await profile_limits(
        connection, org_id, profile_id, audience=audience, kind=kind, run_toolsets=run_limit(limit)
    )
    workspace_admin = org_owner = False
    if user_id is not None:
        roles = await connection.execute(
            select(Membership.workspace_id, Membership.role).where(
                Membership.org_id == org_id,
                Membership.user_id == user_id,
                or_(Membership.workspace_id.is_(None), Membership.workspace_id == workspace_id),
            )
        )
        for member_workspace, role in roles:
            if member_workspace is None:
                org_owner = role == "owner"
            elif role in ("owner", "admin"):
                workspace_admin = True
    return Actor(
        org_id=org_id,
        workspace_id=workspace_id,
        user_id=user_id,
        private=audience == "private",
        toolsets=toolsets,
        allowed=allowed,
        workspace_admin=workspace_admin,
        org_owner=org_owner,
        session_id=session_id,
        run_id=run_id,
        review=kind == "review",
        attended=kind == "chat" and not unattended and lineage != "delegation",
    )


async def copy_version(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    skill_id: uuid.UUID,
    from_version: int,
    *,
    change: str,
    summary: str,
    author_kind: str,
    author_user_id: uuid.UUID | None = None,
) -> int:
    """Writes version ``from_version + 1`` with the text and files of ``from_version`` and
    returns its number. An archive and a restore change no text, so they copy the version.
    The caller updates ``skills.current_version``."""
    version = from_version + 1
    await connection.execute(
        insert(SkillVersion).from_select(
            [
                "org_id",
                "skill_id",
                "version_no",
                "name",
                "description",
                "skill_md",
                "files",
                "tags",
                "requires_toolsets",
                "change",
                "summary",
                "author_kind",
                "author_user_id",
            ],
            select(
                SkillVersion.org_id,
                SkillVersion.skill_id,
                literal(version),
                SkillVersion.name,
                SkillVersion.description,
                SkillVersion.skill_md,
                SkillVersion.files,
                SkillVersion.tags,
                SkillVersion.requires_toolsets,
                literal(change),
                literal(summary),
                literal(author_kind),
                literal(author_user_id, type_=PG_UUID(as_uuid=True)),
            ).where(
                SkillVersion.org_id == org_id,
                SkillVersion.skill_id == skill_id,
                SkillVersion.version_no == from_version,
            ),
        )
    )
    return version
