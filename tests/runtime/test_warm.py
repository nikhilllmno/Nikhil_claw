"""Tests of the warm runner (src/sama/warm.py) with the local provider and the fake runtime."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import pytest

from sama import warm
from sama.orchestrator.sandboxed import SandboxedRuntime, SandboxSettings
from sama.runtime import (
    FailureCode,
    RunCompleted,
    RunConfig,
    RunFailed,
    RunRequest,
    RuntimeKind,
    TextDelta,
    Usage,
    validate_event_stream,
)
from sama.runtime.fake import FakeScript
from sama.runtime.runner import RUN_TOKEN_ENV, WORKDIR_ENV, RunnerJob
from sama.sandbox.local import LocalProvider
from sama.tenancy import RunTokenSigner
from tests.support import collect, make_scope, warm_servers, warm_socket

KEY = b"k" * 32
WARM = (sys.executable, "-m", "sama.warm")
CONFIG = RunConfig(runtime=RuntimeKind.FAKE, model="fake")


def _in_folder(folder: Path) -> list[int]:
    """The process ids of the processes that run in ``folder``."""
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            with contextlib.suppress(OSError):
                if os.readlink(entry / "cwd") == str(folder):
                    found.append(int(entry.name))
    return found


def _wait_until(check: Callable[[], bool], timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("the condition did not come true in time")
        time.sleep(0.05)


@pytest.fixture
def socket_path() -> Iterator[str]:
    with warm_socket() as path:
        yield path


@pytest.fixture
async def runtime(tmp_path: Path, socket_path: str) -> AsyncIterator[SandboxedRuntime]:
    sandboxed = SandboxedRuntime(
        kind=RuntimeKind.FAKE,
        provider=LocalProvider(tmp_path / "sandboxes"),
        signer=RunTokenSigner(KEY),
        settings=SandboxSettings(
            template="local", runner_argv=WARM, extra_env={warm.SOCKET_ENV: socket_path}
        ),
    )
    yield sandboxed
    await sandboxed.close_sandboxes(kill=True)


def _reply(text: str) -> FakeScript:
    usage = Usage(output_tokens=1)
    return FakeScript(
        events=(TextDelta(text=text, usage=usage), RunCompleted(output=text, usage=usage))
    )


def _slow() -> FakeScript:
    words = [TextDelta(text="w ", usage=Usage(output_tokens=i)) for i in range(1, 101)]
    return FakeScript(events=(*words, RunCompleted(output="x", usage=words[-1].usage)), delay_s=0.1)


async def _run(runtime: SandboxedRuntime, script: FakeScript, run_id: str) -> list[object]:
    runtime.fake_scripts.append(script)
    scope = make_scope(run_id)
    events = await collect(runtime.run(RunRequest(scope=scope, input="hi", config=CONFIG)))
    await runtime.wait_for_cleanup()
    return list(events)


def _modes(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage().split()[2]
        for r in caplog.records
        if r.getMessage().startswith("runner timing mode=")
    ]


async def test_the_first_run_is_cold_and_the_next_runs_are_warm(
    runtime: SandboxedRuntime, socket_path: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="sama.orchestrator")
    first = await _run(runtime, _reply("one"), "run-1")
    await asyncio.to_thread(_wait_until, lambda: Path(socket_path).exists())
    second = await _run(runtime, _reply("two"), "run-2")
    third = await _run(runtime, _reply("three"), "run-3")
    for events, text in ((first, "one"), (second, "two"), (third, "three")):
        assert isinstance(events[-1], RunCompleted)
        assert events[-1].output == text
    assert _modes(caplog) == ["mode=cold", "mode=warm", "mode=warm"]
    assert len(warm_servers(socket_path)) == 1


async def test_a_cancel_stops_a_warm_runner(runtime: SandboxedRuntime, socket_path: str) -> None:
    await _run(runtime, _reply("one"), "run-1")
    await asyncio.to_thread(_wait_until, lambda: Path(socket_path).exists())
    runtime.fake_scripts.append(_slow())
    scope = make_scope("run-2")
    stream = runtime.run(RunRequest(scope=scope, input="hi", config=CONFIG))
    events = [await anext(stream)]
    async with asyncio.timeout(10):
        await runtime.cancel(scope.run_id)
        events.extend([e async for e in stream])
    validate_event_stream(events)
    assert isinstance(events[-1], RunFailed)
    assert events[-1].code is FailureCode.CANCELLED


def _start_server(socket_path: str) -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PYTHONPATH", os.pathsep.join(sys.path))
        warm.start_server(socket_path)
    _wait_until(lambda: Path(socket_path).exists())


def test_a_killed_client_kills_its_runner(tmp_path: Path, socket_path: str) -> None:
    _start_server(socket_path)
    work = tmp_path / "work"
    work.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        warm.SOCKET_ENV: socket_path,
        WORKDIR_ENV: str(work),
        RUN_TOKEN_ENV: "token-1",
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    job = RunnerJob(
        op="run",
        run=RunRequest(scope=make_scope("run-1"), input="hi", config=CONFIG),
        fake_scripts=(_slow(),),
    )
    client = subprocess.Popen(
        WARM,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=work,
        text=True,
    )
    assert client.stdin is not None
    assert client.stdout is not None
    client.stdin.write(job.model_dump_json() + "\n")
    client.stdin.flush()
    assert '"text_delta"' in client.stdout.readline()
    assert len(_in_folder(work)) == 2  # the client and the runner
    client.kill()
    client.wait()
    _wait_until(lambda: not _in_folder(work))
    assert len(warm_servers(socket_path)) == 1


def test_a_server_with_other_code_stops_and_takes_no_job(socket_path: str) -> None:
    _start_server(socket_path)
    assert len(warm_servers(socket_path)) == 1
    assert warm.run_warm(socket_path, "other code", time.time()) is None
    _wait_until(lambda: not warm_servers(socket_path))


def test_no_server_means_no_warm_run(socket_path: str) -> None:
    assert warm.run_warm(socket_path, warm.code_id(), time.time()) is None


def test_the_server_gets_only_the_listed_variables(
    socket_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RUN_TOKEN_ENV, "token-1")
    _start_server(socket_path)
    [server] = warm_servers(socket_path)
    environ = Path(f"/proc/{server}/environ").read_bytes().split(b"\0")
    names = {item.split(b"=", 1)[0].decode() for item in environ if item}
    assert names <= set(warm.SERVER_ENV)
    assert RUN_TOKEN_ENV not in names


def test_a_second_server_on_the_same_socket_exits(socket_path: str) -> None:
    _start_server(socket_path)
    second = subprocess.run(
        [sys.executable, "-m", "sama.warm", "--server", socket_path],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        timeout=10,
        check=False,
    )
    assert second.returncode == 0
    assert len(warm_servers(socket_path)) == 1


def test_the_code_id_is_stable() -> None:
    assert warm.code_id() == warm.code_id()
    assert len(warm.code_id()) == 64


def test_kill_stops_a_runner_that_has_no_process_group_yet() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    warm._kill(child.pid)  # its group does not exist, so only the direct kill works
    assert child.wait(timeout=5) == -9


def test_a_client_that_closes_at_once_leaves_no_runner(tmp_path: Path, socket_path: str) -> None:
    _start_server(socket_path)
    work = tmp_path / "work"
    work.mkdir()
    job = RunnerJob(
        op="run",
        run=RunRequest(scope=make_scope("run-1"), input="hi", config=CONFIG),
        fake_scripts=(_slow(),),
    )
    stdin_read, stdin_write = os.pipe()
    os.write(stdin_write, (job.model_dump_json() + "\n").encode())
    env = {
        "PATH": os.environ.get("PATH", ""),
        WORKDIR_ENV: str(work),
        RUN_TOKEN_ENV: "token-1",
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    request = json.dumps(
        {"code": warm.code_id(), "env": env, "cwd": str(work), "started": time.time()}
    ).encode()
    with open(os.devnull, "wb") as sink, socket.socket(socket.AF_UNIX) as sock:
        sock.connect(socket_path)
        data = warm._HEADER.pack(len(request)) + request
        sent = socket.send_fds(sock, [data], [stdin_read, sink.fileno(), sink.fileno()])
        sock.sendall(data[sent:])
    os.close(stdin_read)
    try:
        _wait_until(lambda: not _in_folder(work), timeout_s=5)
    finally:
        os.close(stdin_write)
    assert len(warm_servers(socket_path)) == 1


def test_a_lost_reply_does_not_run_the_job_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = str(tmp_path / "s")
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(path)
        server.listen()

        def take() -> None:
            conn, _ = server.accept()
            with conn:
                _, fds, _, _ = socket.recv_fds(conn, 65536, 3)
                for fd in fds:
                    os.close(fd)

        thread = threading.Thread(target=take)
        thread.start()

        def lost(_: socket.socket) -> bytes:
            raise ConnectionResetError

        monkeypatch.setattr(warm, "_read_reply", lost)
        assert warm.run_warm(path, "code", time.time()) == 1
        thread.join(timeout=5)
