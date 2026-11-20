"""Starts the API: ``python -m sama.api`` (docs/architecture.md, Section 11.4).

Environment:

- ``SAMA_DATABASE_URL``: the control plane database.
- ``SAMA_SUPABASE_URL``: the Supabase project, for example ``https://abc.supabase.co``. The API
  reads the signing keys of the project from its JWKS.
- ``SAMA_SUPABASE_ANON_KEY``: the public key of the project. The web page uses it to sign in.
- ``SAMA_SUPABASE_JWT_SECRET``: optional. The legacy shared secret (``HS256``) of the project.
- ``SAMA_VAULT_KEY``: optional. It opens the vault, so that the redactor knows the vault values
  (memory text with a secret is refused, Section 8.1).
- ``SAMA_PUBLIC_URL``: optional. With ``SAMA_VAULT_KEY`` it turns on the integrations
  (Section 11.6): org admins connect their own Telegram bot on the web page. The vault keeps the
  bot token. ``python -m sama.gateway`` takes the updates of the bots at ``SAMA_PUBLIC_URL``.

This process does not run the worker or the tool server. ``python -m sama.controlplane`` runs
all of them in one process, so that the API can answer the clarify questions of the tool
server.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr

from sama.api.app import ApiSettings, SamaApi
from sama.api.auth import JwtVerifier, issuer, jwks_url
from sama.db.engine import create_engine
from sama.integrations.service import Integrations
from sama.observability.logs import configure_logging
from sama.vault.crypto import VaultKey
from sama.vault.redact import default_redactor, env_secrets
from sama.vault.store import Vault


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"set {name}")
    return value


def build_app() -> FastAPI:
    supabase_url = _required("SAMA_SUPABASE_URL").rstrip("/")
    # The memory routes check text with the redactor (Section 8.1), so it must know the secrets
    # of this process before the first request.
    default_redactor().add(
        *env_secrets("SAMA_DATABASE_URL", "SAMA_SUPABASE_JWT_SECRET", "SAMA_VAULT_KEY")
    )
    secret = os.environ.get("SAMA_SUPABASE_JWT_SECRET", "")
    verifier = JwtVerifier(
        secret=SecretStr(secret) if secret else None,
        jwks_url=jwks_url(supabase_url),
        issuer=issuer(supabase_url),
    )
    engine = create_engine(_required("SAMA_DATABASE_URL"))
    vault_key = os.environ.get("SAMA_VAULT_KEY", "").strip()
    public_url = os.environ.get("SAMA_PUBLIC_URL", "").strip()
    integrations = None
    vault: Vault | None = None
    if vault_key:
        try:
            vault = Vault(engine, VaultKey.from_text(vault_key))
        except ValueError as error:
            raise SystemExit(f"SAMA_VAULT_KEY: {error}") from None
        if public_url:
            integrations = Integrations(engine, vault, public_url=public_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if vault is None:
            yield
            return
        # The redactor must know every vault value before the first request. A failed refresh
        # stops the start. Later refreshes pick up values that another process writes.
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

    api = SamaApi(
        engine,
        verifier=verifier,
        settings=ApiSettings(
            supabase_url=supabase_url,
            supabase_anon_key=_required("SAMA_SUPABASE_ANON_KEY"),
        ),
        integrations=integrations,
    )
    return api.app(lifespan=lifespan)


def main() -> None:
    parser = argparse.ArgumentParser(description="The sama HTTP API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    configure_logging("api")
    uvicorn.run(build_app(), host=args.host, port=args.port, log_config=None)


if __name__ == "__main__":
    main()
