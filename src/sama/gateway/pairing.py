"""Pairing codes for chat channels (docs/architecture.md, Section 11.3).

A user makes a code on the web page. The user then sends the code to the bot:

- A ``user`` code links the chat account of the sender to the user. A direct chat then uses
  the workspace and the agent profile of the code.
- A ``group`` code links a group chat to the workspace and the agent profile of the code. Only
  the linked chat account of the user who made the code can use it.

Codes are single-use and expire after 10 minutes. Only their SHA-256 digest is stored, and
they are never logged. Five wrong codes from one chat account lock it for one hour.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from sama.db.models import (
    AgentProfile,
    AuditEntry,
    ChannelGroup,
    ChannelIdentity,
    PairingCode,
    User,
)
from sama.tenancy.access import load_grants

CODE_ALPHABET: Final = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
"""No ``0``, ``O``, ``1``, or ``I``, so that a person can read the code without errors."""
CODE_LENGTH: Final = 8
CODE_TTL: Final = timedelta(minutes=10)
MAX_OPEN_CODES: Final = 5
"""How many unused codes that have not expired one user can have."""
MAX_FAILURES: Final = 5
LOCKOUT_S: Final = 3600.0
GLOBAL_FAILURES_PER_MINUTE: Final = 60
"""Wrong codes from all chat accounts together. More are refused for the rest of the minute."""

Kind = Literal["user", "group"]
_CODE: Final = re.compile(f"[{CODE_ALPHABET}]{{{CODE_LENGTH}}}")


class PairingError(Exception):
    """The code cannot be made or used. The message is safe to show to the user."""


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize(text: str) -> str | None:
    """The code in ``text``, or None. Spaces and dashes are ignored, and case does not matter."""
    code = re.sub(r"[\s-]", "", text).upper()
    return code if _CODE.fullmatch(code) else None


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Linked:
    """The result of a used code."""

    kind: Kind
    org_id: uuid.UUID
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    agent_profile_id: uuid.UUID


async def create_code(
    connection: AsyncConnection,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    channel: str,
    kind: Kind,
    workspace_id: uuid.UUID,
    agent_profile_id: uuid.UUID,
) -> tuple[str, datetime]:
    """Stores a new code and returns it with its expiry. The caller checks the access first.

    Run it as the table owner. Raises ``PairingError`` when the user has too many open codes.
    """
    # Lock the user row, so that two requests at the same time cannot pass the cap together.
    await connection.execute(
        select(User.id).where(User.org_id == org_id, User.id == user_id).with_for_update()
    )
    now = (await connection.execute(select(func.now()))).scalar_one()
    await connection.execute(
        delete(PairingCode).where(
            PairingCode.org_id == org_id,
            PairingCode.user_id == user_id,
            PairingCode.expires_at < now,
        )
    )
    open_codes = (
        await connection.execute(
            select(func.count())
            .select_from(PairingCode)
            .where(
                PairingCode.org_id == org_id,
                PairingCode.user_id == user_id,
                PairingCode.used_at.is_(None),
            )
        )
    ).scalar_one()
    if open_codes >= MAX_OPEN_CODES:
        raise PairingError("too many open pairing codes. Use one or wait 10 minutes.")
    code = new_code()
    expires_at: datetime = now + CODE_TTL
    await connection.execute(
        insert(PairingCode).values(
            org_id=org_id,
            user_id=user_id,
            channel=channel,
            kind=kind,
            code_hash=code_hash(code),
            workspace_id=workspace_id,
            agent_profile_id=agent_profile_id,
            expires_at=expires_at,
        )
    )
    return code, expires_at


async def redeem(
    connection: AsyncConnection,
    *,
    channel: str,
    code: str,
    kind: Kind,
    external_id: str,
    sender_user_id: uuid.UUID | None = None,
) -> Linked | None:
    """Uses a code. Returns None for a wrong, used, or expired code. Run it as the table owner.

    ``external_id`` is the chat account for a ``user`` code and the group chat for a ``group``
    code. A ``group`` code needs ``sender_user_id``: the user of the linked sender account.
    The code is used up only when the link is made.
    """
    row = (
        await connection.execute(
            select(PairingCode)
            .where(
                PairingCode.code_hash == code_hash(code),
                PairingCode.channel == channel,
                PairingCode.kind == kind,
                PairingCode.used_at.is_(None),
                PairingCode.expires_at > func.now(),
            )
            .with_for_update()
        )
    ).one_or_none()
    if row is None or (kind == "group" and row.user_id != sender_user_id):
        return None
    # The user can lose the workspace or the profile after the code was made.
    grants = await load_grants(connection, row.org_id, row.user_id)
    profile = (
        await connection.execute(
            select(AgentProfile.workspace_id).where(
                AgentProfile.org_id == row.org_id, AgentProfile.id == row.agent_profile_id
            )
        )
    ).one_or_none()
    if (
        not grants.sees_workspace(row.workspace_id)
        or profile is None
        or profile[0] not in (None, row.workspace_id)
    ):
        return None
    await connection.execute(
        update(PairingCode).where(PairingCode.id == row.id).values(used_at=func.now())
    )
    link = Linked(kind, row.org_id, row.user_id, row.workspace_id, row.agent_profile_id)
    table: type[ChannelIdentity | ChannelGroup]
    if kind == "user":
        table, target, owner = ChannelIdentity, "channel_identity", "user_id"
    else:
        table, target, owner = ChannelGroup, "channel_group", "created_by_user_id"
    changed = {
        "org_id": link.org_id,
        "workspace_id": link.workspace_id,
        "agent_profile_id": link.agent_profile_id,
        owner: link.user_id,
    }
    statement = pg_insert(table).values(channel=channel, external_id=external_id, **changed)
    await connection.execute(
        statement.on_conflict_do_update(
            index_elements=["channel", "external_id"],
            set_={
                **{name: statement.excluded[name] for name in changed},
                "updated_at": func.now(),
            },
        )
    )
    await connection.execute(
        insert(AuditEntry).values(
            org_id=link.org_id,
            workspace_id=link.workspace_id,
            actor_kind="user",
            actor_user_id=link.user_id,
            action=f"{target}.linked",
            target_type=target,
            target_id=f"{channel}:{external_id}",
            details={"channel": channel, "agent_profile_id": str(link.agent_profile_id)},
        )
    )
    return link


class Lockout:
    """Counts wrong codes in this process (docs/architecture.md, Section 11.3).

    Five wrong codes from one chat account lock it for one hour. All chat accounts together
    get at most ``GLOBAL_FAILURES_PER_MINUTE`` wrong codes in 60 seconds, so many accounts
    cannot guess together.
    """

    def __init__(
        self,
        *,
        max_failures: int = MAX_FAILURES,
        lockout_s: float = LOCKOUT_S,
        global_per_minute: int = GLOBAL_FAILURES_PER_MINUTE,
        max_keys: int = 10_000,
    ) -> None:
        self._max_failures = max_failures
        self._lockout_s = lockout_s
        self._global_per_minute = global_per_minute
        self._max_keys = max_keys
        self._failures: OrderedDict[str, tuple[int, float]] = OrderedDict()
        """``key -> (failures, time of the first failure)``."""
        self._recent: deque[float] = deque()

    def locked(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._recent) >= self._global_per_minute:
            return True
        failures, first = self._failures.get(key, (0, now))
        if now - first > self._lockout_s:
            self._failures.pop(key, None)
            return False
        return failures >= self._max_failures

    def attempt(self, key: str, now: float | None = None) -> bool:
        """Counts one try as a wrong code before the check, or returns False when locked.

        Call ``succeeded`` when the code is right. The check and the count have no ``await``
        between them, so tries at the same time cannot pass the limit.
        """
        if self.locked(key, now):
            return False
        self.failed(key, now)
        return True

    def failed(self, key: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._recent.append(now)
        failures, first = self._failures.pop(key, (0, now))
        if now - first > self._lockout_s:
            failures, first = 0, now
        failures += 1
        # The lock lasts one hour from the last failure that sets it.
        self._failures[key] = (failures, now if failures >= self._max_failures else first)
        while len(self._failures) > self._max_keys:
            self._failures.popitem(last=False)

    def succeeded(self, key: str) -> None:
        self._failures.pop(key, None)
