"""The E2B sandbox provider (docs/architecture.md, Section 16.2; decision D5).

One E2B sandbox per session, from the sama template (``python -m sama.sandbox.template``).
E2B pauses an idle sandbox by itself (``lifecycle.on_timeout = "pause"``), and ``resume``
connects to it again. Files and memory stay. ``SandboxSpec.allow_out`` becomes the E2B
egress allowlist: all other outbound traffic is blocked, also when the list is empty. Only
``SandboxSpec.open_egress`` turns the block off. E2B filters host names on ports 80 and 443
only, so other ports are blocked unless an IP range is allowed.

CPU and memory come from the template build. ``SandboxSpec.cpu_count`` and ``memory_mb``
document what the template must give.
"""

from __future__ import annotations

import asyncio
import contextlib
import shlex
import time
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from e2b import (
    AsyncSandbox,
    CommandExitException,
    NotFoundException,
    SandboxException,
    SandboxNotFoundException,
)
from e2b.exceptions import FileNotFoundException

from sama.sandbox.base import (
    SANDBOX_USER,
    STDERR_TAIL_LINES,
    WORKDIR,
    ExecResult,
    SandboxError,
    SandboxFileTooLargeError,
    SandboxNotFoundError,
    SandboxOutdatedError,
    SandboxProcess,
    SandboxSpec,
    log_start,
)

if TYPE_CHECKING:
    from e2b.sandbox_async.commands.command_handle import AsyncCommandHandle

DEFAULT_TEMPLATE: Final = "sama-runner"
ALL_TRAFFIC: Final = "0.0.0.0/0"
MAX_TIMEOUT_S: Final = 60 * 60
"""The longest sandbox timeout that E2B accepts in a create or a connect call. A larger value
fails with "Timeout cannot be greater than 1 hours", so the provider caps the timeout."""


def _timeout_s(spec: SandboxSpec) -> int:
    return min(spec.idle_timeout_s, MAX_TIMEOUT_S)


@dataclass
class _Output:
    """Collects the output of a background command. E2B sends chunks, so this splits lines."""

    queue: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)
    buffer: str = ""
    stderr: deque[str] = field(default_factory=lambda: deque(maxlen=STDERR_TAIL_LINES))

    def on_stdout(self, chunk: str) -> None:
        self.buffer += chunk
        *complete, self.buffer = self.buffer.split("\n")
        for line in complete:
            self.queue.put_nowait(line)

    def on_stderr(self, chunk: str) -> None:
        self.stderr.extend(chunk.splitlines())

    def finish(self) -> None:
        if self.buffer:
            self.queue.put_nowait(self.buffer)
            self.buffer = ""
        self.queue.put_nowait(None)


async def _exit_code(handle: AsyncCommandHandle, output: _Output) -> int:
    try:
        return int((await handle.wait()).exit_code)
    except CommandExitException as exit_:
        return int(exit_.exit_code)
    except SandboxException:
        return -1
    finally:
        output.finish()


@dataclass
class E2BProcess:
    _sandbox: AsyncSandbox
    _handle: AsyncCommandHandle
    _output: _Output
    _waiter: asyncio.Task[int]

    @classmethod
    async def start(
        cls, sandbox: AsyncSandbox, cmd: str, env: Mapping[str, str] | None, cwd: str
    ) -> E2BProcess:
        output = _Output()
        handle = await sandbox.commands.run(
            cmd,
            background=True,
            stdin=True,
            envs=dict(env or {}),
            cwd=cwd,
            user=SANDBOX_USER,
            on_stdout=output.on_stdout,
            on_stderr=output.on_stderr,
            timeout=0,
        )
        return cls(sandbox, handle, output, asyncio.create_task(_exit_code(handle, output)))

    async def write(self, data: str) -> None:
        try:
            await self._sandbox.commands.send_stdin(self._handle.pid, data)
        except SandboxException as error:
            raise SandboxError(f"stdin write failed: {type(error).__name__}") from None

    async def close_stdin(self) -> None:
        with contextlib.suppress(SandboxException):
            await self._sandbox.commands.close_stdin(self._handle.pid)

    async def lines(self) -> AsyncIterator[str]:
        while True:
            line = await self._output.queue.get()
            if line is None:
                self._output.queue.put_nowait(None)  # Later readers end too.
                return
            yield line

    async def wait(self) -> int:
        return await asyncio.shield(self._waiter)

    async def kill(self) -> None:
        with contextlib.suppress(SandboxException):
            await self._handle.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(self._waiter), timeout=5)

    def stderr_tail(self) -> str:
        return "\n".join(self._output.stderr)


@dataclass
class E2BSandbox:
    sandbox: AsyncSandbox

    @property
    def id(self) -> str:
        return str(self.sandbox.sandbox_id)

    @property
    def workdir(self) -> str:
        return WORKDIR

    async def exec(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        timeout_s: float = 60,
    ) -> ExecResult:
        try:
            result = await self.sandbox.commands.run(
                shlex.join(argv),
                envs=dict(env or {}),
                cwd=cwd or WORKDIR,
                user=SANDBOX_USER,
                timeout=timeout_s,
            )
        except CommandExitException as exit_:
            return ExecResult(exit_.exit_code, exit_.stdout or "", exit_.stderr or "")
        except SandboxException as error:
            raise SandboxError(f"exec failed: {type(error).__name__}") from None
        return ExecResult(result.exit_code, result.stdout or "", result.stderr or "")

    async def start(
        self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, cwd: str | None = None
    ) -> SandboxProcess:
        try:
            return await E2BProcess.start(self.sandbox, shlex.join(argv), env, cwd or WORKDIR)
        except SandboxException as error:
            raise SandboxError(f"start failed: {type(error).__name__}") from None

    async def write_file(self, path: str, data: bytes) -> None:
        try:
            await self.sandbox.files.write(path, data, user=SANDBOX_USER)
        except SandboxException as error:
            raise SandboxError(f"write_file failed: {type(error).__name__}") from None

    async def read_file(self, path: str, *, max_bytes: int | None = None) -> bytes:
        try:
            if max_bytes is not None:
                return await self._read_bounded(path, max_bytes)
            data = await self.sandbox.files.read(path, format="bytes", user=SANDBOX_USER)
        except (FileNotFoundException, NotFoundException):
            raise FileNotFoundError(path) from None
        except SandboxException as error:
            raise SandboxError(f"read_file failed: {type(error).__name__}") from None
        return bytes(data)

    async def _read_bounded(self, path: str, max_bytes: int) -> bytes:
        """Streams the file and stops after ``max_bytes``, so the control plane never holds
        more than that."""
        chunks: list[bytes] = []
        size = 0
        stream = await self.sandbox.files.read(
            path, format="stream", user=SANDBOX_USER, stream_idle_timeout=60
        )
        async with stream:
            async for chunk in stream:
                size += len(chunk)
                if size > max_bytes:
                    raise SandboxFileTooLargeError(path)
                chunks.append(bytes(chunk))
        return b"".join(chunks)

    async def remove_file(self, path: str) -> None:
        with contextlib.suppress(FileNotFoundException, NotFoundException):
            await self.sandbox.files.remove(path, user=SANDBOX_USER)

    async def keep_alive(self, seconds: int) -> None:
        try:
            await self.sandbox.set_timeout(min(seconds, MAX_TIMEOUT_S))
        except SandboxException as error:
            raise SandboxError(f"keep_alive failed: {type(error).__name__}") from None

    async def pause(self) -> None:
        try:
            await self.sandbox.pause()
        except SandboxException as error:
            raise SandboxError(f"pause failed: {type(error).__name__}") from None

    async def kill(self) -> None:
        with contextlib.suppress(SandboxException):
            await self.sandbox.kill()


def network_options(spec: SandboxSpec) -> dict[str, Any] | None:
    """The E2B network options: allow the listed hosts and block all other egress.

    An empty allowlist blocks all egress. ``None`` (no filter) only with ``open_egress``.
    """
    if spec.open_egress:
        return None
    options: dict[str, Any] = {"deny_out": [ALL_TRAFFIC]}
    if spec.allow_out:
        options["allow_out"] = list(spec.allow_out)
    return options


@dataclass
class E2BProvider:
    """``api_key`` defaults to the ``E2B_API_KEY`` variable of the control plane."""

    api_key: str | None = field(default=None, repr=False)

    @property
    def name(self) -> str:
        return "e2b"

    def _opts(self) -> dict[str, Any]:
        return {"api_key": self.api_key} if self.api_key else {}

    async def create(self, spec: SandboxSpec) -> E2BSandbox:
        started = time.monotonic()
        try:
            sandbox = await AsyncSandbox.create(
                template=spec.template,
                timeout=_timeout_s(spec),
                metadata=spec.labels(),
                network=network_options(spec),  # type: ignore[arg-type]
                lifecycle={"on_timeout": "pause", "auto_resume": False},
                **self._opts(),
            )
        except SandboxException as error:
            raise SandboxError(f"create failed: {type(error).__name__}: {error}") from None
        result = E2BSandbox(sandbox)
        log_start(self.name, "create", result.id, spec.session_id, started)
        return result

    async def resume(self, sandbox_id: str, spec: SandboxSpec) -> E2BSandbox:
        started = time.monotonic()
        try:
            sandbox = await AsyncSandbox.connect(
                sandbox_id, timeout=_timeout_s(spec), **self._opts()
            )
            info = await sandbox.get_info()
        except (SandboxNotFoundException, NotFoundException):
            raise SandboxNotFoundError(sandbox_id) from None
        except SandboxException as error:
            raise SandboxError(f"resume failed: {type(error).__name__}") from None
        if not spec.owns(info.metadata or {}):
            raise SandboxError("the sandbox belongs to another session")
        result = E2BSandbox(sandbox)
        if spec.outdated(info.metadata or {}):
            raise SandboxOutdatedError(sandbox_id, result)
        log_start(self.name, "resume", result.id, spec.session_id, started)
        return result
