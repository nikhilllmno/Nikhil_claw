"""The permissions of the roles of an org (docs/architecture.md, Section 17.2; roadmap M7.10).

The catalog lists the actions that an org owner can allow or refuse for the role ``admin`` and
the role ``member``. A permission without a row in ``org_role_permissions`` is allowed, so an org
that never changes a permission keeps the behaviour of the roles. The org owner has every
permission. A key that is not for a role is never allowed for that role.

The check of a permission comes after the check of the role and adds to it: it never gives more
than the check of the role gives. The check is a function of the grants of the user and of the
rows of the org, so each caller (the API routes, the org service) uses it in the same way.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import AuditEntry, OrgRolePermission
from sama.tenancy.access import Grants, load_grants, lock_grants

ROLES: Final = ("admin", "member")
MAX_CHANGES: Final = 20
"""The most permissions that one change can name."""


@dataclass(frozen=True, slots=True)
class Permission:
    key: str
    label: str
    description: str
    roles: tuple[str, ...]


_BOTH: Final = ("admin", "member")
CATALOG: Final = (
    Permission("jobs.create", "Create scheduled jobs", "Make jobs that run on a schedule.", _BOTH),
    Permission("workflows.create", "Create workflows", "Make workflows with steps.", _BOTH),
    Permission(
        "triggers.create",
        "Create webhooks and triggers",
        "Make webhooks and triggers that start runs.",
        _BOTH,
    ),
    Permission(
        "connections.add",
        "Add connections and MCP servers",
        "Connect apps, MCP servers, and APIs to a workspace.",
        _BOTH,
    ),
    Permission(
        "skills.write", "Create and change skills", "Write, change, and archive skills.", _BOTH
    ),
    Permission(
        "browser.logins",
        "Add saved browser logins",
        "Save a login that the browser can use.",
        _BOTH,
    ),
    Permission(
        "sessions.shared",
        "Start shared sessions",
        "Start a session that the whole workspace can see.",
        _BOTH,
    ),
    Permission("members.invite", "Invite people", "Invite people to the org.", ("admin",)),
    Permission(
        "agents.manage",
        "Create and change agents and their tool policy",
        "Make agents, edit them, and set which tools ask, allow, or deny.",
        ("admin",),
    ),
    Permission(
        "chat_apps.manage",
        "Connect and disconnect chat apps",
        "Connect and disconnect Telegram and Slack.",
        ("admin",),
    ),
)
BY_KEY: Final = {permission.key: permission for permission in CATALOG}
KEYS: Final = tuple(permission.key for permission in CATALOG)


class PermissionDeniedError(Exception):
    """The role of the user in the org cannot do the action. The text is safe to show."""

    def __init__(self, label: str) -> None:
        super().__init__(f"Your role in this org cannot do this: {label}.")


class PermissionChangeError(Exception):
    """A change of permissions is not valid (``status_code`` 403 or 422). The text is safe to
    show."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def _cannot_edit(role: str) -> PermissionChangeError:
    return PermissionChangeError(
        403,
        "only the org owner can change the role admin"
        if role == "admin"
        else "only an org owner or admin can change the role member",
    )


async def stored_rows(
    connection: AsyncConnection, org_id: uuid.UUID
) -> dict[tuple[str, str], bool]:
    """The rows of the org as ``{(role, permission): allowed}``."""
    rows = await connection.execute(
        select(
            OrgRolePermission.role, OrgRolePermission.permission, OrgRolePermission.allowed
        ).where(OrgRolePermission.org_id == org_id)
    )
    return {(row.role, row.permission): row.allowed for row in rows}


def matrix(stored: Mapping[tuple[str, str], bool]) -> dict[str, dict[str, bool]]:
    """The permissions of each role: the stored value, or ``True`` without a row. A role has
    only the keys that are for it."""
    return {
        role: {
            permission.key: stored.get((role, permission.key), True)
            for permission in CATALOG
            if role in permission.roles
        }
        for role in ROLES
    }


def allowed_keys(role: str | None, stored: Mapping[tuple[str, str], bool]) -> list[str]:
    """The keys that a user with this org role may use. The owner has all keys. A role that is
    not ``admin`` or ``member`` has none."""
    if role == "owner":
        return list(KEYS)
    if role not in ROLES:
        return []
    return [key for key, allowed in matrix(stored)[role].items() if allowed]


def can_edit(grants: Grants) -> dict[str, bool]:
    """Who can change the permissions of each role: the owner changes both roles, and an admin
    changes the role ``member``."""
    return {"admin": grants.org_owner, "member": grants.org_admin}


def public(grants: Grants, stored: Mapping[tuple[str, str], bool]) -> dict[str, object]:
    """The answer of ``GET /orgs/{org_id}/role-permissions``."""
    return {
        "permissions": [
            {
                "key": permission.key,
                "label": permission.label,
                "description": permission.description,
                "roles": list(permission.roles),
            }
            for permission in CATALOG
        ],
        "roles": matrix(stored),
        "editable": can_edit(grants),
    }


async def require(connection: AsyncConnection, org_id: uuid.UUID, grants: Grants, key: str) -> None:
    """Raises ``PermissionDeniedError`` when the user may not use the permission ``key``. Call it
    after the check of the role."""
    permission = BY_KEY[key]
    role = grants.org_role
    if role == "owner":
        return
    if role not in permission.roles:
        raise PermissionDeniedError(permission.label)
    stored = await stored_rows(connection, org_id)
    if not stored.get((role, key), True):
        raise PermissionDeniedError(permission.label)


async def require_user(
    connection: AsyncConnection, org_id: uuid.UUID, user_id: uuid.UUID | None, key: str
) -> None:
    """``require`` for the user row of an actor. The service write paths call it, so the API and
    the agent tools refuse in the same way. Without a user (a run with no user), nothing is
    checked: a person did not ask for the write. Raises ``PermissionDeniedError``."""
    if user_id is None:
        return
    await require(connection, org_id, await load_grants(connection, org_id, user_id), key)


class RolePermissions:
    """Reads and changes the permissions of the roles of an org."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def require(self, org_id: uuid.UUID, grants: Grants, key: str) -> None:
        """See ``require``. Reads the rows with its own connection."""
        if grants.org_owner:
            return
        async with self._engine.connect() as connection:
            await require(connection, org_id, grants, key)

    async def view(self, org_id: uuid.UUID, grants: Grants) -> dict[str, object]:
        async with self._engine.connect() as connection:
            return public(grants, await stored_rows(connection, org_id))

    async def change(
        self,
        org_id: uuid.UUID,
        grants: Grants,
        role: str,
        changes: Mapping[str, bool],
    ) -> dict[str, object]:
        """Sets the permissions of ``role``. Raises ``PermissionChangeError``: ``422`` for a
        role or a key that is not valid, ``403`` when the user may not change the role. Writes
        the audit entry ``role.permissions_changed`` only when a value changes. Returns the
        view of ``view`` after the change."""
        if role not in ROLES:
            raise PermissionChangeError(422, "the role is not known: use admin or member")
        if not can_edit(grants)[role]:
            raise _cannot_edit(role)
        if not 1 <= len(changes) <= MAX_CHANGES:
            raise PermissionChangeError(422, f"change 1 to {MAX_CHANGES} permissions at a time")
        for key in changes:
            permission = BY_KEY.get(key)
            if permission is None:
                raise PermissionChangeError(422, f"the permission is not known: {key[:80]}")
            if role not in permission.roles:
                raise PermissionChangeError(422, f"the permission {key} is not for the role {role}")
        async with self._engine.begin() as connection:
            # The share lock keeps the role of the user until the end of the transaction.
            fresh = await lock_grants(connection, org_id, grants.user_id)
            if not can_edit(fresh)[role]:
                raise _cannot_edit(role)
            stored = await stored_rows(connection, org_id)
            changed = {
                key: allowed
                for key, allowed in changes.items()
                if stored.get((role, key), True) != allowed
            }
            for key, allowed in changed.items():
                statement = pg_insert(OrgRolePermission).values(
                    org_id=org_id,
                    role=role,
                    permission=key,
                    allowed=allowed,
                    updated_by_user_id=grants.user_id,
                )
                await connection.execute(
                    statement.on_conflict_do_update(
                        constraint="uq_org_role_permissions_org_id_role_permission",
                        set_={
                            "allowed": allowed,
                            "updated_by_user_id": grants.user_id,
                            "updated_at": func.now(),
                        },
                    )
                )
                stored[(role, key)] = allowed
            if changed:
                await connection.execute(
                    insert(AuditEntry).values(
                        org_id=org_id,
                        actor_kind="user",
                        actor_user_id=grants.user_id,
                        action="role.permissions_changed",
                        target_type="org",
                        target_id=str(org_id),
                        details={"role": role, "changed": changed},
                    )
                )
            return public(fresh, stored)
