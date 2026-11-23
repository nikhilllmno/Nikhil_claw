"""Tests of the runner host (src/sama/runtime/host.py): the socket protocol, the busy answer,
the idle stop, the cancel of a job when its client goes away, and the hard stop."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

from sama import warm
from sama.runtime import fence, host
from sama.runtime.host import HostedJob, HostListener, JobInput, serve_jobs

pytestmark = pytest.mark.asyncio

CODE = "code-1"


@pytest.fixture
def path() -> Iterator[str]:
    with tempfile.TemporaryDirectory(prefix="sama-") as folder:
        yield f"{folder}/h.sock"


@pytest.fixture
def listener(path: str) -> Iterator[HostListener]:
    bound = HostListener.bind(path)
    assert bound is not None
    yield bound
    bound.close()


def exists(path: str) -> bool:
    return os.path.exists(path)


def client(path: str, code: str = CODE) -> Any:
    """What ``sama.warm`` does: one call in a thread, so the host can answer."""
    return asyncio.to_thread(warm.run_warm, path, code, 0.0)


def send_and_leave(path: str) -> None:
    """A client that sends its job and then closes the socket, as a killed client does."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    request = json.dumps({"code": CODE, "env": {}, "cwd": "/", "started": 0.0}).encode()
    data = struct.pack("!I", len(request)) + request
    socket.send_fds(sock, [data], [0, 1, 2])
    sock.close()


async def test_a_job_gets_its_exit_code(path: str, listener: HostListener) -> None:
    seen: list[dict[str, Any]] = []

    async def run(job: HostedJob) -> int:
        seen.append(job.request)
        assert len(job.fds) == 3
        return 7

    server = asyncio.create_task(serve_jobs(listener, run, code=CODE, idle_s=5))
    assert await client(path) == 7
    assert seen[0]["cwd"] == os.getcwd()
    assert "env" in seen[0]
    server.cancel()
    with pytest.raises(asyncio.CancelledError):
        await server


async def test_a_second_job_waits_for_the_first_and_gets_busy(
    path: str, listener: HostListener
) -> None:
    release = asyncio.Event()

    async def run(job: HostedJob) -> int:
        await release.wait()
        return 0

    server = asyncio.create_task(serve_jobs(listener, run, code=CODE, idle_s=5))
    first = asyncio.create_task(client(path))
    await asyncio.sleep(0.2)
    # The host runs a job: the next client is told, and it uses the warm server instead.
    assert await client(path) is None
    assert not first.done()
    release.set()
    assert await first == 0
    server.cancel()
    with pytest.raises(asyncio.CancelledError):
        await server


async def test_a_client_with_other_code_stops_the_host(path: str, listener: HostListener) -> None:
    async def run(job: HostedJob) -> int:
        raise AssertionError("no job runs")

    server = asyncio.create_task(serve_jobs(listener, run, code=CODE, idle_s=5))
    assert await client(path, code="other") is None
    await asyncio.wait_for(server, timeout=2)
    assert not exists(path)


async def test_an_idle_host_stops_and_removes_its_socket(path: str, listener: HostListener) -> None:
    async def run(job: HostedJob) -> int:
        return 0

    await asyncio.wait_for(serve_jobs(listener, run, code=CODE, idle_s=0.2), timeout=3)
    assert not exists(path)
    assert await client(path) is None


async def test_the_idle_time_starts_again_after_a_job(
    path: str, listener: HostListener, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run(job: HostedJob) -> int:
        return 0

    server = asyncio.create_task(serve_jobs(listener, run, code=CODE, idle_s=0.6))
    await asyncio.sleep(0.4)
    assert await client(path) == 0
    await asyncio.sleep(0.4)  # 0.8 seconds after the start, but 0.4 after the job
    assert not server.done()
    await asyncio.wait_for(server, timeout=3)


async def test_a_host_without_its_socket_file_stops(
    path: str, listener: HostListener, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host, "WATCH_S", 0.05)

    async def run(job: HostedJob) -> int:
        return 0

    server = asyncio.create_task(serve_jobs(listener, run, code=CODE, idle_s=60))
    await asyncio.sleep(0.1)
    os.unlink(path)
    await asyncio.wait_for(server, timeout=2)


class StubInput(JobInput):
    def __init__(self) -> None:
        super().__init__(0)
        self.cancelled = asyncio.Event()

    def cancel(self) -> None:
        self.cancelled.set()


async def test_a_job_is_cancelled_when_its_client_goes_away(
    path: str, listener: HostListener
) -> None:
    ended = asyncio.Event()

    async def run(job: HostedJob) -> int:
        stub = StubInput()
        job.input = stub
        await stub.cancelled.wait()
        ended.set()
        return 1

    server = asyncio.create_task(serve_jobs(listener, run, code=CODE, idle_s=5))
    await asyncio.to_thread(send_and_leave, path)
    await asyncio.wait_for(ended.wait(), timeout=3)
    server.cancel()
    with pytest.raises(asyncio.CancelledError):
        await server


async def test_a_job_that_does_not_stop_after_the_client_left_stops_the_host(
    path: str, listener: HostListener, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host, "CANCEL_LIMIT_S", 0.2)
    stopped = threading.Event()

    async def run(job: HostedJob) -> int:
        await asyncio.sleep(60)
        return 0

    server = asyncio.create_task(
        serve_jobs(listener, run, code=CODE, idle_s=5, hard_stop=stopped.set)
    )
    await asyncio.to_thread(send_and_leave, path)
    await asyncio.wait_for(server, timeout=3)
    assert stopped.is_set()


async def test_a_second_host_cannot_take_the_socket(path: str, listener: HostListener) -> None:
    assert HostListener.bind(path) is None
    assert listener.still_bound()


async def test_the_socket_and_the_lock_are_private(path: str, listener: HostListener) -> None:
    assert os.stat(path).st_mode & 0o077 == 0
    assert os.stat(path + ".lock").st_mode & 0o077 == 0


async def test_the_host_path_belongs_to_the_warm_socket() -> None:
    assert host.host_path("/run/w.sock") == "/run/w.sock.host"
    assert host.HOST_SUFFIX == warm.HOST_SUFFIX


async def _lines(job_input: JobInput) -> AsyncIterator[str]:
    async for line in job_input.lines():
        yield line


async def test_the_input_of_a_job_has_the_lines_of_stdin_and_a_cancel_line() -> None:
    read, write = os.pipe()
    job_input = JobInput(read, owns_fd=True)
    await job_input.start()
    os.write(write, b'{"op": "run"}\n')
    iterator = _lines(job_input)
    assert json.loads(await anext(iterator)) == {"op": "run"}
    job_input.cancel()
    assert json.loads(await anext(iterator)) == {"op": "cancel"}
    os.close(write)  # the end of stdin ends the lines
    assert [line async for line in iterator] == []
    await job_input.close()


async def test_a_job_that_stops_after_the_client_left_retires_the_kept_clients(
    path: str, listener: HostListener
) -> None:
    retired = asyncio.Event()

    async def run(job: HostedJob) -> int:
        stub = StubInput()
        job.input = stub
        await stub.cancelled.wait()
        return 1

    async def retire() -> None:
        retired.set()

    server = asyncio.create_task(serve_jobs(listener, run, code=CODE, idle_s=5, retire=retire))
    await asyncio.to_thread(send_and_leave, path)
    await asyncio.wait_for(retired.wait(), timeout=3)
    server.cancel()
    with pytest.raises(asyncio.CancelledError):
        await server


async def test_the_host_stops_when_the_runner_says_it_cannot_go_on(
    path: str, listener: HostListener
) -> None:
    async def run(job: HostedJob) -> int:
        return 0

    broken = False

    def should_stop() -> bool:
        return broken

    server = asyncio.create_task(
        serve_jobs(listener, run, code=CODE, idle_s=30, should_stop=should_stop)
    )
    assert await client(path) == 0
    assert not server.done()
    broken = True
    assert await client(path) == 0
    await asyncio.wait_for(server, timeout=3)


HARD_STOP_SCRIPT = """
import asyncio, os, subprocess, sys
from sama.runtime import fence, host
from sama.runtime.runner import RunnerKeep

async def main(path):
    fence.set_subreaper()
    keep = RunnerKeep(60, "http://127.0.0.1:1")
    await keep.start()
    keep.relay.set_token("run-token")
    # A tool process of the run, and one that left its shell (it belongs to this process now).
    tool = subprocess.Popen(["sleep", "60"])
    subprocess.run(["/bin/sh", "-c", "sleep 60 &"], check=True)
    listener = host.HostListener.bind(path)
    host.CANCEL_LIMIT_S = 0.3

    async def run(job):
        await asyncio.sleep(60)  # a job that does not react to the cancel
        return 0

    folder = os.path.dirname(keep.relay.shell_prefix)
    print(tool.pid, folder, flush=True)
    await host.serve_jobs(listener, run, code="code-1", idle_s=60, hard_stop=keep.stop_now,
                          retire=keep.reset)

asyncio.run(main(sys.argv[1]))
"""


async def test_an_unresponsive_client_makes_the_host_kill_its_whole_tree_and_drop_the_token(
    path: str,
) -> None:
    process = start_script(path)
    try:
        assert process.stdout is not None
        tool_pid, folder = (await asyncio.to_thread(process.stdout.readline)).split()
        await asyncio.sleep(0.2)
        assert not fence.exited(int(tool_pid))
        assert active_token_files(folder)  # the token of the run is active
        await asyncio.to_thread(send_and_leave, path)  # the client goes away; the job never stops
        assert await asyncio.to_thread(process.wait, 8) == 1
        assert fence.exited(int(tool_pid))
        assert not active_token_files(folder)
        # The shell child that left its shell is gone too: no process of the tree is left.
        await asyncio.sleep(0.2)
        assert not any(
            b"sleep\x0060" in (entry / "cmdline").read_bytes()
            for entry in _procs_of_children(process.pid)
        )
    finally:
        process.kill()
        process.wait()


def start_script(path: str) -> Any:
    return subprocess.Popen(
        [sys.executable, "-c", HARD_STOP_SCRIPT, path],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )


def active_token_files(folder: str) -> list[str]:
    if not os.path.exists(folder):
        return []
    return [n for n in os.listdir(folder) if n.startswith(("current", "token."))]


def _procs_of_children(parent: int) -> list[Any]:
    """Processes that were children of ``parent`` and that run now (none after a hard stop)."""
    from pathlib import Path

    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            if int(stat[stat.rindex(")") + 2 :].split()[1]) == parent:
                found.append(entry)
        except (OSError, ValueError):
            continue
    return found
