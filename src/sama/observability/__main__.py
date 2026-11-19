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
    engine = create_engine(_required("SAMA_DATABASE_URL"))
    environment = os.environ.get("SAMA_ENVIRONMENT") or None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    langfuse: LangfuseClient | None = None
    slack: SlackWebhook | None = None
    tasks: list[asyncio.Task[None]] = []
    if os.environ.get("LANGFUSE_PUBLIC_KEY"):
        langfuse = LangfuseClient(
            _required("LANGFUSE_BASE_URL"),
            _required("LANGFUSE_PUBLIC_KEY"),
            SecretStr(_required("LANGFUSE_SECRET_KEY")),
            environment=environment,
        )
        exporter = TraceExporter(
            engine,
            langfuse,
            environment=environment,
            include_content=os.environ.get("SAMA_TRACE_CONTENT", "on").lower() != "off",
        )
        tasks.append(asyncio.create_task(exporter.serve(stop)))
    webhook = os.environ.get("SAMA_ALERT_SLACK_WEBHOOK_URL", "")
    if webhook:
        slack = SlackWebhook(SecretStr(webhook))
        alerter = ErrorRateAlerter(engine, slack, environment=environment)
        tasks.append(asyncio.create_task(alerter.serve(stop)))
    if not tasks:
        await engine.dispose()
        raise SystemExit("set LANGFUSE_PUBLIC_KEY or SAMA_ALERT_SLACK_WEBHOOK_URL")
    log.info(
        "the monitor started",
        extra={"traces": langfuse is not None, "alerts": slack is not None},
    )
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)
        if langfuse is not None:
            await langfuse.aclose()
        if slack is not None:
            await slack.aclose()
        await engine.dispose()


def main() -> None:
    configure_logging("monitor")
    asyncio.run(run())


if __name__ == "__main__":
    main()
