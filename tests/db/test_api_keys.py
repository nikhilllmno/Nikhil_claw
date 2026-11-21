"""Org API keys in Postgres: the store, RLS, and the data controls (docs/architecture.md,
Sections 10.7 and 17.4; roadmap M7.6)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from alembic import command
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.apikeys import store as store_module
from sama.apikeys.store import (
    KEY_PREFIX,
    ApiKeyError,
    ApiKeyStore,
    InvalidApiKeyError,
)
from sama.datacontrols.service import DataControls
from sama.db.engine import as_user
from sama.db.models import ApiKey, AuditEntry, Membership
from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import World, seed


async def _make(
    store: ApiKeyStore, world: World, actor: str = "carol", **options: Any
) -> tuple[dict[str, Any], str]:
    values: dict[str, Any] = {
        "name": "ci",
        "workspace_id": world.ids["a1"],
        "agent_profile_id": None,
        "scopes": ["chat", "mcp"],
        "approvals": "deny",
        "expires_in_days": None,
    } | options
    return await store.create(world.ids["org-a"], actor_user_id=world.ids[actor], **values)


async def test_a_key_is_shown_once_and_only_its_hash_is_kept(
    engine: AsyncEngine, fresh: World
) -> None:
    store = ApiKeyStore(engine)
    view, key = await _make(store, fresh, name="  deploy bot  ")
    assert key.startswith(KEY_PREFIX)
    assert view["prefix"] == key[:12]
    assert view["name"] == "deploy bot"
    assert view["status"] == "active"
    assert view["scopes"] == ["chat", "mcp"]
    assert "key" not in view
    assert "key_hash" not in view
    async with engine.connect() as connection:
        stored = (
            await connection.execute(select(ApiKey.key_hash).where(ApiKey.id == view["id"]))
        ).scalar_one()
        audit = (
            await connection.execute(
                select(AuditEntry.action, AuditEntry.details).where(
                    AuditEntry.target_id == str(view["id"])
                )
            )
        ).one()
    assert stored == hashlib.sha256(key.encode()).digest()
    assert audit.action == "api_key.created"
    assert key not in json.dumps(audit.details)
    listed = await store.list(fresh.ids["org-a"])
    # The newest key first; the seeded key of the world comes after it.
    assert listed[0]["id"] == view["id"]
    assert all("key_hash" not in row for row in listed)


async def test_verify_gives_the_key_context(engine: AsyncEngine, fresh: World) -> None:
    store = ApiKeyStore(engine)
    view, key = await _make(store, fresh, scopes=["mcp"], approvals="pending")
    context = await store.verify(key, "mcp")
    assert context.key_id == view["id"]
    assert context.user_id == fresh.ids["carol"]
    assert context.auth_user_id == fresh.auth["carol"]
    assert context.workspace_id == fresh.ids["a1"]
    assert not context.unattended
    with pytest.raises(InvalidApiKeyError, match="scope chat"):
        await store.verify(key, "chat")
    for bad in ("", "nope", key + "x", KEY_PREFIX + "a" * 43, key.upper()):
        with pytest.raises(InvalidApiKeyError):
            await store.verify(bad, "mcp")
    async with engine.connect() as connection:
        used = (
            await connection.execute(select(ApiKey.last_used_at).where(ApiKey.id == view["id"]))
        ).scalar_one()
    assert used is not None


async def test_only_an_org_admin_makes_or_revokes_a_key(engine: AsyncEngine, fresh: World) -> None:
    store = ApiKeyStore(engine)
    with pytest.raises(ApiKeyError) as error:
        await _make(store, fresh, actor="alice")
    assert error.value.status == 403
    view, _ = await _make(store, fresh)
    with pytest.raises(ApiKeyError) as error:
        await store.revoke(fresh.ids["org-a"], view["id"], actor_user_id=fresh.ids["alice"])
    assert error.value.status == 403
    with pytest.raises(ApiKeyError) as error:
        await store.revoke(fresh.ids["org-a"], uuid.uuid4(), actor_user_id=fresh.ids["carol"])
    assert error.value.status == 404


@pytest.mark.parametrize(
    ("options", "status"),
    [
        ({"name": " "}, 422),
        ({"scopes": []}, 422),
        ({"scopes": ["admin"]}, 422),
        ({"approvals": "allow"}, 422),
        ({"expires_in_days": 0}, 422),
        ({"workspace_id": uuid.uuid4()}, 422),
    ],
)
async def test_a_bad_key_request_is_refused(
    engine: AsyncEngine, fresh: World, options: dict[str, Any], status: int
) -> None:
    with pytest.raises(ApiKeyError) as error:
        await _make(ApiKeyStore(engine), fresh, **options)
    assert error.value.status == status


async def test_a_profile_must_be_in_the_workspace_of_the_key(
    engine: AsyncEngine, fresh: World
) -> None:
    store = ApiKeyStore(engine)
    # ``profile-a2`` belongs to the workspace a2.
    with pytest.raises(ApiKeyError) as error:
        await _make(store, fresh, agent_profile_id=fresh.ids["profile-a2"])
    assert error.value.status == 422
    view, _ = await _make(store, fresh, agent_profile_id=fresh.ids["profile-a"])
    assert view["agent_profile_name"] == "profile-a"
    # A profile of another org is never found.
    with pytest.raises(ApiKeyError):
        await _make(store, fresh, agent_profile_id=fresh.ids["profile-b"])


async def test_a_revoked_or_expired_key_stops(engine: AsyncEngine, fresh: World) -> None:
    store = ApiKeyStore(engine)
    view, key = await _make(store, fresh)
    await store.revoke(fresh.ids["org-a"], view["id"], actor_user_id=fresh.ids["carol"])
    await store.revoke(fresh.ids["org-a"], view["id"], actor_user_id=fresh.ids["carol"])
    with pytest.raises(InvalidApiKeyError):
        await store.verify(key, "chat")
    statuses = {row["id"]: row["status"] for row in await store.list(fresh.ids["org-a"])}
    assert statuses[view["id"]] == "revoked"
    async with engine.connect() as connection:
        actions = (
            await connection.execute(
                select(AuditEntry.action).where(AuditEntry.target_id == str(view["id"]))
            )
        ).scalars()
        assert sorted(actions) == ["api_key.created", "api_key.revoked"]

    later, other = await _make(store, fresh, expires_in_days=1)
    assert await store.verify(other, "chat")
    async with engine.begin() as connection:
        await connection.execute(
            update(ApiKey)
            .where(ApiKey.id == later["id"])
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    with pytest.raises(InvalidApiKeyError):
        await store.verify(other, "chat")
    statuses = {row["id"]: row["status"] for row in await store.list(fresh.ids["org-a"])}
    assert statuses[later["id"]] == "expired"


async def test_a_key_stops_when_its_user_is_no_longer_an_org_admin(
    engine: AsyncEngine, fresh: World
) -> None:
    store = ApiKeyStore(engine)
    _, key = await _make(store, fresh)
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership)
            .where(Membership.user_id == fresh.ids["carol"], Membership.workspace_id.is_(None))
            .values(role="member")
        )
    with pytest.raises(InvalidApiKeyError):
        await store.verify(key, "chat")


async def test_an_org_has_at_most_the_cap_of_active_keys(
    engine: AsyncEngine, fresh: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The world has one active key in org A already.
    monkeypatch.setattr(store_module, "MAX_ACTIVE_KEYS", 3)
    store = ApiKeyStore(engine)
    first, _ = await _make(store, fresh)
    await _make(store, fresh)
    with pytest.raises(ApiKeyError) as error:
        await _make(store, fresh)
    assert error.value.status == 409
    await store.revoke(fresh.ids["org-a"], first["id"], actor_user_id=fresh.ids["carol"])
    await _make(store, fresh)


async def test_only_org_admins_read_the_keys_through_the_data_api(
    engine: AsyncEngine, fresh: World
) -> None:
    store = ApiKeyStore(engine)
    view, _ = await _make(store, fresh)

    async def seen(user: str) -> set[uuid.UUID]:
        async with as_user(engine, fresh.auth[user]) as connection:
            rows = await connection.execute(text("SELECT id FROM api_keys"))
            return {row.id for row in rows}

    assert view["id"] in await seen("carol")
    assert await seen("alice") == set()
    assert view["id"] not in await seen("bob")


async def test_a_user_delete_removes_the_keys_of_the_user(
    engine: AsyncEngine, fresh: World
) -> None:
    store = ApiKeyStore(engine)
    org_a = fresh.ids["org-a"]
    # Alice becomes an admin, makes a key, and carol revokes a key of her own.
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership)
            .where(Membership.user_id == fresh.ids["alice"], Membership.workspace_id.is_(None))
            .values(role="admin")
        )
    alices, key = await _make(store, fresh, actor="alice")
    carols, _ = await _make(store, fresh)
    await store.revoke(org_a, carols["id"], actor_user_id=fresh.ids["alice"])

    export = await DataControls(engine).export(
        org_a, kind="user", user_id=fresh.ids["alice"], actor_user_id=fresh.ids["alice"]
    )
    try:
        with zipfile.ZipFile(export.path) as archive:
            rows = [
                json.loads(line)
                for line in archive.read("tables/api_keys.jsonl").decode().splitlines()
            ]
    finally:
        export.path.unlink()
    assert {row["id"] for row in rows} == {str(alices["id"]), str(carols["id"])}
    assert all("key_hash" not in row for row in rows)

    await DataControls(engine).delete_user(
        org_a, fresh.ids["alice"], actor_user_id=fresh.ids["carol"]
    )
    async with engine.connect() as connection:
        left = (
            await connection.execute(
                select(ApiKey.id, ApiKey.revoked_by_user_id).where(ApiKey.org_id == org_a)
            )
        ).all()
    ids = {row.id: row.revoked_by_user_id for row in left}
    assert alices["id"] not in ids
    assert ids[carols["id"]] is None
    with pytest.raises(InvalidApiKeyError):
        await store.verify(key, "chat")


def test_the_migration_adds_and_removes_the_table() -> None:
    async def fetch(url: str, query: str) -> list[asyncpg.Record]:
        connection = await asyncpg.connect(url)
        try:
            return await connection.fetch(query)
        finally:
            await connection.close()

    def policies(url: str) -> list[str]:
        rows = asyncio.run(
            fetch(url, "SELECT policyname FROM pg_policies WHERE tablename = 'api_keys'")
        )
        return sorted(row["policyname"] for row in rows)

    with migrated_database() as url:
        asyncio.run(seed(url))
        assert policies(url) == ["api_keys_select"]
        config = alembic_config(url)
        command.downgrade(config, "0041")
        assert policies(url) == []
        command.upgrade(config, "head")
        assert policies(url) == ["api_keys_select"]
