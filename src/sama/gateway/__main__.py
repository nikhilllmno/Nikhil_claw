"""Starts the Telegram gateway: ``python -m sama.gateway`` (docs/architecture.md, Section 11.5).

Environment:

- ``SAMA_DATABASE_URL``: the control plane database.
- ``SAMA_TELEGRAM_BOT_TOKEN``: the token of the shared sama bot (from BotFather).
- ``SAMA_TELEGRAM_WEBHOOK_SECRET``: a random value of 16 to 256 characters (``A-Z``, ``a-z``,
  ``0-9``, ``_``, ``-``). Telegram sends it with each update.
- ``SAMA_TELEGRAM_BOT_USERNAME``: optional. Without it, the gateway asks Telegram (``getMe``).
- ``SAMA_PUBLIC_URL``: optional. When it is set, the gateway registers
  ``<SAMA_PUBLIC_URL>/telegram/webhook`` with Telegram at start.
- ``OPENAI_API_KEY``: optional. It turns on voice notes. Until the vault (M1.17) the key comes
  from the environment of the gateway, never from a sandbox.

A worker must run too: the gateway only queues runs and follows them.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr

from sama.db.engine import create_engine
from sama.gateway.telegram import TelegramGateway, TelegramSettings
from sama.gateway.telegram_client import TelegramClient, bot_id
from sama.gateway.transcribe import OpenAITranscriber


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"set {name}")
    return value


def build_app() -> FastAPI:
    token = SecretStr(_required("SAMA_TELEGRAM_BOT_TOKEN"))
    secret = SecretStr(_required("SAMA_TELEGRAM_WEBHOOK_SECRET"))
    engine = create_engine(_required("SAMA_DATABASE_URL"))
    client = TelegramClient(token)
    username = os.environ.get("SAMA_TELEGRAM_BOT_USERNAME", "").lstrip("@")
    if not username:
        username = asyncio.run(_username(token))
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    transcriber = OpenAITranscriber(SecretStr(openai_key)) if openai_key else None
    gateway = TelegramGateway(
        engine,
        client,
        settings=TelegramSettings(bot_username=username),
        webhook_secret=secret,
        bot_user_id=bot_id(token),
        transcriber=transcriber,
    )
    public_url = os.environ.get("SAMA_PUBLIC_URL", "").rstrip("/")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if public_url:
            await client.set_webhook(f"{public_url}/telegram/webhook", secret)
        try:
            yield
        finally:
            await gateway.aclose()
            await client.aclose()
            if transcriber is not None:
                await transcriber.aclose()
            await engine.dispose()

    app = FastAPI(title="sama Telegram gateway", lifespan=lifespan)
    app.include_router(gateway.router())

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


async def _username(token: SecretStr) -> str:
    client = TelegramClient(token)
    try:
        return await client.username()
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="The sama Telegram gateway.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    # httpx logs each request URL, and a Telegram URL holds the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(build_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
