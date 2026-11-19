"""Starts the Telegram gateway: ``python -m sama.gateway`` (docs/architecture.md, Sections 11.5
and 11.6).

The gateway takes the updates of the Telegram bots that the orgs connected as integrations, at
``POST /telegram/<integration id>/webhook``. ``python -m sama.controlplane`` serves the same
routes; run this process only when the API runs on its own (``python -m sama.api``).

Environment:

- ``SAMA_DATABASE_URL``: the control plane database.
- ``SAMA_VAULT_KEY``: the vault key. The vault keeps the bot tokens.
- ``OPENAI_API_KEY``: optional. The default key for voice notes. An org key in the vault comes
  first.

A worker must run too: the gateway only queues runs and follows them.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr

from sama.db.engine import create_engine
from sama.integrations.hub import TelegramHub
from sama.observability.logs import configure_logging
from sama.tools.server import quiet_dependency_logs
from sama.vault.crypto import VaultKey
from sama.vault.redact import default_redactor, env_secrets
from sama.vault.store import OrgKeys, SecretName, Vault


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"set {name}")
    return value


def build_app() -> FastAPI:
    engine = create_engine(_required("SAMA_DATABASE_URL"))
    try:
        vault = Vault(engine, VaultKey.from_text(_required("SAMA_VAULT_KEY")))
    except ValueError as error:
        raise SystemExit(f"SAMA_VAULT_KEY: {error}") from None
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    keys = OrgKeys(
        vault, {SecretName.OPENAI_API_KEY: SecretStr(openai_key) if openai_key else None}
    )
    hub = TelegramHub(engine, vault, keys=keys)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await vault.refresh_redactor()
        try:
            yield
        finally:
            await hub.aclose()
            await engine.dispose()

    app = FastAPI(title="sama Telegram gateway", lifespan=lifespan)
    app.include_router(hub.router())

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="The sama Telegram gateway.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    configure_logging("gateway")
    quiet_dependency_logs()
    default_redactor().add(*env_secrets("SAMA_DATABASE_URL", "SAMA_VAULT_KEY", "OPENAI_API_KEY"))
    uvicorn.run(build_app(), host=args.host, port=args.port, log_config=None)


if __name__ == "__main__":
    main()
