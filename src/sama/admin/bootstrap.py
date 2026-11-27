"""Creates the first org, owner, workspace, and agent profile of a new deploy
(docs/architecture.md, Section 19.4).

Every step finds the row first and adds it only when it is missing, so a second run changes
nothing. The owner is an org-level ``owner`` membership, so the owner sees every workspace.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import Select, insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.dml import ReturningInsert

from sama.db.models import AgentProfile, Membership, Org, User, Workspace
from sama.instructions import DEFAULT_SOUL

DEFAULT_MODEL: Final = "claude-sonnet-5-5"
DEFAULT_REASONING_EFFORT: Final = "high"
"""The reasoning effort of a new profile (M8.5). On the M8.5 benchmark ``high`` lifted the weak
tasks from 7.0 to 8.3 of 10 points, at about twice the model cost of the model default. An admin
can lower it in the Harness section of the console."""
DEFAULT_TOOLSETS: Final = (
    "core",
    "todo",
    "web",
    "clarify",
    "github",
    "plan",
    "memory",
    "skills",
    "sessions",
    "media",
    "tool_search",
    "subagents",
    "connections",
    "integrations",
    "delegation",
    "tasks",
    "cron",
    "code_exec",
    "workflows",
    "browser",
    "coding_agents",
    "custom_tools",
    "moa",
)
DEFAULT_GROUP_TOOLSETS: Final = ("todo", "web", "clarify", "memory", "sessions", "media", "tasks")
"""The toolsets of a shared session (Section 10.2): no shell, no code edit, no outbound write.
``skills`` is out because ``skill_manage`` writes workspace skills. ``github`` is out because
``repo_clone`` needs the sandbox. ``tasks`` is in because the run of a shared card is a shared
session, and it ends its card with ``task_complete``. ``delegation`` is out: a child run holds
sandboxes and budget. Migrations 0017 and 0023 have a copy of this list."""


class BootstrapError(ValueError):
    pass


def default_profile(
    org_id: uuid.UUID,
    *,
    name: str = "sama",
    model: str = DEFAULT_MODEL,
    budget: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The values of the org-wide agent profile of a new org. Sign-up (``sama.orgs``) uses
    them too, with the run budget of the plan. The profile gets the default SOUL of sama
    (M8.5)."""
    return {
        "org_id": org_id,
        "workspace_id": None,
        "name": name,
        "soul": DEFAULT_SOUL,
        "model": model,
        "reasoning_effort": DEFAULT_REASONING_EFFORT,
        "toolsets": list(DEFAULT_TOOLSETS),
        "group_toolsets": list(DEFAULT_GROUP_TOOLSETS),
        "budget": dict(budget or {}),
    }


@dataclass(frozen=True, slots=True)
class Owner:
    """``auth_user_id`` is the Supabase Auth user. Without it, ``email`` finds the user in
    ``auth.users`` (Supabase only)."""

    email: str
    display_name: str
    auth_user_id: uuid.UUID | None = None


@dataclass(slots=True)
class Bootstrapped:
    org_id: uuid.UUID
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    agent_profile_id: uuid.UUID
    added: list[str]
    """The kinds of rows that this call added, in order."""


async def bootstrap(
    engine: AsyncEngine,
    *,
    org_slug: str,
    org_name: str,
    owner: Owner,
    workspace_slug: str = "main",
    workspace_name: str = "Main",
    profile_name: str = "sama",
    model: str = DEFAULT_MODEL,
) -> Bootstrapped:
    async with engine.begin() as connection:
        added: list[str] = []

        async def find_or_add(kind: str, find: Select[Any], add: ReturningInsert[Any]) -> uuid.UUID:
            found = await connection.scalar(find)
            if found is None:
                found = await connection.scalar(add)
                added.append(kind)
            return uuid.UUID(str(found))

        auth_user_id = owner.auth_user_id or await _auth_user(connection, owner.email)
        org_id = await find_or_add(
            "org",
            select(Org.id).where(Org.slug == org_slug),
            # The operator makes this org, so it is internal (Section 18.4).
            insert(Org).values(name=org_name, slug=org_slug, is_internal=True).returning(Org.id),
        )
        user_id = await find_or_add(
            "user",
            select(User.id).where(User.org_id == org_id, User.auth_user_id == auth_user_id),
            insert(User)
            .values(
                org_id=org_id,
                auth_user_id=auth_user_id,
                email=owner.email,
                display_name=owner.display_name,
            )
            .returning(User.id),
        )
        await find_or_add(
            "membership",
            select(Membership.id).where(
                Membership.user_id == user_id, Membership.workspace_id.is_(None)
            ),
            insert(Membership)
            .values(org_id=org_id, user_id=user_id, workspace_id=None, role="owner")
            .returning(Membership.id),
        )
        workspace_id = await find_or_add(
            "workspace",
            select(Workspace.id).where(
                Workspace.org_id == org_id, Workspace.slug == workspace_slug
            ),
            insert(Workspace)
            .values(org_id=org_id, name=workspace_name, slug=workspace_slug)
            .returning(Workspace.id),
        )
        profile_id = await find_or_add(
            "agent_profile",
            select(AgentProfile.id)
            .where(
                AgentProfile.org_id == org_id,
                AgentProfile.workspace_id.is_(None),
                AgentProfile.name == profile_name,
            )
            .order_by(AgentProfile.created_at)
            .limit(1),
            insert(AgentProfile)
            .values(**default_profile(org_id, name=profile_name, model=model))
            .returning(AgentProfile.id),
        )
    return Bootstrapped(org_id, user_id, workspace_id, profile_id, added)


async def _auth_user(connection: AsyncConnection, email: str) -> uuid.UUID:
    has_table = await connection.scalar(text("SELECT to_regclass('auth.users') IS NOT NULL"))
    if not has_table:
        raise BootstrapError("this database has no auth.users; pass the auth user id")
    found = (
        await connection.scalars(
            text("SELECT id FROM auth.users WHERE lower(email) = lower(:email)"),
            {"email": email},
        )
    ).all()
    if len(found) != 1:
        raise BootstrapError(
            "no Supabase Auth user has this email; sign in once on the web page first"
        )
    return uuid.UUID(str(found[0]))
