"""Tests of the vault in Postgres and of the org keys (docs/architecture.md, Section 16.3)."""

from __future__ import annotations

import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import VAULT_NAMES, AuditEntry, VaultSecret
from sama.vault.crypto import KEY_BYTES, VaultError, VaultKey
from sama.vault.redact import REDACTED, Redactor
from sama.vault.store import MAX_VALUE_CHARS, OrgKeys, SecretName, Vault
from tests.orchestrator.conftest import Tenant

KEY = VaultKey(b"v" * KEY_BYTES)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_the_names_match_the_table_check() -> None:
    assert tuple(SecretName) == VAULT_NAMES


async def test_put_get_names_and_delete(engine: AsyncEngine, tenant: Tenant) -> None:
    redactor = Redactor()
    vault = Vault(engine, KEY, redactor=redactor)
    assert await vault.get(tenant.org, SecretName.BRAVE_API_KEY) is None
    await vault.put(tenant.org, SecretName.BRAVE_API_KEY, "  brave-key-123  ")
    await vault.put(tenant.org, SecretName.OPENAI_API_KEY, "openai-key-123")
    value = await vault.get(tenant.org, SecretName.BRAVE_API_KEY)
    assert value is not None
    assert value.get_secret_value() == "brave-key-123"
    assert await vault.names(tenant.org) == [SecretName.BRAVE_API_KEY, SecretName.OPENAI_API_KEY]
    assert redactor.text("x brave-key-123") == f"x {REDACTED}"

    await vault.put(tenant.org, SecretName.BRAVE_API_KEY, "brave-key-456")
    fresh = Vault(engine, KEY, redactor=redactor)
    value = await fresh.get(tenant.org, SecretName.BRAVE_API_KEY)
    assert value is not None
    assert value.get_secret_value() == "brave-key-456"

    assert await vault.delete(tenant.org, SecretName.BRAVE_API_KEY)
    assert not await vault.delete(tenant.org, SecretName.BRAVE_API_KEY)
    assert await vault.get(tenant.org, SecretName.BRAVE_API_KEY) is None
    async with engine.connect() as connection:
        stored = (
            (
                await connection.execute(
                    select(VaultSecret.ciphertext).where(VaultSecret.org_id == tenant.org)
                )
            )
            .scalars()
            .all()
        )
        audit = (
            await connection.execute(
                select(AuditEntry.action, AuditEntry.target_id, AuditEntry.details)
                .where(AuditEntry.org_id == tenant.org, AuditEntry.target_type == "vault_secret")
                .order_by(AuditEntry.id)
            )
        ).all()
    assert all(b"openai-key-123" not in row for row in stored)
    assert [tuple(row) for row in audit] == [
        ("vault.secret_set", "brave_api_key", {}),
        ("vault.secret_set", "openai_api_key", {}),
        ("vault.secret_set", "brave_api_key", {}),
        ("vault.secret_deleted", "brave_api_key", {}),
    ]


async def test_an_org_reads_only_its_own_entry(engine: AsyncEngine, tenant: Tenant) -> None:
    vault = Vault(engine, KEY, redactor=Redactor())
    await vault.put(tenant.org, SecretName.BRAVE_API_KEY, "brave-key-org-1")
    assert await vault.get(uuid.uuid4(), SecretName.BRAVE_API_KEY) is None
    # A row copied to another name does not decrypt: the context binds the name.
    async with engine.begin() as connection:
        await connection.execute(
            update(VaultSecret)
            .where(VaultSecret.org_id == tenant.org)
            .values(name=SecretName.OPENAI_API_KEY.value)
        )
    with pytest.raises(VaultError):
        await Vault(engine, KEY).get(tenant.org, SecretName.OPENAI_API_KEY)


async def test_the_cache_holds_a_value_for_cache_s(engine: AsyncEngine, tenant: Tenant) -> None:
    clock = Clock()
    reader = Vault(engine, KEY, redactor=Redactor(), cache_s=10, clock=clock)
    writer = Vault(engine, KEY, redactor=Redactor())
    assert await reader.get(tenant.org, SecretName.BRAVE_API_KEY) is None
    await writer.put(tenant.org, SecretName.BRAVE_API_KEY, "brave-key-late")
    assert await reader.get(tenant.org, SecretName.BRAVE_API_KEY) is None
    clock.now = 11
    value = await reader.get(tenant.org, SecretName.BRAVE_API_KEY)
    assert value is not None
    assert value.get_secret_value() == "brave-key-late"


async def test_a_bad_value_is_refused(engine: AsyncEngine, tenant: Tenant) -> None:
    vault = Vault(engine, KEY, redactor=Redactor())
    for value in ("  ", "x" * (MAX_VALUE_CHARS + 1)):
        with pytest.raises(ValueError, match="a secret must have"):
            await vault.put(tenant.org, SecretName.BRAVE_API_KEY, value)


async def test_refresh_gives_the_redactor_every_value(
    engine: AsyncEngine, tenant: Tenant, caplog: pytest.LogCaptureFixture
) -> None:
    await Vault(engine, KEY, redactor=Redactor()).put(
        tenant.org, SecretName.ANTHROPIC_API_KEY, "anthropic-key-from-another-process"
    )
    other = VaultKey(b"o" * KEY_BYTES)
    await Vault(engine, other, redactor=Redactor()).put(
        tenant.org, SecretName.TELEGRAM_BOT_TOKEN, "bot-token-with-old-key"
    )
    redactor = Redactor()
    assert await Vault(engine, KEY, redactor=redactor).refresh_redactor() >= 1
    assert redactor.text("anthropic-key-from-another-process") == REDACTED
    assert "a vault entry does not decrypt" in caplog.text
    assert "bot-token-with-old-key" not in caplog.text


async def test_org_keys_use_the_vault_first(engine: AsyncEngine, tenant: Tenant) -> None:
    vault = Vault(engine, KEY, redactor=Redactor())
    await vault.put(tenant.org, SecretName.BRAVE_API_KEY, "brave-key-of-org")
    keys = OrgKeys(vault, {SecretName.BRAVE_API_KEY: SecretStr("brave-default")})
    org_key = await keys.get(str(tenant.org), SecretName.BRAVE_API_KEY)
    assert org_key is not None
    assert org_key.get_secret_value() == "brave-key-of-org"
    default = await keys.get(uuid.uuid4(), SecretName.BRAVE_API_KEY)
    assert default is not None
    assert default.get_secret_value() == "brave-default"
    assert await keys.get(tenant.org, SecretName.OPENAI_API_KEY) is None
    assert keys.available(SecretName.OPENAI_API_KEY)


async def test_org_keys_without_a_vault_use_the_defaults() -> None:
    keys = OrgKeys(None, {SecretName.BRAVE_API_KEY: SecretStr("brave-default")})
    assert keys.available(SecretName.BRAVE_API_KEY)
    assert not keys.available(SecretName.OPENAI_API_KEY)
    value = await keys.get(uuid.uuid4(), SecretName.BRAVE_API_KEY)
    assert value is not None
    assert value.get_secret_value() == "brave-default"
