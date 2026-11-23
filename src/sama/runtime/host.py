"""The runner host: a runner that stays alive after its run to serve the next runs of the session.

Docs: ``docs/architecture.md``, Section 4.4 ("Kept runtime") and Section 16.2.

A run needs a Claude Code CLI (or the MCP connections of the OpenAI runtime). Starting them is
a large part of the time to the first token of a follow-up message. So the runner process of a
run that ended well does not exit. It becomes a *host*. It listens on a Unix socket next to the
socket of the warm runner server (``<socket>.host``). The next ``sama.warm`` client tries the
host first. The host runs the job in its own process, with the files, the environment, and the
work folder of the client, and it keeps the CLI and the MCP connections of the last run for the
job when the job fits (``ClaudeKeeper`` and ``OpenAIKeeper``).

The protocol is the one of the warm server (``sama.warm``): the client sends the request and its
stdin, stdout, and stderr; the host answers with one line: ``S`` (the host has other code, and
it stops), ``B`` (the host runs a job now, so the client uses the warm server), or
``X<exit code>``. A client that closes the socket cancels the job. If the job does not stop
within the cancel limit, the host exits, and with it the CLI: a cancel never waits for a kept
process.

The host runs one job at a time. It stops by itself when no job comes for ``idle_s`` seconds,
when its socket file is gone, or when a client has other code. A stopped host takes its CLI
with it, so the next run starts all again.

The host never holds a run token between jobs: the token lives in the token relay only while a
job runs (``sama.runtime.relay``).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import socket
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Final

from sama import warm

log: Final = logging.getLogger("sama.host")

HOST_SUFFIX: Final = warm.HOST_SUFFIX
CANCEL_LIMIT_S: Final = 8.0
"""The longest time between the end of the client and the exit of the host if the job does not
stop. The orchestrator waits 10 seconds for a cancel (Section 4.3)."""
WATCH_S: Final = 5.0
"""How often an idle host checks that its socket file is still there."""
CANCEL_LINE: Final = '{"op": "cancel"}\n'
_LINE_LIMIT: Final = 64 * 1024 * 1024


def host_path(socket_path: str) -> str:
    """The socket of the host that belongs to the warm server socket ``socket_path``."""
    return socket_path + HOST_SUFFIX


class JobInput:
    """The stdin of one job as a line stream that can also get a cancel line from the host.

    It reads a duplicate of the file, so closing it never closes a file of the owner.
    """

    def __init__(self, fd: int, *, owns_fd: bool = False) -> None:
        self._fd = fd
        self._owns_fd = owns_fd
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._transport: asyncio.ReadTransport | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=_LINE_LIMIT)
        pipe = os.fdopen(os.dup(self._fd), "rb", buffering=0)
        self._transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), pipe
        )
        self._task = asyncio.create_task(self._pump(reader))

    async def _pump(self, reader: asyncio.StreamReader) -> None:
        try:
            while raw := await reader.readline():
                self._queue.put_nowait(raw.decode())
        finally:
            self._queue.put_nowait(None)

    def cancel(self) -> None:
        """Gives the job a cancel line, as if the orchestrator had sent it."""
        self._queue.put_nowait(CANCEL_LINE)

    async def lines(self) -> AsyncIterator[str]:
        while (item := await self._queue.get()) is not None:
            yield item

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._transport is not None:
            self._transport.close()
        if self._owns_fd:
            with contextlib.suppress(OSError):
                os.close(self._fd)


@dataclass
class HostedJob:
    """A job that a client gave to the host: its request and its three files."""

    request: dict[str, Any]
    fds: list[int]
    input: JobInput | None = field(default=None)

    def cancel(self) -> None:
        if self.input is not None:
            self.input.cancel()


RunHosted = Callable[[HostedJob], Coroutine[Any, Any, int]]
HardStop = Callable[[], None]


class HostListener:
    """The listening socket of a host. One host for each socket path."""

    def __init__(self, sock: socket.socket, lock: int, path: str) -> None:
        self.sock = sock
        self._lock = lock
        self.path = path
        self._inode = os.stat(path).st_ino

    @classmethod
    def bind(cls, path: str) -> HostListener | None:
        """Makes the socket, or returns ``None`` if another host holds it."""
        lock = os.open(path + ".lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock)
            return None
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o077)
        try:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
            sock.bind(path)
            sock.listen(4)
        except OSError:
            sock.close()
            os.close(lock)
            raise
        finally:
            os.umask(previous)
        sock.setblocking(False)
        return cls(sock, lock, path)

    def still_bound(self) -> bool:
        """False if the socket file was removed or replaced: the host has no clients any more."""
        try:
            return os.stat(self.path).st_ino == self._inode
        except OSError:
            return False

    def close(self) -> None:
        self.sock.close()
        with contextlib.suppress(OSError):
            if self.still_bound():
                os.unlink(self.path)
        with contextlib.suppress(OSError):
            os.close(self._lock)


async def serve_jobs(
    listener: HostListener,
    run: RunHosted,
    *,
    code: str,
    idle_s: float,
    hard_stop: HardStop | None = None,
) -> None:
    """Serves jobs one at a time until the host is idle, stale, or has no socket file."""
    loop = asyncio.get_running_loop()
    stop = hard_stop or _exit_now
    last = loop.time()
    try:
        while True:
            remaining = idle_s - (loop.time() - last)
            if remaining <= 0:
                log.info("the host was idle for %.0f seconds; it stops", idle_s)
                return
            try:
                conn, _ = await asyncio.wait_for(
                    loop.sock_accept(listener.sock), timeout=min(remaining, WATCH_S)
                )
            except TimeoutError:
                if not listener.still_bound():
                    log.info("the socket of the host is gone; it stops")
                    return
                continue
            if await _serve_one(listener, conn, run, code, stop):
                return
            last = loop.time()
    finally:
        _refuse_waiting(listener)
        listener.close()


def _exit_now() -> None:
    os._exit(1)


def _refuse_waiting(listener: HostListener) -> None:
    """Answers the clients that wait in the queue of a host that stops: they use the warm
    server. No job of them started."""
    while True:
        try:
            conn, _ = listener.sock.accept()
        except OSError:
            return
        with contextlib.suppress(OSError):
            conn.sendall(warm.REPLY_STALE + b"\n")
        conn.close()


async def _serve_one(
    listener: HostListener, conn: socket.socket, run: RunHosted, code: str, stop: HardStop
) -> bool:
    """Runs the job of one client. Returns True if the host must stop."""
    conn.settimeout(2.0)
    try:
        if warm.peer_uid(conn) != os.getuid():
            conn.close()
            return False
        # A thread reads it, so a client that stalls cannot stop the event loop.
        request, fds = await asyncio.to_thread(warm.receive_request, conn)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        log.warning("the host refused a request: %s", type(error).__name__)
        conn.close()
        return False
    conn.setblocking(False)
    if request.get("code") != code:
        # Other code than the client: stop, so the client starts a new server.
        await _reply(conn, warm.REPLY_STALE + b"\n")
        _close_all(fds)
        conn.close()
        return True
    job = HostedJob(request=request, fds=fds)
    gone = asyncio.create_task(_wait_for_close(conn))
    work = asyncio.create_task(run(job))
    busy = asyncio.create_task(_refuse_while_busy(listener))
    status = 1
    try:
        await asyncio.wait({work, gone}, return_when=asyncio.FIRST_COMPLETED)
        if not work.done():
            # The client closed its socket: it was killed, or it gave up. Cancel the job.
            job.cancel()
            done, _ = await asyncio.wait({work}, timeout=CANCEL_LIMIT_S)
            if not done:
                log.warning("the job did not stop after the client left; the host exits")
                stop()
                return True
        status = _status(work)
    finally:
        gone.cancel()
        busy.cancel()
        if not work.done():
            work.cancel()
        _close_all(job.fds)
    await _reply(conn, warm.REPLY_EXIT + str(status).encode() + b"\n")
    conn.close()
    return False


async def _refuse_while_busy(listener: HostListener) -> None:
    """Answers a client that comes while a job runs: the host is busy, so the client uses the
    warm server. The client has sent its request, and the host closes the files of it."""
    loop = asyncio.get_running_loop()
    while True:
        conn, _ = await loop.sock_accept(listener.sock)
        conn.settimeout(2.0)
        try:
            if warm.peer_uid(conn) == os.getuid():
                _, fds = await asyncio.to_thread(warm.receive_request, conn)
                _close_all(fds)
                conn.sendall(warm.REPLY_BUSY + b"\n")
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        finally:
            conn.close()


def _status(work: asyncio.Task[int]) -> int:
    try:
        return work.result()
    except asyncio.CancelledError:
        return 1
    except Exception:
        log.exception("the hosted job raised")
        return 1


async def _wait_for_close(conn: socket.socket) -> None:
    """Returns when the client closes its end of the socket."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            data = await loop.sock_recv(conn, 1)
        except OSError:
            return
        if not data:
            return


async def _reply(conn: socket.socket, data: bytes) -> None:
    loop = asyncio.get_running_loop()
    with contextlib.suppress(OSError):
        await loop.sock_sendall(conn, data)


def _close_all(fds: list[int]) -> None:
    for fd in fds:
        with contextlib.suppress(OSError):
            os.close(fd)
