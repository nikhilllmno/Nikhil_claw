"""The standalone API process and memory: it must know its secrets (Section 8.1)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api.__main__ import build_app
from sama.vault.crypto import VaultKey
from sama.vault.redact import REDACTED, Redactor, default_redactor, redact
from sama.vault.store import SecretName, Vault
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.api.test_app import mia
from tests.orchestrator.conftest import Tenant, engine, queue_database_url, tenant  # noqa: F401

SUPABASE_URL = ISSUER.removesuffix("/auth/v1")


@pytest.fixture
def standalone_env(monkeypatch: pytest.MonkeyPatch, queue_database_url: str) -> Iterator[None]:  # noqa: F811
    monkeypatch.setenv("SAMA_SUPABASE_URL", SUPABASE_URL)
    monkeypatch.setenv("SAMA_SUPABASE_ANON_KEY", "anon-key")
    monkeypatch.setenv("SAMA_SUPABASE_JWT_SECRET", SECRET)
    monkeypatch.setenv("SAMA_DATABASE_URL", queue_database_url)
    monkeypatch.delenv("SAMA_VAULT_KEY", raising=False)
    monkeypatch.delenv("SAMA_PUBLIC_URL", raising=False)
    yield
    default_redactor().forget(SECRET)


@pytest.mark.usefixtures("tenant", "standalone_env")
async def test_the_standalone_api_refuses_its_own_secret_in_memory(tenant: Tenant) -> None:  # noqa: F811
    app = build_app()
    assert redact(f"x {SECRET} y") == f"x {REDACTED} y"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        response = await client.put(
            f"/workspaces/{tenant.ids['ws']}/memory/user",
            headers=bearer(tenant.ids["mia_auth"]),
            json={"entries": [f"the jwt secret is {SECRET}"]},
        )
        assert response.status_code == 422
        assert SECRET not in response.text
        listed = await client.get(f"/workspaces/{tenant.ids['ws']}/memory", headers=mia(tenant))
    notes = {note["target"]: note for note in listed.json()["targets"]}
    assert notes["user"]["entries"] == []


@pytest.mark.usefixtures("standalone_env")
async def test_the_standalone_api_learns_the_vault_values_at_start(
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
) -> None:
    key = VaultKey.generate()
    monkeypatch.setenv("SAMA_VAULT_KEY", key)
    value = "vault-value-" + uuid.uuid4().hex
    await Vault(engine, VaultKey.from_text(key), redactor=Redactor()).put(
        tenant.org, SecretName.BRAVE_API_KEY, value
    )
    app = build_app()
    try:
        assert redact(value) == value
        async with app.router.lifespan_context(app):
            assert redact(value) == REDACTED
    finally:
        default_redactor().forget(value, key)
