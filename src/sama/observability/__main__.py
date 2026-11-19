"""Starts the monitor: ``python -m sama.observability`` (docs/architecture.md, Section 18).

The monitor sends the traces of ended runs to Langfuse and posts error rate alerts to Slack.
It can run next to a worker. Two monitors can run at the same time: the trace export locks
each run, and only one monitor checks the error rate.

Environment:

- ``SAMA_DATABASE_URL``: the control plane database.
- ``LANGFUSE_BASE_URL``, ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``: turn on traces.
- ``SAMA_TRACE_CONTENT``: optional. ``off`` keeps the user text and the answer out of traces.
- ``SAMA_ALERT_SLACK_WEBHOOK_URL``: turns on the alerts. The URL is a secret.
- ``SAMA_ENVIRONMENT``: optional, for example ``staging`` or ``production``.

At least one of Langfuse and Slack must be set.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

from pydantic import SecretStr

from sama.db.engine import create_engine
from sama.observability.alerts import ErrorRateAlerter, SlackWebhook
from sama.observability.langfuse import LangfuseClient
from sama.observability.logs import configure_logging
from sama.observability.traces import TraceExporter

log = logging.getLogger("sama.observability")


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"set {name}")
    return value


async def run() -> None:
    langfuse_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    webhook = os.environ.get("SAMA_ALERT_SLACK_WEBHOOK_URL", "")
    if not langfuse_key and not webhook:
        raise SystemExit("set LANGFUSE_PUBLIC_KEY or SAMA_ALERT_SLACK_WEBHOOK_URL")
    engine = create_engine(_required("SAMA_DATABASE_URL"))
    environment = os.environ.get("SAMA_ENVIRONMENT") or None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    # The task group ends first, then the stack closes the clients and the engine.
    async with contextlib.AsyncExitStack() as stack, asyncio.TaskGroup() as tasks:
        stack.push_async_callback(engine.dispose)
        if langfuse_key:
            langfuse = LangfuseClient(
                _required("LANGFUSE_BASE_URL"),
                langfuse_key,
                SecretStr(_required("LANGFUSE_SECRET_KEY")),
                environment=environment,
            )
            stack.push_async_callback(langfuse.aclose)
            exporter = TraceExporter(
                engine,
                langfuse,
                environment=environment,
                include_content=os.environ.get("SAMA_TRACE_CONTENT", "on").lower() != "off",
            )
            tasks.create_task(exporter.serve(stop))
        if webhook:
            slack = SlackWebhook(SecretStr(webhook))
            stack.push_async_callback(slack.aclose)
            alerter = ErrorRateAlerter(engine, slack, environment=environment)
            tasks.create_task(alerter.serve(stop))
        log.info(
            "the monitor started",
            extra={"traces": bool(langfuse_key), "alerts": bool(webhook)},
        )


def main() -> None:
    configure_logging("monitor")
    asyncio.run(run())


if __name__ == "__main__":
    main()
