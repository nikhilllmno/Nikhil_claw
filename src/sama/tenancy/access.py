"""The visibility rules of the RLS helpers, in code (docs/architecture.md, Section 17.1).

RLS is the second guard. The control plane checks the same rules in code first: the API
before each read and write (Section 11.4), and the tool gate before a human decision
(Section 16.1). The rules here are the rules of ``sama_private.can_see_workspace`` and
``sama_private.session_visible``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from sama.db.models import Membership

ADMIN_ROLES: Final = ("owner", "admin")


@dataclass(frozen=True, slots=True)
class Grants:
    """The memberships of one user row in one org."""

    user_id: uuid.UUID
    rows: tuple[tuple[uuid.UUID | None, str], ...]
    """``(workspace_id, role)`` pairs. A null workspace is the org membership."""

    @property
    def org_member(self) -> bool:
        return any(workspace is None for workspace, _ in self.rows)

    @property
    def org_admin(self) -> bool:
        return any(workspace is None and role in ADMIN_ROLES for workspace, role in self.rows)

    def sees_workspace(self, workspace_id: uuid.UUID) -> bool:
        """The rule of ``sama_private.can_see_workspace``."""
        return self.org_admin or (
            self.org_member and any(workspace == workspace_id for workspace, _ in self.rows)
        )

    def admins_workspace(self, workspace_id: uuid.UUID) -> bool:
        """True for the org ``owner`` and for a member with the role ``admin`` or ``owner`` in
        the workspace. These users may change workspace memory (Section 8)."""
        org_owner = any(workspace is None and role == "owner" for workspace, role in self.rows)
        workspace_admin = self.org_member and any(
            workspace == workspace_id and role in ADMIN_ROLES for workspace, role in self.rows
        )
        return org_owner or workspace_admin

    def sees_session(
        self, workspace_id: uuid.UUID, audience: str, owner_user_id: uuid.UUID | None
    ) -> bool:
        """The rule of ``sama_private.session_visible``."""
        if audience == "private":
            return owner_user_id == self.user_id and self.sees_workspace(workspace_id)
        return audience == "shared" and self.sees_workspace(workspace_id)


async def load_grants(connection: AsyncConnection, org_id: uuid.UUID, user_id: uuid.UUID) -> Grants:
    """Reads the memberships of a user row in an org."""
    rows = await connection.execute(
        select(Membership.workspace_id, Membership.role).where(
            Membership.org_id == org_id, Membership.user_id == user_id
        )
    )
    return Grants(user_id, tuple((row.workspace_id, row.role) for row in rows))
