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
import types
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

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


async def test_a_session_warm_makes_the_first_run_warm(
    runtime: SandboxedRuntime, socket_path: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="sama.orchestrator")
    assert await runtime.warm("org-1", "sess-1") == "ready"
    await asyncio.to_thread(_wait_until, lambda: Path(socket_path).exists())
    # A second warm is safe: the second server exits, and one server stays.
    assert await runtime.warm("org-1", "sess-1") == "ready"
    events = await _run(runtime, _reply("one"), "run-1")
    assert isinstance(events[-1], RunCompleted)
    assert _modes(caplog) == ["mode=warm"]
    await asyncio.to_thread(_wait_until, lambda: len(warm_servers(socket_path)) == 1)


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
    # The watcher is a fork of the server (same command line), and it ends after the runner.
    _wait_until(lambda: len(warm_servers(socket_path)) == 1, timeout_s=5)


_LOST_REPLY_CLIENT = """
import sys, time
from sama import warm

def lost(_):
    raise ConnectionResetError

warm._read_reply = lost
code = warm.run_warm(sys.argv[1], "code", time.time())
sys.exit(99 if code is None else code)
"""


def test_a_lost_reply_does_not_run_the_job_again(tmp_path: Path) -> None:
    path = str(tmp_path / "s")
    errors: list[BaseException] = []
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(path)
        server.listen()
        server.settimeout(20)

        def take() -> None:
            try:
                conn, _ = server.accept()
                with conn:
                    # Read the whole request, then drop the link without a reply.
                    data, fds, _, _ = socket.recv_fds(conn, 65536, 3)
                    for fd in fds:
                        os.close(fd)
                    (size,) = warm._HEADER.unpack_from(data)
                    while len(data) < warm._HEADER.size + size:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=take)
        thread.start()
        # A separate client process with known standard files and a small environment.
        client = subprocess.run(
            [sys.executable, "-c", _LOST_REPLY_CLIENT, path],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": os.pathsep.join(sys.path)},
            timeout=30,
            check=False,
        )
        thread.join(timeout=5)
    assert errors == []
    assert client.returncode == 1, client.stderr.decode()
    assert b"the connection to the warm runner failed" in client.stderr


# -- the host (docs/architecture.md, Section 4.4) -----------------------------------------------


def test_a_host_that_is_busy_gives_the_job_to_the_warm_server(socket_path: str) -> None:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    server.listen(1)

    def answer() -> None:
        conn, _ = server.accept()
        socket.recv_fds(conn, 65536, 3)
        conn.sendall(warm.REPLY_BUSY + b"\n")
        conn.close()

    thread = threading.Thread(target=answer)
    thread.start()
    try:
        assert warm.run_warm(socket_path, warm.code_id(), time.time()) is None
    finally:
        thread.join(timeout=5)
        server.close()


def test_a_runner_that_releases_itself_stays_alive_and_the_watcher_gets_its_exit_code() -> None:
    status_read, status_write = os.pipe()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import os, time; os.write({status_write}, b'D5\\n'); time.sleep(30)",
        ],
        pass_fds=[status_write],
    )
    os.close(status_write)
    conn, other = socket.socketpair()
    try:
        assert warm._wait_or_kill(conn, child.pid, status_read) == (5, True)
        assert child.poll() is None  # the watcher does not kill a host
    finally:
        child.kill()
        child.wait(timeout=5)
        for fd in (status_read,):
            os.close(fd)
        conn.close()
        other.close()


def test_a_runner_that_exits_gives_its_own_exit_code_and_is_not_released() -> None:
    status_read, status_write = os.pipe()
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(3)"], pass_fds=[status_write]
    )
    os.close(status_write)
    conn, other = socket.socketpair()
    try:
        assert warm._wait_or_kill(conn, child.pid, status_read) == (3, False)
    finally:
        os.close(status_read)
        conn.close()
        other.close()


def test_the_client_reads_the_keep_time_from_its_environment() -> None:
    assert warm._keep_seconds({warm.KEEP_ENV: "600"}) == 600.0
    assert warm._keep_seconds({}) == 0.0
    assert warm._keep_seconds({warm.KEEP_ENV: "-3"}) == 0.0
    assert warm._keep_seconds({warm.KEEP_ENV: "x"}) == 0.0


def test_a_released_runner_tells_the_watcher_its_exit_code() -> None:
    read, write = os.pipe()
    handle = warm.HostHandle("/x.host", "code", write)
    handle.release(4)
    assert os.read(read, 64) == b"D4\n"
    os.close(read)


def test_the_server_reads_the_cli_file_once_for_the_disk_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "claude_agent_sdk"
    (folder / "_bundled").mkdir(parents=True)
    binary = folder / "_bundled" / "claude"
    binary.write_bytes(b"x" * 3_000_000)
    opened: list[str] = []
    real_open = open

    def spy(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    module = types.SimpleNamespace(__file__=str(folder / "__init__.py"))
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    monkeypatch.setattr("builtins.open", spy)
    warm._read_cli_binary()
    assert str(binary) in opened


def test_a_server_without_the_claude_sdk_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "claude_agent_sdk", raising=False)
    warm._read_cli_binary()  # no error
