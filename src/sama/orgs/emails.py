"""The email checks of sign-up (docs/architecture.md, Section 17.2).

Org creation and invites need the verified email of the user. The ``email`` claim of the access
token does not prove that the user owns the address: Supabase can issue a token before the user
confirms the email, and ``user_metadata`` is editable by the user. So sama reads
``auth.users.email_confirmed_at`` with the owner connection. A database without ``auth.users``
(a test database) has no verified emails: sign-up fails closed.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from typing import Final, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

MAX_EMAIL_CHARS: Final = 320
_EMAIL: Final = re.compile(r"[^@\s]{1,64}@[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\.[a-z]{2,63}")
"""A plain address: one ``@``, a host name with a dot, and no spaces."""

DISPOSABLE_DOMAINS: Final = frozenset(
    {
        "10minutemail.com",
        "discard.email",
        "dispostable.com",
        "emailondeck.com",
        "fakeinbox.com",
        "getnada.com",
        "guerrillamail.com",
        "guerrillamail.net",
        "maildrop.cc",
        "mailinator.com",
        "mailnesia.com",
        "mintemail.com",
        "mohmal.com",
        "sharklasers.com",
        "spamgourmet.com",
        "temp-mail.org",
        "tempmail.com",
        "tempmailo.com",
        "throwawaymail.com",
        "trashmail.com",
        "yopmail.com",
    }
)
"""Well-known disposable email domains. ``SAMA_SIGNUP_BLOCKED_DOMAINS`` adds more."""


def normalize_email(value: str) -> str | None:
    """The address in lower case without spaces at the ends, or ``None`` if it is not a plain
    address."""
    email = value.strip().lower()
    if len(email) > MAX_EMAIL_CHARS or not _EMAIL.fullmatch(email):
        return None
    return email


def domain_of(email: str) -> str:
    return email.rpartition("@")[2]


def blocked_domain(email: str, blocked: Iterable[str]) -> bool:
    """True if the domain of ``email``, or a parent domain, is in ``blocked``."""
    names = set(blocked)
    labels = domain_of(email).split(".")
    return any(".".join(labels[i:]) in names for i in range(len(labels) - 1))


class EmailCheck(Protocol):
    async def verified_email(self, auth_user_id: uuid.UUID) -> str | None:
        """The confirmed email of the Supabase Auth user in lower case, or ``None``."""
        ...


class AuthUsersEmails:
    """Reads ``auth.users`` of the Supabase project with the owner connection."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def verified_email(self, auth_user_id: uuid.UUID) -> str | None:
        async with self._engine.connect() as connection:
            if not await connection.scalar(text("SELECT to_regclass('auth.users') IS NOT NULL")):
                return None
            row = (
                await connection.execute(
                    text(
                        "SELECT email, email_confirmed_at FROM auth.users"
                        " WHERE id = :id AND deleted_at IS NULL"
                    ),
                    {"id": auth_user_id},
                )
            ).one_or_none()
        if row is None or row.email is None or row.email_confirmed_at is None:
            return None
        return normalize_email(str(row.email))
