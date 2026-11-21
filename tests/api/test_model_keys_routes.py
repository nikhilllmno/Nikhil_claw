"""Tests of the model key routes of the HTTP API (docs/architecture.md, Section 17.3, M6.7)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import JwtVerifier, SamaApi
from sama.db.models import AuditEntry, VaultSecret
from sama.modelkeys import ModelKeys
from sama.vault.crypto import KEY_BYTES, VaultKey
from sama.vault.store import KeySource, OrgKeys, SecretName, Vault
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.api.test_app import add_user, mia
from tests.orchestrator.conftest import Tenant

pytestmark = pytest.mark.usefixtures("tenant")

ANTHROPIC = "sk-ant-api03-" + "a" * 40
OPENAI = "sk-proj-" + "b" * 40
SAMA_KEY = SecretStr("sk-ant-sama-default-" + "c" * 20)


class Provider:
    """A fake provider: ``GET /v1/models`` answers ``status`` and keeps the requests."""

    def __init__(self) -> None:
        self.status = 200
        self.fail = False
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            raise httpx.ConnectError("down")
        return httpx.Response(self.status, json={"data": []})


@pytest.fixture
def provider() -> Provider:
    return Provider()


@pytest.fixture
def vault(engine: AsyncEngine) -> Vault:
    return Vault(engine, VaultKey(b"k" * KEY_BYTES))


@pytest.fixture
def keys(vault: Vault) -> OrgKeys:
    return OrgKeys(vault, {SecretName.ANTHROPIC_API_KEY: SAMA_KEY})


@pytest.fixture
async def mk(
    engine: AsyncEngine, vault: Vault, keys: OrgKeys, provider: Provider
) -> AsyncIterator[httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    api = SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        model_keys=ModelKeys(
            vault,
            keys,
            anthropic_url="https://anthropic.test",
            openai_url="https://openai.test",
            http=http,
        ),
    )
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        yield client
    await http.aclose()


async def _admin(engine: AsyncEngine, tenant: Tenant) -> dict[str, str]:
    return bearer(await add_user(engine, tenant, workspace_member=True, role="admin"))


async def test_an_admin_sets_replaces_and_removes_the_keys_of_the_org(
    mk: httpx.AsyncClient,
    engine: AsyncEngine,
    tenant: Tenant,
    keys: OrgKeys,
    provider: Provider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    admin = await _admin(engine, tenant)
    url = f"/orgs/{tenant.org}/model-keys"
    before = (await mk.get(url, headers=admin)).json()
    assert before == [
        {"provider": "anthropic", "org_key": False, "updated_at": None, "default_key": True},
        {"provider": "openai", "org_key": False, "updated_at": None, "default_key": False},
    ]
    assert (await keys.lookup(tenant.org, SecretName.ANTHROPIC_API_KEY)).source is KeySource.SAMA

    response = await mk.put(f"{url}/anthropic", headers=admin, json={"api_key": f" {ANTHROPIC} "})
    assert response.status_code == 200
    body = response.json()
    assert (body["provider"], body["org_key"]) == ("anthropic", True)
    assert body["updated_at"] is not None
    check = provider.requests[-1]
    assert str(check.url) == "https://anthropic.test/v1/models"
    assert check.headers["x-api-key"] == ANTHROPIC
    assert check.headers["anthropic-version"] == "2023-06-01"
    # The proxy reads the same vault: the key of the org applies at once.
    found = await keys.lookup(tenant.org, SecretName.ANTHROPIC_API_KEY)
    assert found.source is KeySource.ORG
    assert found.value is not None
    assert found.value.get_secret_value() == ANTHROPIC

    response = await mk.put(f"{url}/openai", headers=admin, json={"api_key": OPENAI})
    assert response.status_code == 200
    assert provider.requests[-1].headers["authorization"] == f"Bearer {OPENAI}"
    listed = (await mk.get(url, headers=admin)).json()
    assert [k["org_key"] for k in listed] == [True, True]

    replaced = "sk-ant-api03-" + "z" * 40
    assert (
        await mk.put(f"{url}/anthropic", headers=admin, json={"api_key": replaced})
    ).status_code == 200
    found = await keys.lookup(tenant.org, SecretName.ANTHROPIC_API_KEY)
    assert found.value is not None
    assert found.value.get_secret_value() == replaced

    assert (await mk.delete(f"{url}/anthropic", headers=admin)).status_code == 204
    assert (await mk.delete(f"{url}/anthropic", headers=admin)).status_code == 404
    found = await keys.lookup(tenant.org, SecretName.ANTHROPIC_API_KEY)
    assert (found.source, found.value) == (KeySource.SAMA, SAMA_KEY)

    async with engine.connect() as connection:
        actions = list(
            (
                await connection.execute(
                    select(AuditEntry.action, AuditEntry.target_id, AuditEntry.details)
                    .where(AuditEntry.org_id == tenant.org, AuditEntry.action.like("vault.%"))
                    .order_by(AuditEntry.id)
                )
            ).all()
        )
    assert [(a, t) for a, t, _ in actions] == [
        ("vault.secret_set", "anthropic_api_key"),
        ("vault.secret_set", "openai_api_key"),
        ("vault.secret_set", "anthropic_api_key"),
        ("vault.secret_deleted", "anthropic_api_key"),
    ]
    everything = json.dumps([list(map(str, row)) for row in actions]) + caplog.text
    for secret in (ANTHROPIC, OPENAI, replaced):
        assert secret not in everything
        assert secret[-4:] not in json.dumps(listed)


@pytest.mark.parametrize(
    ("provider_name", "key", "detail"),
    [
        ("anthropic", "sk-" + "a" * 30, "starts with sk-ant-"),
        ("openai", "pk-" + "a" * 30, "starts with sk-"),
        ("openai", "sk-short", "20 to 512"),
        ("openai", "sk-has a space-" + "a" * 20, "no spaces"),
    ],
)
async def test_a_key_of_the_wrong_form_is_refused_before_the_check(
    mk: httpx.AsyncClient,
    engine: AsyncEngine,
    tenant: Tenant,
    provider: Provider,
    provider_name: str,
    key: str,
    detail: str,
) -> None:
    admin = await _admin(engine, tenant)
    response = await mk.put(
        f"/orgs/{tenant.org}/model-keys/{provider_name}", headers=admin, json={"api_key": key}
    )
    assert response.status_code == 400
    assert detail in response.json()["detail"]
    assert key not in response.text
    assert provider.requests == []


@pytest.mark.parametrize(
    ("status", "fail", "answer"),
    [(401, False, 400), (403, False, 400), (500, False, 502), (200, True, 502)],
)
async def test_a_key_that_the_provider_does_not_accept_is_not_stored(
    mk: httpx.AsyncClient,
    engine: AsyncEngine,
    tenant: Tenant,
    provider: Provider,
    status: int,
    fail: bool,
    answer: int,
) -> None:
    provider.status, provider.fail = status, fail
    admin = await _admin(engine, tenant)
    response = await mk.put(
        f"/orgs/{tenant.org}/model-keys/openai", headers=admin, json={"api_key": OPENAI}
    )
    assert response.status_code == answer
    assert OPENAI not in response.text
    async with engine.connect() as connection:
        stored = await connection.scalar(
            select(VaultSecret.id).where(VaultSecret.org_id == tenant.org)
        )
    assert stored is None


async def test_only_an_admin_of_the_org_sees_or_changes_the_keys(
    mk: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant, provider: Provider
) -> None:
    url = f"/orgs/{tenant.org}/model-keys"
    body = {"api_key": OPENAI}
    assert (await mk.get(url, headers=mia(tenant))).status_code == 403
    assert (await mk.put(f"{url}/openai", headers=mia(tenant), json=body)).status_code == 403
    assert (await mk.delete(f"{url}/openai", headers=mia(tenant))).status_code == 403
    stranger = bearer("00000000-0000-0000-0000-00000000beef")
    assert (await mk.get(url, headers=stranger)).status_code == 404
    assert (await mk.put(f"{url}/openai", headers=stranger, json=body)).status_code == 404
    assert provider.requests == []
    admin = await _admin(engine, tenant)
    assert (await mk.put(f"{url}/gemini", headers=admin, json=body)).status_code == 422
    unknown = await mk.put(f"{url}/openai", headers=admin, json={"api_key": OPENAI, "x": 1})
    assert unknown.status_code == 422
    assert OPENAI not in unknown.text


async def test_without_a_vault_the_routes_answer_503(
    client: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    admin = await _admin(engine, tenant)
    response = await client.get(f"/orgs/{tenant.org}/model-keys", headers=admin)
    assert response.status_code == 503
