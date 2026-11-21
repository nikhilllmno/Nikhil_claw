"""Org API keys (docs/architecture.md, Section 10.7, M7.6).

An org admin makes a key for one workspace. The key acts as that admin in the workspace. A
check at each request reads the memberships again: when the admin leaves the org or loses the
admin role, the key stops. The key goes to the admin one time. Sama keeps only its SHA-256
hash.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import (
    API_KEY_APPROVALS,
    API_KEY_SCOPES,
    AgentProfile,
    ApiKey,
    AuditEntry,
    Org,
    User,
    Workspace,
)
from sama.tenancy.access import load_grants, lock_grants
from sama.tenancy.regions import check_here

Scope = Literal["chat", "mcp"]
Approvals = Literal["deny", "pending"]

KEY_PREFIX: Final = "sama_sk_"
"""Every key starts with this text. A secret scanner can find a key that leaked."""
SECRET_BYTES: Final = 32
SHOWN_CHARS: Final = 12
"""The first characters of a key that the list shows (``sama_sk_`` and four more)."""
MAX_KEY_CHARS: Final = 100
NAME_MAX_CHARS: Final = 80
MAX_ACTIVE_KEYS: Final = 50
"""The most keys that an org has that are not revoked and not expired."""
MAX_EXPIRY_DAYS: Final = 365
TOUCH_INTERVAL: Final = timedelta(minutes=1)
"""``last_used_at`` changes at most once in this time, so a busy key does not write a row for
each request."""
_SHAPE: Final = re.compile(r"^sama_sk_[A-Za-z0-9_-]{20,90}$")

assert set(API_KEY_SCOPES) == {"chat", "mcp"}  # noqa: S101 - the Literal must match
assert set(API_KEY_APPROVALS) == {"deny", "pending"}  # noqa: S101


class ApiKeyError(Exception):
    """A key request that sama refuses. ``status`` is the HTTP status. The text is safe to
    show."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class InvalidApiKeyError(Exception):
    """The request has no valid API key. The text is safe to show. The answer is ``401``."""


def hash_key(key: str) -> bytes:
    return hashlib.sha256(key.encode()).digest()


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(SECRET_BYTES)


def looks_like_a_key(token: str) -> bool:
    """True for a text with the shape of a sama API key. A Supabase access token never has
    it."""
    return bool(_SHAPE.match(token))


@dataclass(frozen=True, slots=True)
class KeyContext:
    """The key of a request after the checks. Each request makes a new one."""

    key_id: uuid.UUID
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    auth_user_id: uuid.UUID
    agent_profile_id: uuid.UUID | None
    scopes: frozenset[str]
    approvals: str
    name: str

    @property
    def unattended(self) -> bool:
        """True when an ``ask`` tool call in a run of the key gets ``deny``."""
        return self.approvals == "deny"


def _view(row: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    if row.revoked_at is not None:
        status = "revoked"
    elif row.expires_at is not None and row.expires_at <= now:
        status = "expired"
    else:
        status = "active"
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "workspace_id": row.workspace_id,
        "workspace_name": row.workspace_name,
        "agent_profile_id": row.agent_profile_id,
        "agent_profile_name": row.agent_profile_name,
        "scopes": sorted(row.scopes),
        "approvals": row.approvals,
        "user_id": row.user_id,
        "user_name": row.user_name,
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "last_used_at": row.last_used_at,
        "revoked_at": row.revoked_at,
        "status": status,
    }


def _active() -> Any:
    return and_(
        ApiKey.revoked_at.is_(None),
        or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > func.now()),
    )


class ApiKeyStore:
    """Makes, lists, revokes, and checks the API keys of the orgs."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    # -- admin ------------------------------------------------------------------------------

    async def list(self, org_id: uuid.UUID) -> list[dict[str, Any]]:
        """Every key of the org, newest first, without the hash."""
        async with self._engine.connect() as connection:
            rows = (await connection.execute(self._listed().where(ApiKey.org_id == org_id))).all()
        return [_view(row) for row in rows]

    async def create(
        self,
        org_id: uuid.UUID,
        *,
        actor_user_id: uuid.UUID,
        name: str,
        workspace_id: uuid.UUID,
        agent_profile_id: uuid.UUID | None,
        scopes: Sequence[Scope],
        approvals: Approvals,
        expires_in_days: int | None,
    ) -> tuple[dict[str, Any], str]:
        """Makes a key that acts as ``actor_user_id``. Returns the key row and the key. The
        caller checked that the actor is an org admin; this call checks it again with a lock,
        so a demotion that waits cannot slip in."""
        name = name.strip()
        if not 1 <= len(name) <= NAME_MAX_CHARS:
            raise ApiKeyError(f"the name must have 1 to {NAME_MAX_CHARS} characters", 422)
        wanted = sorted(set(scopes))
        if not wanted or not set(wanted) <= set(API_KEY_SCOPES):
            raise ApiKeyError("give the scopes chat, mcp, or both", 422)
        if approvals not in API_KEY_APPROVALS:
            raise ApiKeyError("approvals must be deny or pending", 422)
        if expires_in_days is not None and not 1 <= expires_in_days <= MAX_EXPIRY_DAYS:
            raise ApiKeyError(f"a key expires after 1 to {MAX_EXPIRY_DAYS} days", 422)
        key = new_key()
        async with self._engine.begin() as connection:
            # One key write at a time in an org, so the cap holds.
            await connection.execute(
                select(Org.id).where(Org.id == org_id).with_for_update(key_share=True)
            )
            grants = await lock_grants(connection, org_id, actor_user_id)
            if not grants.org_admin:
                raise ApiKeyError("only an org owner or admin can do this", 403)
            await self._check_target(connection, org_id, workspace_id, agent_profile_id)
            active = (
                await connection.execute(
                    select(func.count())
                    .select_from(ApiKey)
                    .where(ApiKey.org_id == org_id, _active())
                )
            ).scalar_one()
            if active >= MAX_ACTIVE_KEYS:
                raise ApiKeyError(
                    f"the org has {MAX_ACTIVE_KEYS} active keys; revoke one first", 409
                )
            expires_at = (
                None
                if expires_in_days is None
                else datetime.now(UTC) + timedelta(days=expires_in_days)
            )
            key_id = (
                await connection.execute(
                    insert(ApiKey)
                    .values(
                        org_id=org_id,
                        user_id=actor_user_id,
                        workspace_id=workspace_id,
                        agent_profile_id=agent_profile_id,
                        name=name,
                        prefix=key[:SHOWN_CHARS],
                        key_hash=hash_key(key),
                        scopes=wanted,
                        approvals=approvals,
                        expires_at=expires_at,
                    )
                    .returning(ApiKey.id)
                )
            ).scalar_one()
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=org_id,
                    workspace_id=workspace_id,
                    actor_kind="user",
                    actor_user_id=actor_user_id,
                    action="api_key.created",
                    target_type="api_key",
                    target_id=str(key_id),
                    details={
                        "name": name,
                        "scopes": wanted,
                        "approvals": approvals,
                        "agent_profile_id": None
                        if agent_profile_id is None
                        else str(agent_profile_id),
                        "expires_in_days": expires_in_days,
                    },
                )
            )
            row = (
                await connection.execute(
                    self._listed().where(ApiKey.org_id == org_id, ApiKey.id == key_id)
                )
            ).one()
        return _view(row), key

    async def revoke(
        self, org_id: uuid.UUID, key_id: uuid.UUID, *, actor_user_id: uuid.UUID
    ) -> None:
        """Revokes a key at once: the next request with it gets ``401``. A revoked key stays
        revoked; a second call changes nothing."""
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    select(ApiKey.id, ApiKey.workspace_id, ApiKey.revoked_at)
                    .where(ApiKey.org_id == org_id, ApiKey.id == key_id)
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                raise ApiKeyError("the key does not exist", 404)
            grants = await lock_grants(connection, org_id, actor_user_id)
            if not grants.org_admin:
                raise ApiKeyError("only an org owner or admin can do this", 403)
            if row.revoked_at is not None:
                return
            await connection.execute(
                update(ApiKey)
                .where(ApiKey.org_id == org_id, ApiKey.id == key_id)
                .values(revoked_at=func.now(), revoked_by_user_id=actor_user_id)
            )
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=org_id,
                    workspace_id=row.workspace_id,
                    actor_kind="user",
                    actor_user_id=actor_user_id,
                    action="api_key.revoked",
                    target_type="api_key",
                    target_id=str(key_id),
                    details={},
                )
            )

    # -- requests ---------------------------------------------------------------------------

    async def verify(self, token: str, scope: Scope) -> KeyContext:
        """The key of a request, after these checks: the key exists, is not revoked or
        expired, has ``scope``, and its user is still an org owner or admin.
        The region of the org must be served here. Raises ``InvalidApiKeyError`` with one text
        for each reason that does not tell an attacker whether a key exists."""
        if not looks_like_a_key(token) or len(token) > MAX_KEY_CHARS:
            raise InvalidApiKeyError("the API key is not valid")
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    select(
                        ApiKey.id,
                        ApiKey.org_id,
                        ApiKey.workspace_id,
                        ApiKey.user_id,
                        ApiKey.agent_profile_id,
                        ApiKey.scopes,
                        ApiKey.approvals,
                        ApiKey.name,
                        ApiKey.revoked_at,
                        ApiKey.expires_at,
                        ApiKey.last_used_at,
                        User.auth_user_id,
                        Org.region,
                    )
                    .join(User, and_(User.org_id == ApiKey.org_id, User.id == ApiKey.user_id))
                    .join(Org, Org.id == ApiKey.org_id)
                    .where(ApiKey.key_hash == hash_key(token))
                )
            ).one_or_none()
            now = datetime.now(UTC)
            if (
                row is None
                or row.revoked_at is not None
                or (row.expires_at is not None and row.expires_at <= now)
                or row.auth_user_id is None
            ):
                raise InvalidApiKeyError("the API key is not valid")
            check_here(row.region)
            grants = await load_grants(connection, row.org_id, row.user_id)
            if not grants.org_admin:
                # The user left the org or is no longer an org admin: the key stops.
                raise InvalidApiKeyError("the API key is not valid")
            if scope not in row.scopes:
                raise InvalidApiKeyError(f"the API key does not have the scope {scope}")
            if row.last_used_at is None or now - row.last_used_at >= TOUCH_INTERVAL:
                await connection.execute(
                    update(ApiKey).where(ApiKey.id == row.id).values(last_used_at=func.now())
                )
        return KeyContext(
            key_id=row.id,
            org_id=row.org_id,
            workspace_id=row.workspace_id,
            user_id=row.user_id,
            auth_user_id=row.auth_user_id,
            agent_profile_id=row.agent_profile_id,
            scopes=frozenset(row.scopes),
            approvals=row.approvals,
            name=row.name,
        )

    # -- helpers ----------------------------------------------------------------------------

    @staticmethod
    def _listed() -> Any:
        return (
            select(
                ApiKey.id,
                ApiKey.name,
                ApiKey.prefix,
                ApiKey.workspace_id,
                Workspace.name.label("workspace_name"),
                ApiKey.agent_profile_id,
                AgentProfile.name.label("agent_profile_name"),
                ApiKey.scopes,
                ApiKey.approvals,
                ApiKey.user_id,
                User.display_name.label("user_name"),
                ApiKey.created_at,
                ApiKey.expires_at,
                ApiKey.last_used_at,
                ApiKey.revoked_at,
            )
            .join(
                Workspace,
                and_(Workspace.org_id == ApiKey.org_id, Workspace.id == ApiKey.workspace_id),
            )
            .join(User, and_(User.org_id == ApiKey.org_id, User.id == ApiKey.user_id))
            .outerjoin(
                AgentProfile,
                and_(
                    AgentProfile.org_id == ApiKey.org_id,
                    AgentProfile.id == ApiKey.agent_profile_id,
                ),
            )
            .order_by(ApiKey.created_at.desc(), ApiKey.id)
        )

    @staticmethod
    async def _check_target(
        connection: AsyncConnection,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID,
        agent_profile_id: uuid.UUID | None,
    ) -> None:
        found = (
            await connection.execute(
                select(Workspace.id).where(Workspace.org_id == org_id, Workspace.id == workspace_id)
            )
        ).scalar_one_or_none()
        if found is None:
            raise ApiKeyError("the workspace does not exist", 422)
        if agent_profile_id is None:
            return
        profile_workspace = (
            await connection.execute(
                select(AgentProfile.workspace_id).where(
                    AgentProfile.org_id == org_id, AgentProfile.id == agent_profile_id
                )
            )
        ).one_or_none()
        if profile_workspace is None or profile_workspace[0] not in (None, workspace_id):
            raise ApiKeyError("the agent profile does not exist in this workspace", 422)
