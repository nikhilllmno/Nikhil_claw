"""A child process with pipes, for the providers that start a local program."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from sama.sandbox.base import STDERR_TAIL_LINES, SandboxError

_STREAM_LIMIT: Final = 16 * 1024 * 1024
"""The longest stdout line. An event with a large tool output is one line."""


@dataclass
class SubprocessProcess:
    """A child process with pipes. ``argv[0]`` is the program."""

    _process: asyncio.subprocess.Process
    _stderr: deque[str]
    _stderr_task: asyncio.Task[None]

    @classmethod
    async def start(
        cls, argv: Sequence[str], env: Mapping[str, str], cwd: str | None = None
    ) -> SubprocessProcess:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env),
            cwd=cwd,
            limit=_STREAM_LIMIT,
            start_new_session=True,
        )
        tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        task = asyncio.create_task(_drain(process, tail))
        return cls(process, tail, task)

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def write(self, data: str) -> None:
        stdin = self._process.stdin
        if stdin is None or stdin.is_closing():
            raise SandboxError("the process stdin is closed")
        try:
            stdin.write(data.encode())
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            raise SandboxError("the process ended") from None

    async def close_stdin(self) -> None:
        stdin = self._process.stdin
        if stdin is not None and not stdin.is_closing():
            stdin.close()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await stdin.wait_closed()

    async def lines(self) -> AsyncIterator[str]:
        stdout = self._process.stdout
        if stdout is None:
            return
        while True:
            try:
                raw = await stdout.readline()
            except ValueError:
                raise SandboxError("a stdout line is too long") from None
            if not raw:
                return
            yield raw.decode(errors="replace").rstrip("\n")

    async def wait(self) -> int:
        code = await self._process.wait()
        with contextlib.suppress(asyncio.CancelledError):
            await self._stderr_task
        return code

    async def kill(self) -> None:
        """Kills the process group, so the children of the program stop too."""
        if self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self._process.pid, signal.SIGKILL)
        await self._process.wait()

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)


async def _drain(process: asyncio.subprocess.Process, tail: deque[str]) -> None:
    if process.stderr is None:
        return
    async for raw in process.stderr:
        tail.append(raw.decode(errors="replace").rstrip("\n"))


async def run_command(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    stdin: bytes | None = None,
    timeout_s: float = 60,
) -> tuple[int, bytes, bytes]:
    """Runs a program to the end. Returns the exit code, stdout, and stderr.

    ``env=None`` gives the program the environment of this process. Raises ``SandboxError``
    if the program does not end in ``timeout_s``. On a timeout or a cancel, it kills the
    program.
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=None if env is None else dict(env),
        cwd=cwd,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(stdin), timeout=timeout_s)
    except TimeoutError:
        await _reap(process)
        raise SandboxError(f"the command timed out after {timeout_s} s") from None
    except asyncio.CancelledError:
        await _reap(process)
        raise
    return process.returncode or 0, out, err


async def _reap(process: asyncio.subprocess.Process) -> None:
    """Kills the program and waits for it, so no process is left behind."""
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    await asyncio.shield(process.wait())
