"""Reads of the skill tables. The control plane reads as the table owner, so these queries
filter by the scopes of the actor themselves (the same rule as the RLS policy)."""

from __future__ import annotations

import uuid
from collections.abc import Collection

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from sama.db.models import AgentProfile, Membership, Run, Session, Skill, SkillVersion
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
                skill_md=row["skill_md"] if full else "",
                files=dict(row["files"]) if full else {},
            )
        )
    return docs


async def profile_limits(
    connection: AsyncConnection, org_id: uuid.UUID, profile_id: uuid.UUID
) -> tuple[frozenset[str], frozenset[str]]:
    """The toolsets and the skill list of an agent profile."""
    row = (
        await connection.execute(
            select(AgentProfile.toolsets, AgentProfile.skills).where(
                AgentProfile.org_id == org_id, AgentProfile.id == profile_id
            )
        )
    ).one_or_none()
    if row is None:
        return frozenset(), frozenset()
    return frozenset(map(str, row[0])), frozenset(map(str, row[1]))


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
            )
            .join(Run, and_(Run.org_id == Session.org_id, Run.session_id == Session.id))
            .where(Session.org_id == org_id, Session.id == session_id, Run.id == run_id)
        )
    ).one_or_none()
    if row is None:
        raise SkillError("The run is not known.")
    workspace_id, audience, owner, profile_id, triggered_by = row
    user_id = run_user(triggered_by, audience, owner) or (
        uuid.UUID(token_user) if token_user else None
    )
    toolsets, allowed = await profile_limits(connection, org_id, profile_id)
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
    )
