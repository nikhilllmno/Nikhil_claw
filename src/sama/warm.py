"""The warm runner: start the runner in a process that has its imports already.

Start it with ``python -m sama.warm`` instead of ``python -m sama.runtime.runner``
(docs/architecture.md, Section 16.2). The protocol on stdin, stdout, and stderr does not
change. This module imports only the standard library, so the client starts fast.

- The client connects to the warm server on a Unix socket (``SAMA_RUNNER_SOCKET``, else
  ``/tmp/sama-runner.sock``). It sends its stdin, stdout, and stderr, its environment, and its
  work folder. Then it waits for the exit code and exits with it.
- The warm server imports the runner and the Claude runtime once. For each client, it forks a
  watcher. The watcher forks the runner process. The runner process uses the files and the
  environment of the client, so it is the same as a cold runner.
- When the client stops (for example, the orchestrator kills it), the watcher kills the runner
  and all its child processes.
- If there is no warm server, or it has other code, the client starts a new warm server for
  the next run and runs this job in its own process (cold).

The warm server never gets the run token: it starts with a short list of variables, and only
the forked processes get the environment of a client.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import random
import select
import signal
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Mapping
from typing import Any, Final, NoReturn

SOCKET_ENV: Final = "SAMA_RUNNER_SOCKET"
DEFAULT_SOCKET: Final = "/tmp/sama-runner.sock"  # noqa: S108 -- one user in the sandbox
SERVER_ENV: Final = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "VIRTUAL_ENV",
    "PYTHONPATH",
    "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE",
)
"""The only variables that the warm server gets from the client that starts it."""
PRELOAD: Final = (
    "sama.runtime.runner",
    "sama.runtime.claude",
    "sama.runtime.claude_transcript",
    "sama.runtime.remote_gate",
    "sama.runtime.gating",
    "sama.runtime.session_items",
    "sama.runtime.openai_agents",
    "sama.runtime.archive",
)
"""The modules that the warm server imports before the first fork."""
CONNECT_TIMEOUT_S: Final = 2.0
SERVER_LOCK_WAIT_S: Final = 3.0
"""A new warm server waits this long for a stale one to stop."""
_HEADER: Final = struct.Struct("!I")
_MAX_REQUEST: Final = 4 * 1024 * 1024
_STALE: Final = b"S"
_EXIT: Final = b"X"


def code_id() -> str:
    """An id of the Python and the sama code. A warm server with another id is stale."""
    digest = hashlib.sha256(f"{sys.executable}\0{sys.version}".encode())
    root = os.path.dirname(os.path.abspath(__file__))
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            if name.endswith(".py"):
                stat = os.stat(os.path.join(folder, name))
                digest.update(f"{folder}/{name}\0{stat.st_mtime_ns}\0{stat.st_size}\0".encode())
    return digest.hexdigest()


# -- client ----------------------------------------------------------------------------------


def _log(message: str) -> None:
    sys.stderr.write(f"INFO:sama.warm:{message}\n")
    sys.stderr.flush()


def run_warm(path: str, code: str, started: float) -> int | None:
    """Runs the job in the warm server. Returns the exit code, or None if no warm server with
    this code takes the job. Then the job is not started and stdin is not read."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(CONNECT_TIMEOUT_S)
        try:
            sock.connect(path)
        except OSError:
            return None
        request = json.dumps(
            {"code": code, "env": dict(os.environ), "cwd": os.getcwd(), "started": started}
        ).encode()
        data = _HEADER.pack(len(request)) + request
        try:
            sent = socket.send_fds(sock, [data], [0, 1, 2])
            sock.sendall(data[sent:])
        except OSError:
            # The server reads the whole request before it starts a runner, so no job started.
            return None
        sock.settimeout(None)
        try:
            reply = _read_reply(sock)
        except OSError as error:
            # The server has the job, so the runner can have started. A cold run now could do
            # the work of the job two times.
            _log(f"the connection to the warm runner failed: {type(error).__name__}")
            return 1
        if reply == _STALE:
            return None
        if reply.startswith(_EXIT):
            with contextlib.suppress(ValueError):
                return int(reply[1:])
        # The watcher stopped without an exit code. The job can have started, so it does not
        # run again.
        _log("the warm runner stopped without an exit code")
        return 1
    finally:
        sock.close()


def _read_reply(sock: socket.socket) -> bytes:
    reply = b""
    while not reply.endswith(b"\n"):
        chunk = sock.recv(64)
        if not chunk:
            break
        reply += chunk
    return reply.strip()


def start_server(path: str) -> None:
    """Starts a warm server for the next run. It is a new session, so it outlives this
    process."""
    env = {name: os.environ[name] for name in SERVER_ENV if name in os.environ}
    with contextlib.suppress(OSError):
        subprocess.Popen(  # noqa: S603 -- our own interpreter and module
            [sys.executable, "-m", "sama.warm", "--server", path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd="/",
            start_new_session=True,
            close_fds=True,
        )


def client_main() -> NoReturn:
    started = time.time()
    path = os.environ.get(SOCKET_ENV) or DEFAULT_SOCKET
    code = code_id()
    exit_code = run_warm(path, code, started)
    if exit_code is not None:
        raise SystemExit(exit_code)
    start_server(path)
    _log("no warm runner; this run is cold")
    from sama.runtime import runner

    runner.STARTED_AT = started
    runner.main()
    raise SystemExit(0)  # main always raises SystemExit


# -- server ----------------------------------------------------------------------------------


def server_main(path: str) -> NoReturn:
    """Imports the runtime, then forks a watcher for each client. One server for each socket:
    a second server exits."""
    os.umask(0o077)
    lock = os.open(path + ".lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    deadline = time.monotonic() + SERVER_LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os._exit(0)  # another warm server runs
            time.sleep(0.05)
    import importlib

    for module in PRELOAD:
        with contextlib.suppress(ImportError):
            importlib.import_module(module)
    code = code_id()
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(16)
    # The kernel reaps the watchers. Each watcher sets the default back for its runner.
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    while True:
        try:
            conn, _ = server.accept()
        except InterruptedError:
            continue
        try:
            pid = os.fork()
        except OSError:
            conn.close()
            continue
        if pid == 0:
            server.close()
            os.close(lock)
            signal.signal(signal.SIGCHLD, signal.SIG_DFL)
            exit_code = 1
            try:
                exit_code = _watch(conn, code)
            finally:
                os._exit(exit_code)
        conn.close()


def _peer_uid(conn: socket.socket) -> int:
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return int(struct.unpack("3i", creds)[1])


def _receive(conn: socket.socket) -> tuple[dict[str, Any], list[int]]:
    data, fds, _, _ = socket.recv_fds(conn, 65536, 3)
    if len(fds) != 3 or len(data) < _HEADER.size:
        raise ValueError("the request has no files or no header")
    (size,) = _HEADER.unpack_from(data)
    if size > _MAX_REQUEST:
        raise ValueError("the request is too large")
    while len(data) < _HEADER.size + size:
        chunk = conn.recv(65536)
        if not chunk:
            raise ValueError("the request is not complete")
        data += chunk
    request = json.loads(data[_HEADER.size : _HEADER.size + size])
    if not isinstance(request, dict):
        raise ValueError("the request is not an object")
    return request, fds


def _watch(conn: socket.socket, code: str) -> int:
    """Starts the runner for one client and waits. Kills the runner if the client stops."""
    if _peer_uid(conn) != os.getuid():
        return 1
    request, fds = _receive(conn)
    if request.get("code") != code:
        conn.sendall(_STALE + b"\n")
        # The server has other code than the client: stop it, so the client starts a new one.
        with contextlib.suppress(OSError):
            os.kill(os.getppid(), signal.SIGTERM)
        return 0
    pid = os.fork()
    if pid == 0:
        conn.close()
        _become_runner(request, fds)
    # The runner gets its own process group. The child does the same, and the first call wins,
    # so the group exists before the watcher can kill it.
    with contextlib.suppress(OSError):
        os.setpgid(pid, pid)
    for fd in fds:
        os.close(fd)
    status = _wait_or_kill(conn, pid)
    _kill(pid)  # child processes that the runner left
    with contextlib.suppress(OSError):
        conn.sendall(_EXIT + str(status).encode() + b"\n")
    return 0


def _kill(pid: int) -> None:
    """Kills the process group of the runner, and the runner itself."""
    for kill in (os.killpg, os.kill):
        with contextlib.suppress(OSError):
            kill(pid, signal.SIGKILL)


def _wait_or_kill(conn: socket.socket, pid: int) -> int:
    """Waits for the runner. If the client closes the socket first, kills the runner."""
    pidfd: int | None = None
    with contextlib.suppress(OSError, AttributeError):
        pidfd = os.pidfd_open(pid)
    watched = [conn.fileno()] + ([pidfd] if pidfd is not None else [])
    while True:
        timeout = 0.1 if pidfd is None or conn.fileno() not in watched else None
        ready, _, _ = select.select(watched, [], [], timeout)
        if conn.fileno() in ready and not conn.recv(1):
            watched.remove(conn.fileno())
            _kill(pid)
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            if pidfd is not None:
                os.close(pidfd)
            code = os.waitstatus_to_exitcode(status)
            return code if code >= 0 else 128 - code


def _become_runner(request: Mapping[str, Any], fds: list[int]) -> NoReturn:
    """In the runner process: take the files, the environment, and the work folder of the
    client, then run the runner."""
    exit_code = 1
    try:
        # Its own process group, so the watcher can kill the runner and its child processes.
        # Not ``setsid``: the watcher can make the group first, and then ``setsid`` fails.
        with contextlib.suppress(OSError):
            os.setpgid(0, 0)
        for target, fd in enumerate(fds):
            os.dup2(fd, target)
            if fd != target:
                os.close(fd)
        env = request["env"]
        os.environ.clear()
        os.environ.update({str(k): str(v) for k, v in env.items()})
        os.chdir(str(request["cwd"]))
        random.seed()
        from sama.runtime import runner

        runner.STARTED_AT = float(request["started"])
        runner.MODE = "warm"
        try:
            runner.main()
        except SystemExit as stop:
            exit_code = stop.code if isinstance(stop.code, int) else 1
    except BaseException:  # this process must exit with os._exit
        import traceback

        traceback.print_exc()
    finally:
        with contextlib.suppress(Exception):
            sys.stdout.flush()
            sys.stderr.flush()
        os._exit(exit_code)


def main(argv: list[str]) -> NoReturn:
    if len(argv) == 2 and argv[0] == "--server":
        server_main(argv[1])
    client_main()


if __name__ == "__main__":
    main(sys.argv[1:])
