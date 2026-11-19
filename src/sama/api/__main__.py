"""Starts the API: ``python -m sama.api`` (docs/architecture.md, Section 11.4).

Environment:

- ``SAMA_DATABASE_URL``: the control plane database.
- ``SAMA_SUPABASE_URL``: the Supabase project, for example ``https://abc.supabase.co``. The API
  reads the signing keys of the project from its JWKS.
- ``SAMA_SUPABASE_ANON_KEY``: the public key of the project. The web page uses it to sign in.
- ``SAMA_SUPABASE_JWT_SECRET``: optional. The legacy shared secret (``HS256``) of the project.
- ``SAMA_VAULT_KEY`` and ``SAMA_PUBLIC_URL``: optional. Together they turn on the integrations
  (Section 11.6): org admins connect their own Telegram bot on the web page. The vault keeps the
  bot token. ``python -m sama.gateway`` takes the updates of the bots at ``SAMA_PUBLIC_URL``.

This process does not run the worker or the tool server. ``python -m sama.controlplane`` runs
all of them in one process, so that the API can answer the clarify questions of the tool
server.
"""

from __future__ import annotations

import argparse
import os

import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr

from sama.api.app import ApiSettings, SamaApi
from sama.api.auth import JwtVerifier, issuer, jwks_url
from sama.db.engine import create_engine
from sama.integrations.service import Integrations
from sama.observability.logs import configure_logging
from sama.vault.crypto import VaultKey
from sama.vault.store import Vault


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"set {name}")
    return value


def build_app() -> FastAPI:
    supabase_url = _required("SAMA_SUPABASE_URL").rstrip("/")
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
    if vault_key and public_url:
        try:
            vault = Vault(engine, VaultKey.from_text(vault_key))
        except ValueError as error:
            raise SystemExit(f"SAMA_VAULT_KEY: {error}") from None
        integrations = Integrations(engine, vault, public_url=public_url)
    api = SamaApi(
        engine,
        verifier=verifier,
        settings=ApiSettings(
            supabase_url=supabase_url,
            supabase_anon_key=_required("SAMA_SUPABASE_ANON_KEY"),
        ),
        integrations=integrations,
    )
    return api.app()


def main() -> None:
    parser = argparse.ArgumentParser(description="The sama HTTP API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    configure_logging("api")
    uvicorn.run(build_app(), host=args.host, port=args.port, log_config=None)


if __name__ == "__main__":
    main()
