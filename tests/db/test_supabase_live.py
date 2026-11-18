"""Live tenancy test on the Supabase project (docs/architecture.md, Section 17.1).

It checks that a user of org A cannot read rows of org B through the Supabase Data API and
through SQL with row-level security. The project must have the migrations applied.

Run it with ``SAMA_LIVE_TESTS=1`` and these variables: ``SAMA_SUPABASE_URL``,
``SAMA_SUPABASE_ANON_KEY``, ``SAMA_SUPABASE_SERVICE_ROLE_KEY``, ``SAMA_DATABASE_URL``. It
creates two Supabase Auth users and two orgs, and deletes them at the end.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from sama.db import as_user, create_engine
from sama.db.models import Base
from tests.db.world import World, seed, unseed

REQUIRED = (
    "SAMA_SUPABASE_URL",
    "SAMA_SUPABASE_ANON_KEY",
    "SAMA_SUPABASE_SERVICE_ROLE_KEY",
    "SAMA_DATABASE_URL",
)
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("SAMA_LIVE_TESTS") != "1" or not all(map(os.environ.get, REQUIRED)),
        reason="set SAMA_LIVE_TESTS=1 and the SAMA_SUPABASE_* variables",
    ),
]
TABLES = sorted(Base.metadata.tables)
LIVE_USERS = ("alice", "bob")


@dataclass
class Live:
    world: World
    tokens: dict[str, str]
    url: str
    anon_key: str


def _call(method: str, url: str, headers: dict[str, str], body: Any = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = Request(  # noqa: S310 - the URL is the configured Supabase project.
        url, data=data, method=method, headers={"Content-Type": "application/json", **headers}
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except HTTPError as error:
        raw = error.read()
        return error.code, json.loads(raw) if raw else None


async def call(method: str, url: str, headers: dict[str, str], body: Any = None) -> tuple[int, Any]:
    return await asyncio.to_thread(_call, method, url, headers, body)


@pytest.fixture
async def live() -> AsyncIterator[Live]:
    base = os.environ["SAMA_SUPABASE_URL"].rstrip("/")
    anon = os.environ["SAMA_SUPABASE_ANON_KEY"]
    service = os.environ["SAMA_SUPABASE_SERVICE_ROLE_KEY"]
    database = os.environ["SAMA_DATABASE_URL"]
    admin = {"apikey": service, "Authorization": f"Bearer {service}"}
    auth: dict[str, uuid.UUID] = {}
    passwords: dict[str, str] = {}
    emails: dict[str, str] = {}
    world: World | None = None
    try:
        for name in LIVE_USERS:
            passwords[name] = secrets.token_urlsafe(24)
            emails[name] = email = f"sama-live-{name}-{uuid.uuid4().hex[:8]}@example.com"
            status, user = await call(
                "POST",
                f"{base}/auth/v1/admin/users",
                admin,
                {"email": email, "password": passwords[name], "email_confirm": True},
            )
            assert status == 200, status
            auth[name] = uuid.UUID(user["id"])
        world = await seed(database, auth)
        tokens: dict[str, str] = {}
        for name in LIVE_USERS:
            status, session = await call(
                "POST",
                f"{base}/auth/v1/token?grant_type=password",
                {"apikey": anon},
                {"email": emails[name], "password": passwords[name]},
            )
            assert status == 200, status
            tokens[name] = session["access_token"]
        yield Live(world=world, tokens=tokens, url=base, anon_key=anon)
    finally:
        if world is not None:
            await unseed(database, world)
        for user_id in auth.values():
            await call("DELETE", f"{base}/auth/v1/admin/users/{user_id}", admin)


async def rest(live: Live, table: str, token: str | None) -> tuple[int, Any]:
    headers = {"apikey": live.anon_key}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    column = "id" if table == "orgs" else "org_id"
    return await call("GET", f"{live.url}/rest/v1/{table}?select={column}", headers)


async def test_the_data_api_shows_each_user_only_their_org(live: Live) -> None:
    ids = live.world.ids
    for user, own, other in (("alice", "org-a", "org-b"), ("bob", "org-b", "org-a")):
        for table in TABLES:
            status, rows = await rest(live, table, live.tokens[user])
            assert status == 200, (user, table, status)
            seen = {uuid.UUID(next(iter(row.values()))) for row in rows}
            assert ids[other] not in seen, (user, table)
            if table != "audit_log":  # Only org admins read the audit log.
                assert ids[own] in seen, (user, table)


async def test_the_data_api_gives_anon_nothing(live: Live) -> None:
    for table in TABLES:
        status, _ = await rest(live, table, None)
        assert status in {401, 403}, (table, status)


async def test_the_data_api_rejects_writes(live: Live) -> None:
    headers = {"apikey": live.anon_key, "Authorization": f"Bearer {live.tokens['alice']}"}
    status, _ = await call(
        "PATCH",
        f"{live.url}/rest/v1/sessions?org_id=eq.{live.world.ids['org-a']}",
        headers,
        {"title": "changed"},
    )
    assert status in {401, 403}, status


async def test_rls_in_sql_shows_each_user_only_their_org(live: Live) -> None:
    engine = create_engine(os.environ["SAMA_DATABASE_URL"], poolclass=NullPool)
    try:
        for user, other in (("alice", "org-b"), ("bob", "org-a")):
            async with as_user(engine, live.world.auth[user]) as connection:
                for table in TABLES:
                    column = "id" if table == "orgs" else "org_id"
                    query = text(f"SELECT count(*) FROM {table} WHERE {column} = :org")  # noqa: S608
                    rows = await connection.execute(query, {"org": live.world.ids[other]})
                    assert rows.scalar_one() == 0, (user, table)
    finally:
        await engine.dispose()
