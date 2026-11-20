"""The vault in a standalone process (docs/architecture.md, Section 8.1).

``python -m sama.api`` and ``python -m sama.tools`` write memory and skills. Both check the text
with the redactor, so the redactor must know every vault value before the first request. These
two functions are the shared start-up of the two processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine

from sama.vault.crypto import VaultKey
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
