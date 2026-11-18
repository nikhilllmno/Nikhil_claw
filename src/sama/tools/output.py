"""Tool output rules (docs/architecture.md, Sections 6.2, 16.5, and 16.7).

- ``cap``: a tool result is at most 50,000 bytes. A larger result goes to a file in the
  session sandbox (the "spill"), and the agent gets the head, a note with the path, and the
  tail.
- ``untrusted``: content from outside (web pages, search results) is marked as untrusted.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Final, Protocol

from sama.sandbox.base import Sandbox
from sama.tenancy.run_token import RunTokenClaims

log = logging.getLogger("sama.tools")

MAX_OUTPUT_BYTES: Final = 50_000
HEAD_BYTES: Final = 30_000
TAIL_BYTES: Final = 10_000
SPILL_DIR: Final = ".sama/spill"
SPILL_TIMEOUT_S: Final = 30.0

_CLOSE_TAG = re.compile(r"</(\s*untrusted_content)", re.IGNORECASE)


class Spill(Protocol):
    async def save(self, claims: RunTokenClaims, name: str, data: bytes) -> str | None:
        """Writes the data to a file the agent can read. Returns the path, or ``None`` if the
        run has no place for it."""
        ...


SessionSandbox = Callable[[str, str], Awaitable[Sandbox | None]]
"""``(org_id, session_id) -> Sandbox | None``, for example
``SandboxedRuntime.session_sandbox``."""


class SandboxSpill:
    """Writes spill files to ``<workdir>/.sama/spill/`` in the session sandbox."""

    def __init__(self, session_sandbox: SessionSandbox) -> None:
        self._session_sandbox = session_sandbox

    async def save(self, claims: RunTokenClaims, name: str, data: bytes) -> str | None:
        sandbox = await self._session_sandbox(claims.org_id, claims.session_id)
        if sandbox is None:
            return None
        path = f"{sandbox.workdir.rstrip('/')}/{SPILL_DIR}/{name}"
        await sandbox.write_file(path, data)
        return path


async def cap(text: str, *, tool: str, claims: RunTokenClaims, spill: Spill | None) -> str:
    """Returns ``text`` if it fits in ``MAX_OUTPUT_BYTES``. Otherwise spills it and returns the
    head, a note, and the tail."""
    data = text.encode("utf-8")
    if len(data) <= MAX_OUTPUT_BYTES:
        return text
    path: str | None = None
    if spill is not None:
        name = f"{tool}-{uuid.uuid4().hex[:12]}.txt"
        try:
            async with asyncio.timeout(SPILL_TIMEOUT_S):
                path = await spill.save(claims, name, data)
        except Exception as error:  # the cap must not fail the tool call
            log.warning(
                "spill failed: tool=%s run=%s error=%s", tool, claims.run_id, type(error).__name__
            )
    # A cut can split a character. "ignore" drops only the partial character at the cut.
    head = data[:HEAD_BYTES].decode("utf-8", errors="ignore")
    tail = data[-TAIL_BYTES:].decode("utf-8", errors="ignore")
    rest = (
        f"The full output is in the file {path}. Read that file to see the rest."
        if path is not None
        else "The full output was not kept."
    )
    note = f"[The output has {len(data):,} bytes. Only the start and the end are here. {rest}]"
    return f"{head}\n\n{note}\n\n{tail}"


def untrusted(body: str, *, source: str, url: str | None = None) -> str:
    """Marks content from outside as untrusted. A closing tag in the body is escaped, so the
    body cannot end the block early."""
    safe = _CLOSE_TAG.sub(r"<\\/\1", body)
    attrs = f' source="{html.escape(source, quote=True)}"'
    if url is not None:
        attrs += f' url="{html.escape(url, quote=True)}"'
    return f"<untrusted_content{attrs}>\n{safe}\n</untrusted_content>"
