"""Starts the model proxy: ``python -m sama.modelproxy`` (docs/architecture.md, Section 16.6).

Environment:

- ``SAMA_RUN_TOKEN_KEY``: the run token key of the control plane, at least 32 bytes.
- ``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``: the provider keys. A provider with no key gets
  ``503``. The vault replaces these variables in M1.17.
- ``SAMA_DATABASE_URL``: used only with ``--store postgres``.
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr

from sama.modelproxy.app import ModelProxy, ProxySettings
from sama.modelproxy.prices import PriceTable
from sama.modelproxy.store import MemoryUsageStore, PostgresUsageStore, UsageStore
from sama.tenancy.run_token import RunTokenSigner

log = logging.getLogger("sama.modelproxy")


def _secret(name: str) -> SecretStr | None:
    value = os.environ.get(name, "")
    return SecretStr(value) if value else None


def build_app(store_kind: str) -> FastAPI:
    key = os.environ.get("SAMA_RUN_TOKEN_KEY", "")
    if not key:
        raise SystemExit("set SAMA_RUN_TOKEN_KEY")
    store: UsageStore
    if store_kind == "postgres":
        from sama.db.engine import create_engine

        url = os.environ.get("SAMA_DATABASE_URL", "")
        if not url:
            raise SystemExit("set SAMA_DATABASE_URL for --store postgres")
        store = PostgresUsageStore(create_engine(url))
    else:
        log.warning("the call records are in memory only")
        store = MemoryUsageStore()
    proxy = ModelProxy(
        signer=RunTokenSigner(key.encode()),
        store=store,
        prices=PriceTable.load(),
        settings=ProxySettings(
            anthropic_key=_secret("ANTHROPIC_API_KEY"), openai_key=_secret("OPENAI_API_KEY")
        ),
    )
    app = proxy.app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await proxy.aclose()

    app.router.lifespan_context = lifespan
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="The sama model proxy.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--store", choices=("memory", "postgres"), default="memory")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(build_app(args.store), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
