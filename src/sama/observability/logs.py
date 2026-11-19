"""Structured logs (docs/architecture.md, Section 18.1).

Each sama process calls ``configure_logging`` once at start. The result is one JSON object per
line on stderr, with the time, the level, the logger, the message, the service, and the ids
of the current run. ``log_context`` adds ids such as ``run_id`` to every record that the code
logs inside it, also in tasks that start inside it.

``SAMA_LOG_FORMAT=text`` gives plain lines for a terminal. ``SAMA_LOG_LEVEL`` sets the level
(default ``INFO``).
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Final, TextIO

_context: Final[contextvars.ContextVar[Mapping[str, str] | None]] = contextvars.ContextVar(
    "sama_log_context", default=None
)

QUIET_LOGGERS: Final = ("httpx", "httpcore")
"""Loggers that log each request URL. A Telegram URL holds the bot token, so these log only
warnings and errors."""

_RESERVED: Final = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys() | {"message", "asctime"}
)


@contextmanager
def log_context(**fields: object) -> Iterator[None]:
    """Adds ``fields`` to each record logged inside the block. ``None`` values are left out.
    An inner block can add fields or replace them; the outer fields come back at the end."""
    added = {key: str(value) for key, value in fields.items() if value is not None}
    token = _context.set({**current_context(), **added})
    try:
        yield
    finally:
        _context.reset(token)


def current_context() -> Mapping[str, str]:
    return _context.get() or {}


class JsonFormatter(logging.Formatter):
    """Formats a record as one JSON object. Fields from ``log_context`` and from ``extra=``
    become top-level keys. A key never replaces ``ts``, ``level``, ``logger``, or ``msg``."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            "service": self._service,
        }
        for key, value in current_context().items():
            entry.setdefault(key, value)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                entry.setdefault(key, value if _is_plain(value) else str(value))
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            entry["exc"] = record.exc_text
        return json.dumps(entry, ensure_ascii=False, default=str)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        fields = " ".join(f"{key}={value}" for key, value in current_context().items())
        return f"{line} {fields}" if fields else line


def _is_plain(value: object) -> bool:
    return value is None or isinstance(value, str | int | float | bool | list | dict)


def configure_logging(service: str, *, stream: TextIO | None = None) -> None:
    """Sends all logs of the process to ``stream`` (default stderr) in the format of
    ``SAMA_LOG_FORMAT``. It replaces the handlers of the root logger."""
    handler = logging.StreamHandler(stream or sys.stderr)
    if os.environ.get("SAMA_LOG_FORMAT", "json").lower() == "text":
        handler.setFormatter(_TextFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    else:
        handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(os.environ.get("SAMA_LOG_LEVEL", "INFO").upper())
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # uvicorn adds its own handlers unless ``log_config=None``. Its records go to the root.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
