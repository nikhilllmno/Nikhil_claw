"""Migration 0031 and the email check of sign-up (docs/architecture.md, Section 17.2)."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.orgs import AuthUsersEmails
from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import World, seed


async def _fetch(url: str, query: str, *args: object) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


def _columns(url: str, table: str) -> set[str]:
    rows = asyncio.run(
        _fetch(
            url,
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public'"
            " AND table_name = $1",
            table,
        )
    )
    return {row["column_name"] for row in rows}


def _policies(url: str, table: str) -> list[str]:
    rows = asyncio.run(
        _fetch(url, "SELECT policyname FROM pg_policies WHERE tablename = $1", table)
    )
    return sorted(row["policyname"] for row in rows)


def test_the_upgrade_adds_the_invites_and_the_downgrade_removes_them() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        assert "token_hash" in _columns(url, "org_invites")
        assert "created_by_auth_user_id" in _columns(url, "orgs")
        assert _policies(url, "org_invites") == ["org_invites_select"]
        config = alembic_config(url)
        command.downgrade(config, "0030")
        assert _columns(url, "org_invites") == set()
        assert "created_by_auth_user_id" not in _columns(url, "orgs")
        command.upgrade(config, "head")
        assert _policies(url, "org_invites") == ["org_invites_select"]


@pytest.mark.parametrize(
    ("email", "role", "size", "ends"),
    [
        ("Bob@example.test", "member", 32, "NULL, NULL, NULL"),
        ("bob@example.test", "owner", 32, "NULL, NULL, NULL"),
        ("bob@example.test", "member", 5, "NULL, NULL, NULL"),
        ("bob@example.test", "member", 32, "NULL, now(), NULL"),
        ("bob@example.test", "member", 32, "now(), now(), :carol"),
    ],
)
async def test_the_invite_checks_hold(
    engine: AsyncEngine, world: World, email: str, role: str, size: int, ends: str
) -> None:
    """A lowercase email, no invite to owner, a 32-byte hash, and at most one end."""
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO org_invites (org_id, email, role, token_hash,"  # noqa: S608
                    " invited_by_user_id, expires_at, revoked_at, accepted_at, accepted_user_id)"
                    " VALUES (:org, :email, :role, :hash, :carol, now() + interval '1 day',"
                    f" {ends})"
                ),
                {
                    "org": world.ids["org-a"],
                    "email": email,
                    "role": role,
                    "hash": os.urandom(size),
                    "carol": world.ids["carol"],
                },
            )


@pytest.fixture
async def auth_users(engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """A small ``auth.users`` like the one of Supabase. The shim does not have it."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE auth.users (id uuid PRIMARY KEY, email text,"
                " email_confirmed_at timestamptz, deleted_at timestamptz)"
            )
        )
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DROP TABLE auth.users"))


async def test_the_email_check_needs_a_confirmed_email(engine: AsyncEngine) -> None:
    emails = AuthUsersEmails(engine)
    # Without auth.users, sign-up fails closed.
    assert await emails.verified_email(uuid.uuid4()) is None


async def test_the_email_check_reads_auth_users(auth_users: AsyncEngine) -> None:
    ids = {name: uuid.uuid4() for name in ("ok", "unconfirmed", "deleted", "no_email")}
    async with auth_users.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO auth.users (id, email, email_confirmed_at, deleted_at) VALUES"
                " (:ok, ' Ana@Example.TEST ', now(), NULL),"
                " (:unconfirmed, 'b@example.test', NULL, NULL),"
                " (:deleted, 'c@example.test', now(), now()),"
                " (:no_email, NULL, now(), NULL)"
            ),
            ids,
        )
    emails = AuthUsersEmails(auth_users)
    assert await emails.verified_email(ids["ok"]) == "ana@example.test"
    for name in ("unconfirmed", "deleted", "no_email"):
        assert await emails.verified_email(ids[name]) is None, name
    assert await emails.verified_email(uuid.uuid4()) is None
