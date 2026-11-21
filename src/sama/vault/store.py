"""The sama vault in Postgres, and the org keys of the services (docs/architecture.md,
Section 16.3).

``Vault`` stores the secrets of each org in ``vault_secrets``, sealed with
``sama.vault.crypto``. It never logs a value. Each value that it reads goes to the redactor,
and ``refresh_redactor`` (every ``REFRESH_S``) gives the redactor all values, so a value that
another process wrote is redacted too.

``OrgKeys`` gives the key of one org: the vault entry first, then the default key of the
process (the environment). The model proxy and ``web_search`` use it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, NewType

from pydantic import SecretStr
from sqlalchemy import delete, func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import AuditEntry, VaultSecret
from sama.vault.crypto import Sealed, VaultError, VaultKey, seal, unseal
from sama.vault.redact import Redactor, default_redactor

log: Final = logging.getLogger("sama.vault")

CACHE_S: Final = 60.0
"""How long a read value (or a missing value) stays in the cache of a process. A change from
another process shows after at most this time."""
REFRESH_S: Final = 60.0
MAX_VALUE_CHARS: Final = 10_000


class SecretName(StrEnum):
    ANTHROPIC_API_KEY = "anthropic_api_key"
    OPENAI_API_KEY = "openai_api_key"
    BRAVE_API_KEY = "brave_api_key"
    TELEGRAM_BOT_TOKEN = "telegram_bot_token"  # noqa: S105 (a name, not a secret)
    """The token of the Telegram integration of the org (Section 11.6)."""
    SLACK_BOT_TOKEN = "slack_bot_token"  # noqa: S105
    """The bot token of the Slack integration of the org (Section 11.6)."""
    SLACK_SIGNING_SECRET = "slack_signing_secret"  # noqa: S105
    """The signing secret of the Slack app of the org (Section 11.6)."""
    COMPOSIO_API_KEY = "composio_api_key"
    """The Composio API key of the org (Section 10.3). Without it, the key of the process."""
    COMPOSIO_WEBHOOK_SECRET = "composio_webhook_secret"  # noqa: S105
    """The signing secret of the Composio webhook subscription of the org (Section 12.2).
    Without it, the secret of the process."""


ConnectionSecretName = NewType("ConnectionSecretName", str)
"""The vault entry of the secret fields of one connection: ``connection:<connection id>``."""

TriggerSecretName = NewType("TriggerSecretName", str)
"""The vault entry of the signing secret of one webhook trigger: ``trigger:<trigger id>``."""

VaultName = SecretName | ConnectionSecretName | TriggerSecretName
"""The name of a vault entry."""


def connection_secret(connection_id: uuid.UUID) -> ConnectionSecretName:
    """The vault name of the secret fields of a connection (Section 10.3)."""
    return ConnectionSecretName(f"connection:{connection_id}")


def trigger_secret(trigger_id: uuid.UUID) -> TriggerSecretName:
    """The vault name of the signing secret of a webhook trigger (Section 12.2)."""
    return TriggerSecretName(f"trigger:{trigger_id}")


def secret_parts(name: str, value: str) -> list[str]:
    """The values that the redactor gets for one entry. A connection entry is a JSON object of
    the secret form fields: the redactor gets each field too, so a field is redacted alone."""
    parts = [value]
    if name.startswith("connection:"):
        with contextlib.suppress(ValueError):
            fields = json.loads(value)
            if isinstance(fields, dict):
                parts += [v for v in fields.values() if isinstance(v, str) and v.strip()]
    return parts


def _vault_name(name: str) -> VaultName:
    try:
        return SecretName(name)
    except ValueError:
        # A ``connection:`` or ``trigger:`` name. Both are plain text at run time.
        return ConnectionSecretName(name)


_SEALED: Final = (VaultSecret.key_id, VaultSecret.wrapped_key, VaultSecret.ciphertext)


def _context(org_id: uuid.UUID, name: str) -> bytes:
    return f"sama-vault:{org_id}:{name}".encode()


class Vault:
    def __init__(
        self,
        engine: AsyncEngine,
        key: VaultKey,
        *,
        redactor: Redactor | None = None,
        cache_s: float = CACHE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._key = key
        self._redactor = redactor or default_redactor()
        self._cache_s = cache_s
        self._clock = clock
        self._cache: dict[tuple[uuid.UUID, VaultName], tuple[float, str | None]] = {}

    async def put(
        self,
        org_id: uuid.UUID,
        name: VaultName,
        value: str,
        *,
        actor_user_id: uuid.UUID | None = None,
        connection: AsyncConnection | None = None,
    ) -> None:
        """Stores or replaces a secret, and writes an audit entry without the value.

        With ``connection`` the write is part of the transaction of the caller, and the cache
        forgets the name, so a rollback cannot leave a wrong value in the cache.
        """
        value = value.strip()
        if not value or len(value) > MAX_VALUE_CHARS:
            raise ValueError(f"a secret must have 1 to {MAX_VALUE_CHARS} characters")
        sealed = seal(self._key, value, context=_context(org_id, name))
        row = {
            "key_id": sealed.key_id,
            "wrapped_key": sealed.wrapped_key,
            "ciphertext": sealed.ciphertext,
        }
        statement = (
            pg_insert(VaultSecret)
            .values(org_id=org_id, name=str(name), **row)
            .on_conflict_do_update(
                index_elements=["org_id", "name"], set_={**row, "updated_at": func.now()}
            )
        )
        self._redactor.add(*secret_parts(str(name), value))
        if connection is not None:
            self._cache.pop((org_id, name), None)
            await connection.execute(statement)
            await _audit(connection, org_id, "vault.secret_set", name, actor_user_id)
            return
        async with self._engine.begin() as own:
            await own.execute(statement)
            await _audit(own, org_id, "vault.secret_set", name, actor_user_id)
        self._cache[(org_id, name)] = (self._clock() + self._cache_s, value)

    async def delete(
        self,
        org_id: uuid.UUID,
        name: VaultName,
        *,
        actor_user_id: uuid.UUID | None = None,
        connection: AsyncConnection | None = None,
    ) -> bool:
        """Deletes a secret. With ``connection`` it is part of the transaction of the caller."""
        self._cache.pop((org_id, name), None)
        if connection is None:
            async with self._engine.begin() as own:
                gone = await self.delete(org_id, name, actor_user_id=actor_user_id, connection=own)
            self._cache.pop((org_id, name), None)
            return gone
        gone = (
            await connection.execute(
                delete(VaultSecret)
                .where(VaultSecret.org_id == org_id, VaultSecret.name == str(name))
                .returning(VaultSecret.id)
            )
        ).first() is not None
        if gone:
            await _audit(connection, org_id, "vault.secret_deleted", name, actor_user_id)
        return gone

    async def names(self, org_id: uuid.UUID) -> list[VaultName]:
        """The names of the secrets of an org. It decrypts nothing."""
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                select(VaultSecret.name)
                .where(VaultSecret.org_id == org_id)
                .order_by(VaultSecret.name)
            )
            return [_vault_name(name) for (name,) in rows]

    async def updated(self, org_id: uuid.UUID) -> dict[VaultName, datetime]:
        """The time of the last change of each secret of an org. It decrypts nothing."""
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                select(VaultSecret.name, VaultSecret.updated_at).where(VaultSecret.org_id == org_id)
            )
            return {_vault_name(name): at for name, at in rows}

    async def get(
        self, org_id: uuid.UUID, name: VaultName, *, fresh: bool = False
    ) -> SecretStr | None:
        """The secret, or None. Raises ``VaultError`` if the entry does not decrypt.

        ``fresh`` reads the table and not the cache, for a value that another process can have
        changed a moment ago.
        """
        now = self._clock()
        cached = self._cache.get((org_id, name))
        if not fresh and cached is not None and cached[0] > now:
            return SecretStr(cached[1]) if cached[1] is not None else None
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(*_SEALED).where(
                        VaultSecret.org_id == org_id, VaultSecret.name == str(name)
                    )
                )
            ).first()
        value = None
        if row is not None:
            value = unseal(self._key, Sealed(*row), context=_context(org_id, name))
            self._redactor.add(*secret_parts(str(name), value))
        self._cache[(org_id, name)] = (now + self._cache_s, value)
        return SecretStr(value) if value is not None else None

    async def refresh_redactor(self, *, strict: bool = False) -> int:
        """Gives the redactor every value in the vault. Returns the count of values. An entry
        that does not decrypt is logged and left out. The redactor keeps the values that it
        already has, so a value that a cache or a run still uses stays redacted.

        With ``strict``, an entry that does not decrypt raises ``VaultError`` and the redactor
        gets no value. The read is one query, so the check covers the same rows that it reads.
        A caller that must know every value (a write that checks text for secrets) uses it."""
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(select(VaultSecret.org_id, VaultSecret.name, *_SEALED))
            ).all()
        values = []
        for org_id, name, key_id, wrapped_key, ciphertext in rows:
            sealed = Sealed(key_id, wrapped_key, ciphertext)
            try:
                values += secret_parts(
                    name, unseal(self._key, sealed, context=_context(org_id, name))
                )
            except VaultError as error:
                log.error("a vault entry does not decrypt org=%s name=%s: %s", org_id, name, error)
                if strict:
                    raise
        self._redactor.add(*values)
        return len(values)

    async def serve(self, stop: asyncio.Event, *, interval_s: float = REFRESH_S) -> None:
        """Refreshes the redactor every ``interval_s`` until ``stop`` is set. A failed refresh
        is logged. The caller does the first refresh before it starts work."""
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval_s)
            if stop.is_set():
                return
            try:
                await self.refresh_redactor()
            except Exception:
                log.exception("the vault refresh of the redactor failed")


async def _audit(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    action: str,
    name: VaultName,
    actor_user_id: uuid.UUID | None,
) -> None:
    await connection.execute(
        insert(AuditEntry).values(
            org_id=org_id,
            actor_kind="user" if actor_user_id else "system",
            actor_user_id=actor_user_id,
            action=action,
            target_type="vault_secret",
            target_id=str(name),
            details={},
        )
    )


class KeySource(StrEnum):
    """Where a provider key came from (Section 16.6). The usage records keep it, so billing
    (M6.6) can tell the calls on the key of the org from the calls on the key of sama."""

    ORG = "org"
    """The org brought its own key (the vault entry, M6.7)."""
    SAMA = "sama"
    """The default key of the process (the environment)."""


@dataclass(frozen=True, slots=True)
class OrgKey:
    """A key and its source. ``value`` is None when the org has no key and no default exists."""

    value: SecretStr | None
    source: KeySource


class OrgKeys:
    """The key of an org: the vault entry first, then the default of the process."""

    def __init__(
        self, vault: Vault | None, defaults: Mapping[SecretName, SecretStr | None]
    ) -> None:
        self._vault = vault
        self._defaults = dict(defaults)

    def available(self, name: SecretName) -> bool:
        """True if some org can have the key: the vault is on, or a default exists."""
        return self._vault is not None or self._defaults.get(name) is not None

    async def get(self, org_id: str | uuid.UUID, name: SecretName) -> SecretStr | None:
        """Raises ``VaultError`` if the vault entry does not decrypt."""
        return (await self.lookup(org_id, name)).value

    async def lookup(self, org_id: str | uuid.UUID, name: SecretName) -> OrgKey:
        """The key and its source. Raises ``VaultError`` if the vault entry does not decrypt:
        the caller must not use the default key then, because the org chose its own key."""
        if self._vault is not None:
            value = await self._vault.get(uuid.UUID(str(org_id)), name)
            if value is not None:
                return OrgKey(value, KeySource.ORG)
        return OrgKey(self._defaults.get(name), KeySource.SAMA)

    def has_default(self, name: SecretName) -> bool:
        """True if the process has a default key of this name."""
        return self._defaults.get(name) is not None
