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

- ``SAMA_VAULT_KEY``: the vault key, 32 bytes in base64 (Section 16.3). It turns on the
  vault, which keeps the provider, search, and bot keys of each org, and the integrations
  (Section 11.6): org admins connect their own Telegram bot on the web page.
- ``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``: the default provider keys of the model proxy.
  An org key in the vault comes first.
- ``BRAVE_API_KEY``: the default search key. ``web_search`` is on when this key or the vault
  is set.
- ``LANGFUSE_BASE_URL``, ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``: turn on traces.
- ``SAMA_ALERT_SLACK_WEBHOOK_URL``: turns on the error rate alerts.
- ``SAMA_WEB_ORIGINS``: the origins of the web app (``web/``) that may call the API from a
  browser, comma-separated, for example ``https://sama-web.vercel.app,http://localhost:3000``.
  An origin has no path and no ``*``. Without it, the API sends no CORS headers.
- ``SAMA_EXTENSION_IDS``: the ids of the Chrome extensions that may open the extension channel
  (``/channels/chrome/ws``), comma-separated. Without it, any ``chrome-extension://`` origin may
  open it. The channel always needs a valid access token.
- ``SAMA_ENVIRONMENT``, ``SAMA_TRACE_CONTENT``, ``SAMA_SUPABASE_JWT_SECRET``,
  ``SAMA_SANDBOX_TEMPLATE``, ``SAMA_SANDBOX_ALLOW_OUT`` (comma-separated hosts),
  ``SAMA_WORKER_CONCURRENCY`` (default 4), ``SAMA_VISION_MODEL`` (the model of
  ``vision_analyze``, default ``claude-sonnet-5-5``).
- ``SAMA_CONNECTOR_HOST_TEMPLATE``: the E2B template of the connector hosts of stdio MCP
  servers (default ``base``).
"""

from __future__ import annotations

import argparse
import os

import uvicorn

from sama.controlplane.app import build
from sama.controlplane.server import DrainingServer
from sama.controlplane.settings import ControlPlaneSettings, SettingsError
from sama.observability.logs import configure_logging
from sama.tools.server import quiet_dependency_logs

SHUTDOWN_GRACE_S = 30
"""How long uvicorn waits for open requests (event streams included) after the drain of the
worker (``DRAIN_S``). The Fly.io ``kill_timeout`` must be longer than the whole stop."""


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
