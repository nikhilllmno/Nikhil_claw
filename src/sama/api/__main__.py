"""Starts the API: ``python -m sama.api`` (docs/architecture.md, Section 11.4).

Environment:

- ``SAMA_DATABASE_URL``: the control plane database.
- ``SAMA_SUPABASE_URL``: the Supabase project, for example ``https://abc.supabase.co``. The API
  reads the signing keys of the project from its JWKS.
- ``SAMA_SUPABASE_ANON_KEY``: the public key of the project. The web page uses it to sign in.
- ``SAMA_SUPABASE_JWT_SECRET``: optional. The legacy shared secret (``HS256``) of the project.
- ``SAMA_TELEGRAM_BOT_USERNAME``: optional. The shared sama bot. It turns on Telegram pairing
  codes on the web page. ``python -m sama.gateway`` runs the bot.

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
from sama.observability.logs import configure_logging


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
    api = SamaApi(
        create_engine(_required("SAMA_DATABASE_URL")),
        verifier=verifier,
        settings=ApiSettings(
            supabase_url=supabase_url,
            supabase_anon_key=_required("SAMA_SUPABASE_ANON_KEY"),
            telegram_bot_username=os.environ.get("SAMA_TELEGRAM_BOT_USERNAME", "").lstrip("@")
            or None,
        ),
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
