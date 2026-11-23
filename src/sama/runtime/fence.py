"""The process fence of a kept runner (docs/architecture.md, Section 4.4, "Process fence").

A kept runner lives for more than one run. A process that a run left behind (a shell command
that started work in the background, a tool that forked) must not live into the next run: it
could call the token relay, or read the token file, while the next run's token is active.

The runner process is a *child subreaper*: a process whose parent exits becomes a child of the
runner instead of a child of ``init``. So every process that a run started, also one that
detached from its shell, stays a descendant of the runner and can be found in ``/proc``.

``kill_descendants`` kills and reaps every descendant of a process, except some trusted
processes (the kept CLI itself). It runs at the end of each run, and before a kept client is
used again and before the next token is active. A caller that finds a process to kill must not
keep the clients of the run: the run was not clean.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import signal
import time
from collections.abc import Collection
from typing import Final

log: Final = logging.getLogger("sama.fence")

PR_SET_CHILD_SUBREAPER: Final = 36
KILL_TIMEOUT_S: Final = 3.0
"""The longest time to kill and reap the descendants of a process."""
_POLL_S: Final = 0.01


class FenceError(Exception):
    """A descendant did not stop in ``KILL_TIMEOUT_S``. The runner must not go on."""


class KeepError(Exception):
    """A kept client did not stop in time. A new CLI must not start on the same SDK session
    while the old one can still write to it, so the run fails (retryable) and the runner
    stops."""


def set_subreaper() -> bool:
    """Makes this process the parent of the orphans of its descendants. ``False`` if the
    system does not allow it (then the fence sees only the process tree)."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return int(libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)) == 0
    except (OSError, AttributeError):
        return False


def _table() -> dict[int, tuple[int, str]]:
    """The process table: ``pid -> (parent pid, state)``, read from ``/proc``."""
    table: dict[int, tuple[int, str]] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        with contextlib.suppress(OSError, ValueError, IndexError):
            with open(f"/proc/{name}/stat", "rb") as handle:
                raw = handle.read().decode(errors="replace")
            # ``pid (name) S ppid ...``. The name can hold spaces and brackets.
            rest = raw[raw.rindex(")") + 2 :].split()
            table[int(name)] = (int(rest[1]), rest[0])
    return table


def descendants(root: int) -> dict[int, str]:
    """Every descendant of ``root`` (children, their children, and so on): ``pid -> state``."""
    table = _table()
    children: dict[int, list[int]] = {}
    for pid, (parent, _) in table.items():
        children.setdefault(parent, []).append(pid)
    found: dict[int, str] = {}
    todo = list(children.get(root, ()))
    while todo:
        pid = todo.pop()
        if pid in found or pid == root:
            continue
        found[pid] = table[pid][1]
        todo.extend(children.get(pid, ()))
    return found


def _reap(pid: int) -> None:
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, os.WNOHANG)


def kill_descendants(
    root: int, trusted: Collection[int] = (), *, timeout_s: float = KILL_TIMEOUT_S
) -> list[int]:
    """Kills and reaps every descendant of ``root`` that is not in ``trusted``, and returns the
    pids that it found. An empty list means that there was nothing to kill.

    A trusted process stays, but its own descendants do not: the kept CLI has no child between
    two runs. The function looks again after each kill, because a process can start another
    one while it dies. It raises ``FenceError`` if a process does not stop in ``timeout_s``.
    """
    deadline = time.monotonic() + timeout_s
    found: list[int] = []
    while True:
        alive = {pid: state for pid, state in descendants(root).items() if pid not in trusted}
        for pid, state in alive.items():
            if pid not in found:
                found.append(pid)
            if state != "Z":
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
            _reap(pid)
        # A zombie has no code and no files. Its parent reaps it, and a trusted parent that does
        # not wait leaves it, so it does not hold the fence.
        running = [pid for pid, state in alive.items() if state != "Z"]
        if not running:
            return found
        if time.monotonic() >= deadline:
            raise FenceError(f"{len(running)} process(es) did not stop")
        time.sleep(_POLL_S)


def kill_all(root: int, *, timeout_s: float = KILL_TIMEOUT_S) -> list[int]:
    """Kills and reaps every descendant of ``root``, the kept CLI too. For a runner that stops."""
    return kill_descendants(root, (), timeout_s=timeout_s)


def exited(pid: int) -> bool:
    """True if the process is gone or only a zombie."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read().decode(errors="replace")
    except OSError:
        return True
    return raw[raw.rindex(")") + 2 :].split()[0] == "Z"
