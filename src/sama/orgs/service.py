"""Self-serve sign-up, invites, and roles (docs/architecture.md, Section 17.2).

The API checks the rules here before each write, and writes as the table owner. Each write of
an org locks its ``orgs`` row first, so two admins cannot remove the last owner at the same
time. Org creation takes one advisory lock, so the rate limits count every org that is made
before it.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.admin.bootstrap import default_profile
from sama.db.engine import as_user
from sama.db.models import (
    INVITE_ROLES,
    AgentProfile,
    AuditEntry,
    Membership,
    Org,
    OrgInvite,
    User,
    Workspace,
)
from sama.orgs.emails import DISPOSABLE_DOMAINS, EmailCheck, blocked_domain, normalize_email
from sama.orgs.plans import SIGNUP_PLAN, plan_of
from sama.tenancy.access import Grants, load_grants

SignupMode = Literal["open", "invite_only"]
Role = Literal["owner", "admin", "member"]
InviteRole = Literal["admin", "member"]

SLUG_PATTERN: Final = r"^[a-z0-9](?:[a-z0-9-]{1,46}[a-z0-9])$"
_SLUG: Final = re.compile(SLUG_PATTERN)
RESERVED_SLUGS: Final = frozenset(
    {"admin", "api", "app", "help", "invite", "login", "sama", "settings", "signup", "www"}
)
MAX_ORG_NAME_CHARS: Final = 100
MAX_DISPLAY_NAME_CHARS: Final = 100
MAX_WORKSPACES_PER_CHANGE: Final = 100
_CREATE_LOCK: Final = 0x53414D414F524753
"""The advisory lock of org creation: the ASCII of ``SAMAORGS``."""
_PENDING = and_(OrgInvite.accepted_at.is_(None), OrgInvite.revoked_at.is_(None))


class OrgError(Exception):
    """A request that the rules refuse. ``status_code`` is the HTTP answer."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class SignupPolicy(BaseModel):
    """The settings of self-serve sign-up (``SAMA_SIGNUP``, ``SAMA_SIGNUP_BLOCKED_DOMAINS``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: SignupMode = "invite_only"
    """``open``: a user with a verified email makes an org. ``invite_only`` (the default): only
    an invite adds a user. Invites work in both modes."""
    blocked_domains: tuple[str, ...] = ()
    """Email domains that cannot make an org, in addition to ``DISPOSABLE_DOMAINS``."""
    orgs_per_user_per_day: int = Field(default=3, ge=1)
    orgs_per_user: int = Field(default=10, ge=1)
    """All the orgs that one Supabase Auth user made with sign-up."""
    orgs_per_hour: int = Field(default=50, ge=1)
    """All the orgs that sign-up made in the last hour, for every user."""
    invite_days: int = Field(default=7, ge=1, le=30)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> SignupPolicy:
        """Raises ``ValueError`` with the name of the variable when a value is not valid."""
        mode = env.get("SAMA_SIGNUP", "").strip().lower() or "invite_only"
        if mode not in ("open", "invite_only"):
            raise ValueError("SAMA_SIGNUP must be open or invite_only")
        domains = tuple(
            name.strip().lower().lstrip("@")
            for name in env.get("SAMA_SIGNUP_BLOCKED_DOMAINS", "").split(",")
            if name.strip()
        )
        return cls(mode="open" if mode == "open" else "invite_only", blocked_domains=domains)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def slug_from_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    return slug if len(slug) >= 3 and slug not in RESERVED_SLUGS else "org"


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _local_part(email: str) -> str:
    return email.partition("@")[0][:MAX_DISPLAY_NAME_CHARS] or email


async def _lock_org(connection: AsyncConnection, org_id: uuid.UUID) -> Any:
    row = (
        await connection.execute(
            select(Org.id, Org.name, Org.plan).where(Org.id == org_id).with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise OrgError(404, "the org does not exist")
    return row


async def _grants(
    connection: AsyncConnection, org_id: uuid.UUID, auth_user_id: uuid.UUID
) -> Grants | None:
    user_id = await connection.scalar(
        select(User.id).where(User.org_id == org_id, User.auth_user_id == auth_user_id)
    )
    if user_id is None:
        return None
    grants = await load_grants(connection, org_id, user_id)
    return grants if grants.org_member else None


def _role(grants: Grants) -> str:
    return next(role for workspace, role in grants.rows if workspace is None)


async def _member(
    connection: AsyncConnection, org_id: uuid.UUID, auth_user_id: uuid.UUID
) -> Grants:
    """The grants of the caller. Raises ``404`` for a user who is not a member, the same answer
    as an org that does not exist."""
    grants = await _grants(connection, org_id, auth_user_id)
    if grants is None:
        raise OrgError(404, "the org does not exist")
    return grants


async def _admin(connection: AsyncConnection, org_id: uuid.UUID, auth_user_id: uuid.UUID) -> Grants:
    grants = await _member(connection, org_id, auth_user_id)
    if not grants.org_admin:
        raise OrgError(403, "only an org admin can do this")
    return grants


async def _audit(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    details: dict[str, Any],
) -> None:
    await connection.execute(
        insert(AuditEntry).values(
            org_id=org_id,
            actor_kind="user",
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            details=details,
        )
    )


async def _owner_count(connection: AsyncConnection, org_id: uuid.UUID) -> int:
    return int(
        await connection.scalar(
            select(func.count()).where(
                Membership.org_id == org_id,
                Membership.workspace_id.is_(None),
                Membership.role == "owner",
            )
        )
        or 0
    )


def _invite_out(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "email": row.email,
        "role": row.role,
        "workspace_id": None if row.workspace_id is None else str(row.workspace_id),
        "invited_by": row.invited_by,
        "expires_at": _iso(row.expires_at),
        "created_at": _iso(row.created_at),
        "expired": bool(row.expired),
    }


class OrgService:
    def __init__(
        self, engine: AsyncEngine, emails: EmailCheck, policy: SignupPolicy | None = None
    ) -> None:
        self._engine = engine
        self._emails = emails
        self.policy = policy or SignupPolicy()

    async def _verified_email(self, auth_user_id: uuid.UUID) -> str:
        email = await self._emails.verified_email(auth_user_id)
        if email is None:
            raise OrgError(403, "confirm your email address first")
        return email

    # Org creation.

    async def create_org(
        self,
        auth_user_id: uuid.UUID,
        *,
        name: str,
        slug: str | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        policy = self.policy
        if policy.mode != "open":
            raise OrgError(403, "self-serve sign-up is closed: ask an org admin for an invite")
        email = await self._verified_email(auth_user_id)
        if blocked_domain(email, DISPOSABLE_DOMAINS | set(policy.blocked_domains)):
            raise OrgError(403, "this email domain cannot make an org")
        name = name.strip()
        if not name or len(name) > MAX_ORG_NAME_CHARS:
            raise OrgError(422, f"the name must have 1 to {MAX_ORG_NAME_CHARS} characters")
        if slug is not None and (not _SLUG.fullmatch(slug) or slug in RESERVED_SLUGS):
            raise OrgError(422, "the slug is not valid or is reserved")
        plan = SIGNUP_PLAN
        async with self._engine.begin() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CREATE_LOCK}
            )
            await self._check_rate(connection, auth_user_id)
            slug = await self._free_slug(connection, slug, name)
            org_id = (
                await connection.execute(
                    insert(Org)
                    .values(
                        name=name,
                        slug=slug,
                        plan=plan.name,
                        limits=dict(plan.limits),
                        created_by_auth_user_id=auth_user_id,
                    )
                    .returning(Org.id)
                )
            ).scalar_one()
            user_id = (
                await connection.execute(
                    insert(User)
                    .values(
                        org_id=org_id,
                        auth_user_id=auth_user_id,
                        email=email,
                        display_name=(display_name or "").strip() or _local_part(email),
                    )
                    .returning(User.id)
                )
            ).scalar_one()
            await connection.execute(
                insert(Membership).values(org_id=org_id, user_id=user_id, role="owner")
            )
            workspace_id = (
                await connection.execute(
                    insert(Workspace)
                    .values(org_id=org_id, name="Main", slug="main")
                    .returning(Workspace.id)
                )
            ).scalar_one()
            profile_id = (
                await connection.execute(
                    insert(AgentProfile)
                    .values(**default_profile(org_id, budget=plan.run_budget))
                    .returning(AgentProfile.id)
                )
            ).scalar_one()
            await _audit(
                connection,
                org_id,
                user_id,
                "org.created",
                "org",
                org_id,
                {"slug": slug, "plan": plan.name, "via": "signup"},
            )
        return {
            "id": str(org_id),
            "name": name,
            "slug": slug,
            "plan": plan.name,
            "user_id": str(user_id),
            "workspace_id": str(workspace_id),
            "agent_profile_id": str(profile_id),
        }

    async def _check_rate(self, connection: AsyncConnection, auth_user_id: uuid.UUID) -> None:
        policy = self.policy
        now = func.now()
        mine = (
            await connection.execute(
                select(
                    func.count(),
                    func.count().filter(Org.created_at > now - timedelta(days=1)),
                ).where(Org.created_by_auth_user_id == auth_user_id)
            )
        ).one()
        if mine[0] >= policy.orgs_per_user:
            raise OrgError(429, f"one user can make at most {policy.orgs_per_user} orgs")
        if mine[1] >= policy.orgs_per_user_per_day:
            raise OrgError(
                429, f"one user can make at most {policy.orgs_per_user_per_day} orgs in a day"
            )
        recent = await connection.scalar(
            select(func.count()).where(
                Org.created_at > now - timedelta(hours=1),
                Org.created_by_auth_user_id.is_not(None),
            )
        )
        if (recent or 0) >= policy.orgs_per_hour:
            raise OrgError(429, "too many new orgs: try again later")

    async def _free_slug(self, connection: AsyncConnection, slug: str | None, name: str) -> str:
        async def taken(value: str) -> bool:
            return await connection.scalar(select(Org.id).where(Org.slug == value)) is not None

        if slug is not None:
            if await taken(slug):
                raise OrgError(409, "this slug is taken")
            return slug
        base = slug_from_name(name)
        candidate = base
        for _ in range(8):
            if not await taken(candidate):
                return candidate
            candidate = f"{base}-{secrets.token_hex(3)}"
        raise OrgError(409, "no free slug: give a slug")

    # Invites.

    async def list_invites(
        self, auth_user_id: uuid.UUID, org_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        """The open invites of the org, newest first. For org admins."""
        async with as_user(self._engine, auth_user_id) as connection:
            await _admin(connection, org_id, auth_user_id)
            rows = await connection.execute(
                select(
                    OrgInvite.id,
                    OrgInvite.email,
                    OrgInvite.role,
                    OrgInvite.workspace_id,
                    OrgInvite.expires_at,
                    OrgInvite.created_at,
                    User.display_name.label("invited_by"),
                    (OrgInvite.expires_at <= func.now()).label("expired"),
                )
                .outerjoin(
                    User,
                    and_(User.org_id == OrgInvite.org_id, User.id == OrgInvite.invited_by_user_id),
                )
                .where(OrgInvite.org_id == org_id, _PENDING)
                .order_by(OrgInvite.created_at.desc())
                .limit(200)
            )
            return [_invite_out(row) for row in rows]

    async def create_invite(
        self,
        auth_user_id: uuid.UUID,
        org_id: uuid.UUID,
        *,
        email: str,
        role: str = "member",
        workspace_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Makes an invite and returns it with its token. The token is shown only here."""
        address = normalize_email(email)
        if address is None:
            raise OrgError(422, "the email address is not valid")
        if role not in INVITE_ROLES:
            raise OrgError(422, "an invite gives the role admin or member")
        if workspace_id is not None and role != "member":
            raise OrgError(422, "only a member invite names a workspace: an admin sees all")
        async with self._engine.begin() as connection:
            org = await _lock_org(connection, org_id)
            actor = await _admin(connection, org_id, auth_user_id)
            if workspace_id is not None:
                found = await connection.scalar(
                    select(Workspace.id).where(
                        Workspace.org_id == org_id, Workspace.id == workspace_id
                    )
                )
                if found is None:
                    raise OrgError(404, "the workspace does not exist")
            member = await connection.scalar(
                select(Membership.id)
                .join(User, and_(User.org_id == Membership.org_id, User.id == Membership.user_id))
                .where(
                    Membership.org_id == org_id,
                    Membership.workspace_id.is_(None),
                    func.lower(User.email) == address,
                )
                .limit(1)
            )
            if member is not None:
                raise OrgError(409, "this email is already a member of the org")
            plan = plan_of(org.plan)
            if plan.invites_per_day is not None:
                today = await connection.scalar(
                    select(func.count()).where(
                        OrgInvite.org_id == org_id,
                        OrgInvite.created_at > func.now() - timedelta(days=1),
                    )
                )
                if (today or 0) >= plan.invites_per_day:
                    raise OrgError(429, f"the plan allows {plan.invites_per_day} invites in a day")
            if plan.max_members is not None:
                members = await connection.scalar(
                    select(func.count()).where(
                        Membership.org_id == org_id, Membership.workspace_id.is_(None)
                    )
                )
                pending = await connection.scalar(
                    select(func.count()).where(
                        OrgInvite.org_id == org_id,
                        _PENDING,
                        OrgInvite.expires_at > func.now(),
                        OrgInvite.email != address,
                    )
                )
                if (members or 0) + (pending or 0) >= plan.max_members:
                    raise OrgError(
                        403,
                        f"the plan of this org allows {plan.max_members} members,"
                        " open invites included",
                    )
            # One open invite for each email: a new invite ends the old one.
            await connection.execute(
                update(OrgInvite)
                .where(OrgInvite.org_id == org_id, OrgInvite.email == address, _PENDING)
                .values(revoked_at=func.now())
            )
            token = secrets.token_urlsafe(32)
            row = (
                await connection.execute(
                    insert(OrgInvite)
                    .values(
                        org_id=org_id,
                        email=address,
                        role=role,
                        workspace_id=workspace_id,
                        token_hash=hash_token(token),
                        invited_by_user_id=actor.user_id,
                        expires_at=func.now() + timedelta(days=self.policy.invite_days),
                    )
                    .returning(OrgInvite.id, OrgInvite.expires_at, OrgInvite.created_at)
                )
            ).one()
            await _audit(
                connection,
                org_id,
                actor.user_id,
                "invite.created",
                "org_invite",
                row.id,
                {
                    "email": address,
                    "role": role,
                    "workspace_id": None if workspace_id is None else str(workspace_id),
                },
            )
            inviter = await connection.scalar(
                select(User.display_name).where(User.id == actor.user_id)
            )
        return {
            "id": str(row.id),
            "email": address,
            "role": role,
            "workspace_id": None if workspace_id is None else str(workspace_id),
            "invited_by": inviter,
            "expires_at": _iso(row.expires_at),
            "created_at": _iso(row.created_at),
            "expired": False,
            "token": token,
        }

    async def revoke_invite(
        self, auth_user_id: uuid.UUID, org_id: uuid.UUID, invite_id: uuid.UUID
    ) -> None:
        async with self._engine.begin() as connection:
            await _lock_org(connection, org_id)
            actor = await _admin(connection, org_id, auth_user_id)
            email = await connection.scalar(
                update(OrgInvite)
                .where(OrgInvite.org_id == org_id, OrgInvite.id == invite_id, _PENDING)
                .values(revoked_at=func.now())
                .returning(OrgInvite.email)
            )
            if email is None:
                raise OrgError(404, "the invite does not exist")
            await _audit(
                connection,
                org_id,
                actor.user_id,
                "invite.revoked",
                "org_invite",
                invite_id,
                {"email": email},
            )

    async def my_invites(self, auth_user_id: uuid.UUID) -> list[dict[str, Any]]:
        """The open invites for the verified email of the user. None without one."""
        email = await self._emails.verified_email(auth_user_id)
        if email is None:
            return []
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                select(
                    OrgInvite.id,
                    OrgInvite.org_id,
                    Org.name.label("org_name"),
                    OrgInvite.role,
                    OrgInvite.expires_at,
                    User.display_name.label("invited_by"),
                )
                .join(Org, Org.id == OrgInvite.org_id)
                .outerjoin(
                    User,
                    and_(User.org_id == OrgInvite.org_id, User.id == OrgInvite.invited_by_user_id),
                )
                .where(OrgInvite.email == email, _PENDING, OrgInvite.expires_at > func.now())
                .order_by(OrgInvite.created_at.desc())
                .limit(50)
            )
            return [
                {
                    "id": str(row.id),
                    "org_id": str(row.org_id),
                    "org_name": row.org_name,
                    "role": row.role,
                    "invited_by": row.invited_by,
                    "expires_at": _iso(row.expires_at),
                }
                for row in rows
            ]

    async def accept_invite(
        self,
        auth_user_id: uuid.UUID,
        *,
        token: str | None = None,
        invite_id: uuid.UUID | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        """Adds the user to the org of the invite. The verified email of the user must be the
        email of the invite. ``token`` comes from the link; ``invite_id`` from ``my_invites``.
        """
        if (token is None) == (invite_id is None):
            raise OrgError(422, "give the token or the invite id")
        email = await self._verified_email(auth_user_id)
        gone = "the invite does not exist or has ended"
        async with self._engine.begin() as connection:
            where = (
                OrgInvite.token_hash == hash_token(token)
                if token is not None
                else and_(OrgInvite.id == invite_id, OrgInvite.email == email)
            )
            found = (
                await connection.execute(select(OrgInvite.id, OrgInvite.org_id).where(where))
            ).one_or_none()
            if found is None:
                raise OrgError(404, gone)
            org_id = found.org_id
            # Lock the org first and the invite second, in the same order as the other invite
            # and member changes, so that two transactions cannot wait for each other.
            org = await _lock_org(connection, org_id)
            invite = (
                await connection.execute(
                    select(
                        OrgInvite.id,
                        OrgInvite.email,
                        OrgInvite.role,
                        OrgInvite.workspace_id,
                        OrgInvite.accepted_at,
                        OrgInvite.revoked_at,
                        (OrgInvite.expires_at <= func.now()).label("expired"),
                    )
                    .where(OrgInvite.id == found.id)
                    .with_for_update()
                )
            ).one()
            if invite.accepted_at is not None or invite.revoked_at is not None or invite.expired:
                raise OrgError(410, gone)
            if invite.email != email:
                raise OrgError(
                    403, "this invite is for another email address: sign in with that address"
                )
            user_id = await connection.scalar(
                select(User.id).where(User.org_id == org_id, User.auth_user_id == auth_user_id)
            )
            if user_id is not None:
                if (await load_grants(connection, org_id, user_id)).org_member:
                    raise OrgError(409, "you are already a member of this org")
            else:
                user_id = (
                    await connection.execute(
                        insert(User)
                        .values(
                            org_id=org_id,
                            auth_user_id=auth_user_id,
                            email=email,
                            display_name=(display_name or "").strip()[:MAX_DISPLAY_NAME_CHARS]
                            or _local_part(email),
                        )
                        .returning(User.id)
                    )
                ).scalar_one()
            await connection.execute(
                insert(Membership).values(org_id=org_id, user_id=user_id, role=invite.role)
            )
            if invite.workspace_id is not None:
                await connection.execute(
                    insert(Membership).values(
                        org_id=org_id,
                        user_id=user_id,
                        workspace_id=invite.workspace_id,
                        role="member",
                    )
                )
            await connection.execute(
                update(OrgInvite)
                .where(OrgInvite.id == invite.id)
                .values(accepted_at=func.now(), accepted_user_id=user_id)
            )
            await _audit(
                connection,
                org_id,
                user_id,
                "invite.accepted",
                "org_invite",
                invite.id,
                {"email": email, "role": invite.role},
            )
        return {
            "org_id": str(org_id),
            "org_name": org.name,
            "user_id": str(user_id),
            "role": invite.role,
        }

    # Members and roles.

    async def members(self, auth_user_id: uuid.UUID, org_id: uuid.UUID) -> list[dict[str, Any]]:
        """The members of the org, for every member. Reads with RLS."""
        async with as_user(self._engine, auth_user_id) as connection:
            await _member(connection, org_id, auth_user_id)
            return await _members(connection, org_id)

    async def change_member(
        self,
        auth_user_id: uuid.UUID,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        role: str | None = None,
        workspace_ids: Sequence[uuid.UUID] | None = None,
    ) -> dict[str, Any]:
        """Changes the org role or the workspaces of a member. An admin changes admins and
        members. Only an owner gives or takes the role ``owner``. The last owner stays."""
        async with self._engine.begin() as connection:
            await _lock_org(connection, org_id)
            actor = await _admin(connection, org_id, auth_user_id)
            target = await load_grants(connection, org_id, user_id)
            if not target.org_member:
                raise OrgError(404, "the member does not exist")
            current = _role(target)
            if current == "owner" and not actor.org_owner:
                raise OrgError(403, "only an owner can change an owner")
            if role is not None and role != current:
                if role == "owner" and not actor.org_owner:
                    raise OrgError(403, "only an owner can give the role owner")
                if current == "owner" and await _owner_count(connection, org_id) <= 1:
                    raise OrgError(409, "the org needs at least one owner")
                await connection.execute(
                    update(Membership)
                    .where(
                        Membership.org_id == org_id,
                        Membership.user_id == user_id,
                        Membership.workspace_id.is_(None),
                    )
                    .values(role=role)
                )
                await _audit(
                    connection,
                    org_id,
                    actor.user_id,
                    "member.role_changed",
                    "user",
                    user_id,
                    {"from": current, "to": role},
                )
            if workspace_ids is not None:
                await self._set_workspaces(connection, org_id, actor, target, workspace_ids)
            rows = await _members(connection, org_id, user_id)
        return rows[0]

    async def _set_workspaces(
        self,
        connection: AsyncConnection,
        org_id: uuid.UUID,
        actor: Grants,
        target: Grants,
        workspace_ids: Sequence[uuid.UUID],
    ) -> None:
        wanted = set(workspace_ids)
        found = set(
            (
                await connection.scalars(
                    select(Workspace.id).where(Workspace.org_id == org_id, Workspace.id.in_(wanted))
                )
            ).all()
        )
        if found != wanted:
            raise OrgError(404, "a workspace does not exist")
        have = {workspace for workspace, _ in target.rows if workspace is not None}
        if have == wanted:
            return
        user_id = target.user_id
        if have - wanted:
            await connection.execute(
                delete(Membership).where(
                    Membership.org_id == org_id,
                    Membership.user_id == user_id,
                    Membership.workspace_id.in_(have - wanted),
                )
            )
        for workspace_id in sorted(wanted - have):
            await connection.execute(
                insert(Membership).values(
                    org_id=org_id, user_id=user_id, workspace_id=workspace_id, role="member"
                )
            )
        await _audit(
            connection,
            org_id,
            actor.user_id,
            "member.workspaces_changed",
            "user",
            user_id,
            {
                "added": sorted(str(w) for w in wanted - have),
                "removed": sorted(str(w) for w in have - wanted),
            },
        )

    async def remove_member(
        self, auth_user_id: uuid.UUID, org_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        """Removes a member from the org, or the caller leaves it. The user row stays for the
        history of the org; it has no membership."""
        async with self._engine.begin() as connection:
            await _lock_org(connection, org_id)
            actor = await _member(connection, org_id, auth_user_id)
            leaving = actor.user_id == user_id
            if not leaving and not actor.org_admin:
                raise OrgError(403, "only an org admin can do this")
            target = actor if leaving else await load_grants(connection, org_id, user_id)
            if not target.org_member:
                raise OrgError(404, "the member does not exist")
            current = _role(target)
            if current == "owner" and not leaving and not actor.org_owner:
                raise OrgError(403, "only an owner can remove an owner")
            if current == "owner" and await _owner_count(connection, org_id) <= 1:
                raise OrgError(409, "the org needs at least one owner")
            await connection.execute(
                delete(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id)
            )
            await _audit(
                connection,
                org_id,
                actor.user_id,
                "member.left" if leaving else "member.removed",
                "user",
                user_id,
                {"role": current},
            )


async def _members(
    connection: AsyncConnection, org_id: uuid.UUID, user_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    query = (
        select(
            User.id,
            User.display_name,
            User.email,
            Membership.workspace_id,
            Membership.role,
            Membership.created_at,
        )
        .join(Membership, and_(Membership.org_id == User.org_id, Membership.user_id == User.id))
        .where(User.org_id == org_id)
        .order_by(User.display_name, User.id)
    )
    if user_id is not None:
        query = query.where(User.id == user_id)
    members: dict[uuid.UUID, dict[str, Any]] = {}
    workspaces: dict[uuid.UUID, list[str]] = {}
    for row in await connection.execute(query):
        workspaces.setdefault(row.id, [])
        if row.workspace_id is None:
            members[row.id] = {
                "user_id": str(row.id),
                "display_name": row.display_name,
                "email": row.email,
                "role": row.role,
                "joined_at": _iso(row.created_at),
            }
        else:
            workspaces[row.id].append(str(row.workspace_id))
    return [
        member | {"workspace_ids": sorted(workspaces[member_id])}
        for member_id, member in members.items()
    ]
