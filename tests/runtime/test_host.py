"""Tests of the runner host (src/sama/runtime/host.py): the socket protocol, the busy answer,
the idle stop, the cancel of a job when its client goes away, and the hard stop."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import tempfile
import threading
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

from sama import warm
from sama.runtime import host
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
