"""Starts the control plane: ``python -m sama.controlplane`` (docs/architecture.md, Section 19).

Required environment:

- ``SAMA_DATABASE_URL``: the control plane database.
- ``SAMA_PUBLIC_URL``: the public address of this process, for example
  ``https://sama-staging.fly.dev``. The sandboxes reach the model proxy and the tool server
  there, and Telegram sends its updates there.
- ``SAMA_RUN_TOKEN_KEY``: the run token key, at least 32 bytes.
- ``SAMA_SUPABASE_URL``, ``SAMA_SUPABASE_ANON_KEY``: the Supabase project for sign-in.
- ``E2B_API_KEY``: the sandbox provider.

Optional environment:

- ``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``: the provider keys of the model proxy.
- ``BRAVE_API_KEY``: turns on ``web_search``.
- ``SAMA_TELEGRAM_BOT_TOKEN``, ``SAMA_TELEGRAM_WEBHOOK_SECRET``, ``SAMA_TELEGRAM_BOT_USERNAME``:
  turn on the Telegram gateway. Without the username, the process asks Telegram at start.
- ``LANGFUSE_BASE_URL``, ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``: turn on traces.
- ``SAMA_ALERT_SLACK_WEBHOOK_URL``: turns on the error rate alerts.
- ``SAMA_ENVIRONMENT``, ``SAMA_TRACE_CONTENT``, ``SAMA_SUPABASE_JWT_SECRET``,
  ``SAMA_SANDBOX_TEMPLATE``, ``SAMA_SANDBOX_ALLOW_OUT`` (comma-separated hosts),
  ``SAMA_WORKER_CONCURRENCY`` (default 4).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os

import uvicorn

from sama.controlplane.app import build
from sama.controlplane.server import DrainingServer
from sama.controlplane.settings import ControlPlaneSettings, SettingsError
from sama.gateway.telegram_client import TelegramClient
from sama.observability.logs import configure_logging
from sama.tools.server import quiet_dependency_logs

SHUTDOWN_GRACE_S = 30
"""How long uvicorn waits for open requests (event streams included) after the drain of the
worker (``DRAIN_S``). The Fly.io ``kill_timeout`` must be longer than the whole stop."""


async def _username(settings: ControlPlaneSettings) -> ControlPlaneSettings:
    telegram = settings.telegram
    if telegram is None or telegram.bot_username:
        return settings
    client = TelegramClient(telegram.bot_token)
    try:
        username = await client.username()
    finally:
        await client.aclose()
    return dataclasses.replace(
        settings, telegram=dataclasses.replace(telegram, bot_username=username)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="The sama control plane.")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - the platform proxy connects
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    configure_logging("controlplane")
    quiet_dependency_logs()
    try:
        settings = ControlPlaneSettings.from_env(os.environ)
    except SettingsError as error:
        raise SystemExit(str(error)) from None
    settings = asyncio.run(_username(settings))
    plane = build(settings)
    config = uvicorn.Config(
        plane.app,
        host=args.host,
        port=args.port,
        log_config=None,
        timeout_graceful_shutdown=SHUTDOWN_GRACE_S,
    )
    DrainingServer(config, plane).run()


if __name__ == "__main__":
    main()
