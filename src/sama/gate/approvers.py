"""Who may decide an approval in a shared session (docs/architecture.md, Section 11.3).

Every member of a workspace can read a shared session, and every linked member of a group
can steer its run. So a button in a group must not let each member approve a risky tool call.
The default approvers of a shared session are the user who started the run, the org admins,
and the admins of the workspace. The key ``approvers`` of ``agent_profiles.policy`` widens the
set: ``["*"]`` means each member who can see the session, and a list of user ids adds those
users. A private session is not changed: only its owner sees it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from sama.tenancy.access import Grants

ANY_MEMBER: Final = "*"


@dataclass(frozen=True, slots=True)
class Approvers:
    """The extra approvers of a profile. The requester and the workspace admins always may."""

    any_member: bool = False
    users: frozenset[uuid.UUID] = frozenset()


def approvers_of(policy: Any) -> Approvers:
    """Reads ``approvers`` of the profile policy. A wrong value adds nobody, so it never
    widens the set by mistake."""
    value = policy.get("approvers") if isinstance(policy, Mapping) else None
    if not isinstance(value, list):
        return Approvers()
    users: set[uuid.UUID] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        try:
            users.add(uuid.UUID(item))
        except ValueError:
            continue
    return Approvers(any_member=ANY_MEMBER in value, users=frozenset(users))


def may_decide_shared(
    grants: Grants,
    workspace_id: uuid.UUID,
    *,
    requester: uuid.UUID | None,
    approvers: Approvers,
) -> bool:
    """True when the user of ``grants`` may decide an approval of a shared session that the
    user can see."""
    return (
        approvers.any_member
        or grants.user_id == requester
        or grants.user_id in approvers.users
        or grants.org_admin
        or grants.admins_workspace(workspace_id)
    )


SHARED_REFUSAL: Final = (
    "In a shared chat, only the person who asked or a workspace admin can decide this."
)
