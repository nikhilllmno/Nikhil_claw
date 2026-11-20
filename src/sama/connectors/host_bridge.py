"""The bridge of a connector host (docs/architecture.md, Section 10.3.2).

Sama copies this file into the connector host and runs it with ``python3 -u bridge.py
<config>``. It must use the standard library only, because the host has no sama package.
The control plane never imports it.

The bridge:

1. stops the server of an earlier bridge (after a resume, its process can still run);
2. installs the pinned package (``pypi``: a virtual environment; ``npm``: ``npx`` does it);
3. starts the server in its own process group. The server uses the stdin and stdout of the
   bridge, so the control plane speaks MCP to it directly. The bridge never writes stdout;
4. watches the memory of the server and its children. Above the limit, it stops them and
   exits with ``MEMORY_EXIT``. The next call starts a new bridge.

The config has no secrets. The secrets are environment variables of the bridge, and the
server gets them. The bridge never writes them.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

MEMORY_EXIT = 86
SETUP_EXIT = 87
STATE = Path.home() / ".sama"
PIDS = STATE / "server.pid"
INTERVAL_S = 1.0


def log(text: str) -> None:
    sys.stderr.write(f"sama-bridge: {text}\n")
    sys.stderr.flush()


def stop_old() -> None:
    """Stops the server and the bridge of an earlier start, if they still run."""
    try:
        bridge_pid, group = (int(x) for x in PIDS.read_text().split())
    except (OSError, ValueError):
        return
    if bridge_pid != os.getpid() and _is_bridge(bridge_pid):
        _kill(lambda: os.kill(bridge_pid, signal.SIGKILL))
    if group > 1:
        _kill(lambda: os.killpg(group, signal.SIGKILL))
    PIDS.unlink(missing_ok=True)


def _is_bridge(pid: int) -> bool:
    try:
        return b"bridge.py" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False


def _kill(action: Callable[[], None]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        action()


def program(config: dict[str, object]) -> list[str]:
    """The argv of the server. ``pypi`` installs the package in a virtual environment first."""
    command = config.get("command")
    if isinstance(command, list):
        return [str(part) for part in command]
    registry = config["registry"]
    name, version = str(config["name"]), str(config["version"])
    raw = config.get("args")
    args = [str(arg) for arg in raw] if isinstance(raw, list) else []
    binary = config.get("bin")
    if registry == "npm":
        if binary:
            return ["npx", "-y", "-p", f"{name}@{version}", str(binary), *args]
        return ["npx", "-y", f"{name}@{version}", *args]
    key = hashlib.sha256(f"{name}=={version}".encode()).hexdigest()[:12]
    venv = STATE / f"venv-{key}"
    ready = venv / ".sama-ready"
    if not ready.exists():
        log(f"installing {name}=={version}")
        steps = (
            [sys.executable, "-m", "venv", str(venv)],
            [
                str(venv / "bin" / "pip"),
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--quiet",
                f"{name}=={version}",
            ],
        )
        for step in steps:
            # The setup output goes to stderr: stdout is for MCP only.
            if subprocess.run(step, stdout=sys.stderr, stdin=subprocess.DEVNULL).returncode:  # noqa: S603
                log("the install failed")
                sys.exit(SETUP_EXIT)
        ready.write_text(version)
    return [str(venv / "bin" / str(binary or name)), *args]


def group_rss_kb(session: int) -> int:
    """The resident memory of all processes of the session of the server, in KiB."""
    total = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
            fields = stat[stat.rindex(")") + 2 :].split()
            if int(fields[3]) != session:  # The field after ppid, pgrp is the session id.
                continue
            for line in Path(f"/proc/{entry}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1])
                    break
        except (OSError, ValueError, IndexError):
            continue
    return total


def main() -> int:
    config = json.loads(sys.argv[1])
    limit_kb = int(config["memory_mb"]) * 1024
    STATE.mkdir(parents=True, exist_ok=True)
    stop_old()
    argv = program(config)
    try:
        server = subprocess.Popen(argv, start_new_session=True)  # noqa: S603
    except OSError as error:
        log(f"the server did not start: {type(error).__name__}")
        return SETUP_EXIT
    PIDS.write_text(f"{os.getpid()} {server.pid}")

    def stop(*_: object) -> None:
        _kill(lambda: os.killpg(server.pid, signal.SIGKILL))
        sys.exit(143)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    while True:
        code = server.poll()
        if code is not None:
            _kill(lambda: os.killpg(server.pid, signal.SIGKILL))
            return code if code >= 0 else 128 - code
        used = group_rss_kb(server.pid)
        if used > limit_kb:
            log(f"the server used {used // 1024} MB, more than {limit_kb // 1024} MB; stopping it")
            _kill(lambda: os.killpg(server.pid, signal.SIGKILL))
            server.wait()
            return MEMORY_EXIT
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    sys.exit(main())
