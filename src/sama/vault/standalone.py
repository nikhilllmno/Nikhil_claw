"""The vault in a standalone process (docs/architecture.md, Section 8.1).

``python -m sama.api`` and ``python -m sama.tools`` write memory and skills. Both check the text
with the redactor, so the redactor must know every vault value before the first request. These
two functions are the shared start-up of the two processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import VaultSecret
from sama.vault.crypto import VaultKey
from sama.vault.redact import default_redactor, env_secrets
from sama.vault.store import Vault


def vault_from_env(engine: AsyncEngine) -> Vault | None:
    """The vault of ``SAMA_VAULT_KEY``, or ``None`` when the variable is not set. A bad key
    stops the process."""
    text = os.environ.get("SAMA_VAULT_KEY", "").strip()
    if not text:
        return None
    try:
        return Vault(engine, VaultKey.from_text(text))
    except ValueError as error:
        raise SystemExit(f"SAMA_VAULT_KEY: {error}") from None


@asynccontextmanager
async def keep_redactor_current(vault: Vault | None) -> AsyncIterator[None]:
    """Gives the redactor every vault value before the body runs, and refreshes it in the
    background while the body runs. A failed first refresh raises, so the process does not
    start (fail closed). Later refreshes pick up the values that another process writes."""
    if vault is None:
        yield
        return
    await vault.refresh_redactor()
    stop = asyncio.Event()
    refresh = asyncio.create_task(vault.serve(stop))
    try:
        yield
    finally:
        stop.set()
        refresh.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refresh


ENV_SECRETS = (
    "SAMA_RUN_TOKEN_KEY",
    "SAMA_DATABASE_URL",
    "SAMA_VAULT_KEY",
    "SAMA_SUPABASE_JWT_SECRET",
    "SAMA_ALERT_SLACK_WEBHOOK_URL",
    "LANGFUSE_SECRET_KEY",
    "E2B_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "BRAVE_API_KEY",
    "GEMINI_API_KEY",
    "X_BEARER_TOKEN",
    "MEM0_API_KEY",
)
"""The environment variables that hold a secret of the control plane. The redactor knows their
values before a write checks text for secrets."""

VAULT_UNREADABLE = "the vault cannot be read, so the change is not stored"


class VaultUnreadableError(Exception):
    """The redactor cannot learn every secret of the org. The text is safe to show."""


async def know_the_secrets(engine: AsyncEngine, org_id: uuid.UUID) -> None:
    """Gives the redactor the environment secrets and every vault value, as the control plane
    does at start (Section 16.3). A write that checks text for secrets (the sub-agents and the
    soul of a profile) calls it first, so that an opaque known secret is then not stored.

    Fails closed: if the vault key is bad, the table cannot be read, or an entry does not
    decrypt, it raises ``VaultUnreadableError`` and the caller must not write. Without
    ``SAMA_VAULT_KEY`` the org must have no vault entry.
    """
    default_redactor().add(*env_secrets(*ENV_SECRETS))
    try:
        try:
            vault = vault_from_env(engine)
        except SystemExit as error:
            raise VaultUnreadableError(str(error.code)) from None
        if vault is None:
            async with engine.connect() as connection:
                stored = (
                    await connection.execute(
                        select(func.count()).where(VaultSecret.org_id == org_id)
                    )
                ).scalar_one()
            if stored:
                raise VaultUnreadableError("set SAMA_VAULT_KEY: the org has vault entries")
            return
        # Strict: one read, and any entry that does not decrypt raises. No count to compare.
        await vault.refresh_redactor(strict=True)
    except VaultUnreadableError:
        raise
    except Exception:
        # No detail: the text of a database error can have the connection URL.
        raise VaultUnreadableError(VAULT_UNREADABLE) from None
