"""The browser settings routes: saved logins and the URL policy (Sections 14.1 and 16.4, M5.3)."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import JwtVerifier, SamaApi
from sama.api.browser import BrowserSettings
from sama.browser.logins import BrowserLogins
from sama.browser.policy import PolicyStore
from sama.db.models import AuditEntry, BrowserLogin, VaultSecret
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import Vault, browser_login_secret
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.api.test_app import add_user, mia
from tests.orchestrator.conftest import Tenant

pytestmark = pytest.mark.usefixtures("tenant")

PASSWORD = "correct-horse-battery-staple"
USERNAME = "mia.login@example.com"


@pytest.fixture
def vault(engine: AsyncEngine) -> Vault:
    return Vault(engine, VaultKey(b"k" * KEY_BYTES))


@pytest.fixture
def forgotten() -> list[str]:
    return []


async def _client(api: SamaApi) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        yield client


@pytest.fixture
async def http(
    engine: AsyncEngine, vault: Vault, forgotten: list[str]
) -> AsyncIterator[httpx.AsyncClient]:
    api = SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        browser=BrowserSettings(
            PolicyStore(engine), BrowserLogins(engine, vault), on_policy_change=forgotten.append
        ),
    )
    async for client in _client(api):
        yield client


async def _admin(engine: AsyncEngine, tenant: Tenant) -> dict[str, str]:
    return bearer(await add_user(engine, tenant, workspace_member=True, role="admin"))


def _login(**changes: str) -> dict[str, str]:
    body = {
        "name": "Example",
        "site": "https://example.com/login",
        "username": USERNAME,
        "password": PASSWORD,
    }
    body.update(changes)
    return body


async def test_a_member_adds_a_personal_login_and_the_secret_stays_in_the_vault(
    http: httpx.AsyncClient,
    engine: AsyncEngine,
    vault: Vault,
    tenant: Tenant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    url = f"/orgs/{tenant.org}/browser/logins"
    response = await http.post(url, headers=mia(tenant), json=_login())
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["name"], body["site"], body["scope"]) == (
        "Example",
        "https://example.com",
        "personal",
    )
    assert (body["mine"], body["can_manage"]) == (True, True)
    assert body["user_id"] == str(tenant.ids["mia"])
    listed = await http.get(url, headers=mia(tenant))
    assert [login["id"] for login in listed.json()] == [body["id"]]

    login_id = uuid.UUID(body["id"])
    secret = await vault.get(tenant.org, browser_login_secret(login_id), fresh=True)
    assert secret is not None
    assert json.loads(secret.get_secret_value()) == {"username": USERNAME, "password": PASSWORD}
    async with engine.connect() as connection:
        rows = (await connection.execute(select(BrowserLogin))).mappings().all()
        audit = (
            await connection.execute(
                select(AuditEntry.action, AuditEntry.details).where(AuditEntry.org_id == tenant.org)
            )
        ).all()
        sealed = (await connection.execute(select(VaultSecret.ciphertext))).scalars().all()
    assert {a for a, _ in audit} >= {"browser.login_created", "vault.secret_set"}
    everything = (
        response.text + listed.text + caplog.text + json.dumps([dict(r) for r in rows], default=str)
    ) + json.dumps([list(map(str, row)) for row in audit])
    for value in (PASSWORD, USERNAME):
        assert value not in everything
        assert all(value.encode() not in bytes(c) for c in sealed)


async def test_only_an_admin_adds_an_org_login_and_each_member_sees_it(
    http: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    url = f"/orgs/{tenant.org}/browser/logins"
    refused = await http.post(url, headers=mia(tenant), json=_login(scope="org"))
    assert refused.status_code == 403
    admin = await _admin(engine, tenant)
    added = await http.post(url, headers=admin, json=_login(scope="org", name="Shared"))
    assert added.status_code == 201
    assert added.json()["scope"] == "org"
    assert added.json()["user_id"] is None
    # Mia sees the org login but cannot change it.
    seen = (await http.get(url, headers=mia(tenant))).json()
    assert [(login["name"], login["can_manage"]) for login in seen] == [("Shared", False)]
    login_id = added.json()["id"]
    assert (await http.delete(f"{url}/{login_id}", headers=mia(tenant))).status_code == 403
    assert (
        await http.patch(f"{url}/{login_id}", headers=mia(tenant), json={"name": "x"})
    ).status_code == 403
    # The same name is refused for a second org login.
    again = await http.post(url, headers=admin, json=_login(scope="org", name="shared"))
    assert again.status_code == 409


async def test_personal_logins_are_private_to_their_user_and_visible_to_admins(
    http: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    url = f"/orgs/{tenant.org}/browser/logins"
    mine = (await http.post(url, headers=mia(tenant), json=_login())).json()
    other = bearer(await add_user(engine, tenant, workspace_member=True))
    assert (await http.get(url, headers=other)).json() == []
    # The other member cannot see, change, or remove it.
    assert (await http.delete(f"{url}/{mine['id']}", headers=other)).status_code == 404
    # The same name is allowed for the personal login of another user.
    assert (await http.post(url, headers=other, json=_login())).status_code == 201
    admin = await _admin(engine, tenant)
    names = [
        (login["name"], login["mine"]) for login in (await http.get(url, headers=admin)).json()
    ]
    assert names == [("Example", False), ("Example", False)]
    assert (await http.delete(f"{url}/{mine['id']}", headers=admin)).status_code == 204


async def test_a_change_keeps_the_other_part_of_the_secret(
    http: httpx.AsyncClient, vault: Vault, tenant: Tenant
) -> None:
    url = f"/orgs/{tenant.org}/browser/logins"
    added = (await http.post(url, headers=mia(tenant), json=_login())).json()
    new_password = "a-new-long-password-42"
    changed = await http.patch(
        f"{url}/{added['id']}",
        headers=mia(tenant),
        json={"password": new_password, "site": "accounts.example.com"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["site"] == "https://accounts.example.com"
    assert new_password not in changed.text
    secret = await vault.get(tenant.org, browser_login_secret(uuid.UUID(added["id"])), fresh=True)
    assert secret is not None
    assert json.loads(secret.get_secret_value()) == {"username": USERNAME, "password": new_password}
    assert (await http.delete(f"{url}/{added['id']}", headers=mia(tenant))).status_code == 204
    assert (
        await vault.get(tenant.org, browser_login_secret(uuid.UUID(added["id"])), fresh=True)
        is None
    )
    assert (await http.delete(f"{url}/{added['id']}", headers=mia(tenant))).status_code == 404


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        ({"site": "ftp://example.com"}, "http or https"),
        ({"site": "https://user:pw@example.com"}, "user name or a password"),
        ({"username": " padded "}, "no spaces"),
        ({"name": "   "}, "1 to 80"),
    ],
)
async def test_a_login_that_is_not_valid_is_refused(
    http: httpx.AsyncClient, tenant: Tenant, changes: dict[str, str], detail: str
) -> None:
    response = await http.post(
        f"/orgs/{tenant.org}/browser/logins", headers=mia(tenant), json=_login(**changes)
    )
    assert response.status_code == 400
    assert detail in response.json()["detail"]
    assert PASSWORD not in response.text


async def test_a_validation_error_does_not_repeat_the_password(
    http: httpx.AsyncClient, tenant: Tenant
) -> None:
    long = "p" * 1_001
    response = await http.post(
        f"/orgs/{tenant.org}/browser/logins", headers=mia(tenant), json=_login(password=long)
    )
    assert response.status_code == 422
    assert long not in response.text


async def test_an_admin_sets_the_url_policy(
    http: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant, forgotten: list[str]
) -> None:
    url = f"/orgs/{tenant.org}/browser/url-policy"
    assert (await http.get(url, headers=mia(tenant))).status_code == 403
    assert (await http.put(url, headers=mia(tenant), json={"block_hosts": []})).status_code == 403
    admin = await _admin(engine, tenant)
    empty = (await http.get(url, headers=admin)).json()
    assert empty == {"allow_hosts": [], "block_hosts": [], "mode": "block_list", "updated_at": None}
    response = await http.put(
        url,
        headers=admin,
        json={
            "allow_hosts": ["https://Docs.Example.com/path", "*.bücher.de", "example.com"],
            "block_hosts": ["evil.test", "evil.test."],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["allow_hosts"] == ["docs.example.com", "example.com", "xn--bcher-kva.de"]
    assert body["block_hosts"] == ["evil.test"]
    assert body["mode"] == "allow_list"
    assert forgotten == [str(tenant.org)]
    assert (await http.get(url, headers=admin)).json() == body
    bad = await http.put(url, headers=admin, json={"block_hosts": ["exa mple.com"]})
    assert bad.status_code == 400
    async with engine.connect() as connection:
        actions = (
            await connection.execute(
                select(AuditEntry.action).where(
                    AuditEntry.org_id == tenant.org, AuditEntry.action == "browser.url_policy_set"
                )
            )
        ).all()
    assert len(actions) == 1


async def test_the_routes_answer_409_without_the_browser_and_503_without_the_vault(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    verifier = JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER)
    async for client in _client(SamaApi(engine, verifier=verifier)):
        response = await client.get(f"/orgs/{tenant.org}/browser/logins", headers=mia(tenant))
        assert response.status_code == 409
    no_vault = SamaApi(
        engine, verifier=verifier, browser=BrowserSettings(PolicyStore(engine), None)
    )
    async for client in _client(no_vault):
        response = await client.get(f"/orgs/{tenant.org}/browser/logins", headers=mia(tenant))
        assert response.status_code == 503
