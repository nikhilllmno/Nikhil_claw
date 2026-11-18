"""Tests of the host process helpers in ``sama.sandbox.process``."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from sama.sandbox.base import SandboxError
from sama.sandbox.process import run_command


def _pid_from(path: Path) -> int:
    """Waits up to 5 s for the program to write its pid. The program runs on its own."""
    deadline = time.monotonic() + 5
    while not path.exists() or not path.read_text().strip():
        assert time.monotonic() < deadline, "the program did not start"
        time.sleep(0.05)
    return int(path.read_text())


def _alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


async def test_run_command_returns_the_output() -> None:
    code, out, err = await run_command(["sh", "-c", "echo out; echo err >&2; exit 3"])
    assert (code, out, err) == (3, b"out\n", b"err\n")


async def test_run_command_kills_the_program_on_a_timeout(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    script = 'echo $$ > "$0"; exec sleep 30'
    with pytest.raises(SandboxError, match="timed out"):
        await run_command(["sh", "-c", script, str(pid_file)], timeout_s=0.5)
    assert not _alive(_pid_from(pid_file))


async def test_run_command_kills_the_program_on_a_cancel(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    script = 'echo $$ > "$0"; exec sleep 30'
    task = asyncio.ensure_future(run_command(["sh", "-c", script, str(pid_file)]))
    await asyncio.sleep(0.3)  # the loop starts the program
    pid = _pid_from(pid_file)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _alive(pid)
