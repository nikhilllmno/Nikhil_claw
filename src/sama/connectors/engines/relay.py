"""A local relay for a driver that resolves the host itself (docs/architecture.md, Section
10.4).

Some drivers (pymongo) resolve the host name and open their own TLS connection. Then the
gateway cannot connect to the checked address (a second DNS answer could give a private
address) or send the real host name for TLS. The relay listens on ``127.0.0.1`` (a free port)
for one call. The driver connects to the relay without TLS. The relay connects to the checked
target with the TLS context of the gateway and copies the bytes in both directions.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from collections.abc import AsyncIterator

from sama.connectors.dbcore import Target

CHUNK = 65_536


@contextlib.asynccontextmanager
async def relay(
    target: Target, tls: ssl.SSLContext | None, server_hostname: str | None
) -> AsyncIterator[int]:
    """Yields the local port. Each connection to the port gets a new connection to the
    target. All connections close at the end."""
    tasks: set[asyncio.Task[None]] = set()
    writers: set[asyncio.StreamWriter] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.add(writer)
        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    target.host,
                    target.port,
                    ssl=tls,
                    server_hostname=server_hostname if tls is not None else None,
                ),
                10,
            )
        except (OSError, TimeoutError):
            writer.close()
            writers.discard(writer)
            return
        writers.add(remote_writer)
        try:
            await asyncio.gather(_pump(reader, remote_writer), _pump(remote_reader, writer))
        finally:
            for end in (writer, remote_writer):
                end.close()
                writers.discard(end)

    def start(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.ensure_future(handle(reader, writer))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    server = await asyncio.start_server(start, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        for writer in list(writers):
            writer.close()
        for task in list(tasks):
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.wait_closed(), 5)


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(CHUNK):
            writer.write(data)
            await writer.drain()
    except (OSError, ssl.SSLError):
        pass
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            if writer.can_write_eof():
                writer.write_eof()
