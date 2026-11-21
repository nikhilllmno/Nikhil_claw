"""The browser vault and the URL policy in Postgres: the stores, RLS, and the data controls
(docs/architecture.md, Sections 14.1, 16.4, and 17.4; roadmap M5.3)."""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
from alembic import command
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.browser.logins import BrowserLogins, LoginError
from sama.browser.policy import PolicyStore
from sama.datacontrols.service import DataControls
from sama.db.engine import as_user
from sama.db.models import BrowserLogin, VaultSecret
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import Vault, browser_login_secret
from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import World, seed

PASSWORD = "pw-for-the-db-test-1"


@pytest.fixture
def vault(engine: AsyncEngine) -> Vault:
    return Vault(engine, VaultKey(b"v" * KEY_BYTES))


async def _add(
    logins: BrowserLogins, world: World, name: str, owner: str | None, actor: str = "carol"
) -> uuid.UUID:
    login = await logins.create(
        world.ids["org-a"],
        name=name,
        site="https://example.com",
        username=f"{name}@example.com",
        password=SecretStr(PASSWORD),
        owner_user_id=world.ids[owner] if owner else None,
        actor_user_id=world.ids[actor],
    )
    return login.id


async def test_members_read_org_logins_and_their_own_and_admins_read_all(
    engine: AsyncEngine, vault: Vault, fresh: World
) -> None:
    logins = BrowserLogins(engine, vault)
    shared = await _add(logins, fresh, "shared", None)
    alices = await _add(logins, fresh, "alice-only", "alice", actor="alice")
    daves = await _add(logins, fresh, "dave-only", "dave", actor="dave")

    async def seen(user: str) -> set[uuid.UUID]:
        async with as_user(engine, fresh.auth[user]) as connection:
            return set((await connection.execute(select(BrowserLogin.id))).scalars())

    assert await seen("alice") == {shared, alices}
    assert await seen("dave") == {shared, daves}
    assert await seen("carol") == {shared, alices, daves}  # the org admin
    assert await seen("bob") == set()  # another org
    # The usable logins of a user, as the tool sees them.
    usable = await logins.usable(str(fresh.ids["org-a"]), str(fresh.ids["alice"]))
    assert {login.id for login in usable} == {shared, alices}
    # The credentials come from the vault, and no member can read the vault values.
    found = next(login for login in usable if login.id == alices)
    credentials = await logins.credentials(found)
    assert credentials.password.get_secret_value() == PASSWORD
    assert credentials.username.get_secret_value() == "alice-only@example.com"
    async with as_user(engine, fresh.auth["alice"]) as connection:
        assert (await connection.execute(select(VaultSecret.name))).all() == []


async def test_a_member_cannot_write_the_tables(engine: AsyncEngine, fresh: World) -> None:
    async with as_user(engine, fresh.auth["carol"]) as connection:
        with pytest.raises(Exception, match="permission denied"):
            await connection.execute(
                text(
                    "INSERT INTO browser_url_policies (org_id, block_hosts)"
                    " VALUES (:org, '{evil.test}')"
                ),
                {"org": fresh.ids["org-a"]},
            )


async def test_only_admins_read_the_url_policy(engine: AsyncEngine, fresh: World) -> None:
    store = PolicyStore(engine)
    view = await store.put(
        fresh.ids["org-a"],
        allow_hosts=[],
        block_hosts=["evil.test"],
        actor_user_id=fresh.ids["carol"],
    )
    assert view.policy.block_hosts == ("evil.test",)
    assert (await store.policy(str(fresh.ids["org-a"]))).block_hosts == ("evil.test",)
    assert (await store.policy(str(fresh.ids["org-b"]))).block_hosts == ()

    async def rows(user: str) -> int:
        async with as_user(engine, fresh.auth[user]) as connection:
            return len((await connection.execute(text("SELECT * FROM browser_url_policies"))).all())

    assert await rows("carol") == 1
    assert await rows("alice") == 0
    assert await rows("bob") == 0
    # A second put replaces the row.
    await store.put(fresh.ids["org-a"], allow_hosts=["a.com"], block_hosts=[], actor_user_id=None)
    assert (await store.view(fresh.ids["org-a"])).policy.allow_hosts == ("a.com",)


async def test_a_duplicate_name_is_a_conflict(
    engine: AsyncEngine, vault: Vault, fresh: World
) -> None:
    logins = BrowserLogins(engine, vault)
    await _add(logins, fresh, "Mail", None)
    with pytest.raises(LoginError) as raised:
        await _add(logins, fresh, "mail", None)
    assert raised.value.status_code == 409
    # A personal login can have the name of an org login.
    await _add(logins, fresh, "mail", "alice", actor="alice")
    # Nothing of the failed add stays in the vault.
    async with engine.connect() as connection:
        count = len(
            (
                await connection.execute(
                    select(VaultSecret.name).where(VaultSecret.name.like("browser_login:%"))
                )
            ).all()
        )
        rows = len((await connection.execute(select(BrowserLogin.id))).all())
    assert count == rows


async def test_a_user_delete_removes_the_personal_logins_and_their_secrets(
    engine: AsyncEngine, vault: Vault, fresh: World
) -> None:
    logins = BrowserLogins(engine, vault)
    shared = await _add(logins, fresh, "shared", None, actor="alice")
    alices = await _add(logins, fresh, "alice-only", "alice", actor="alice")
    org_a, alice = fresh.ids["org-a"], fresh.ids["alice"]
    await DataControls(engine).delete_user(org_a, alice, actor_user_id=fresh.ids["carol"])
    async with engine.connect() as connection:
        left = (
            await connection.execute(
                select(BrowserLogin.id, BrowserLogin.created_by_user_id).where(
                    BrowserLogin.org_id == org_a
                )
            )
        ).all()
        names = set(
            (
                await connection.execute(
                    select(VaultSecret.name).where(VaultSecret.org_id == org_a)
                )
            ).scalars()
        )
    # The org login stays without its creator. The personal login and its secret go.
    assert [(row.id, row.created_by_user_id) for row in left] == [(shared, None)]
    assert str(browser_login_secret(shared)) in names
    assert str(browser_login_secret(alices)) not in names


def test_the_migration_adds_and_removes_the_tables() -> None:
    async def fetch(url: str, query: str) -> list[asyncpg.Record]:
        connection = await asyncpg.connect(url)
        try:
            return await connection.fetch(query)
        finally:
            await connection.close()

    def policies(url: str) -> list[str]:
        rows = asyncio.run(
            fetch(
                url,
                "SELECT policyname FROM pg_policies"
                " WHERE tablename IN ('browser_logins', 'browser_url_policies')",
            )
        )
        return sorted(row["policyname"] for row in rows)

    with migrated_database() as url:
        asyncio.run(seed(url))
        assert policies(url) == ["browser_logins_select", "browser_url_policies_select"]
        config = alembic_config(url)
        command.downgrade(config, "0037")
        assert policies(url) == []
        command.upgrade(config, "head")
        assert policies(url) == ["browser_logins_select", "browser_url_policies_select"]
