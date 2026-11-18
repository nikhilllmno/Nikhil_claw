"""The sandbox interface (docs/architecture.md, Section 16.2; decision D5).

A ``SandboxProvider`` creates one sandbox per session and resumes it for the next run. The
orchestrator uses only this interface, so E2B, Docker, and later providers are the same to it.

The sandbox runs the runner and the native tools of the agent. It gets no database URL, no
provider key, and no integration token. The orchestrator gives the runner a run token only.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

log: Final = logging.getLogger("sama.sandbox")

DEFAULT_CPU_COUNT: Final = 2
DEFAULT_MEMORY_MB: Final = 4096
DEFAULT_IDLE_TIMEOUT_S: Final = 15 * 60
"""An idle sandbox pauses after this time. E2B pauses it by itself (``on_timeout: pause``)."""

WORKDIR: Final = "/home/user/work"
"""The work folder of the agent in the sama image. It stays the same for the life of a
session, because the Claude CLI keys its session files by the folder."""


class SandboxError(Exception):
    """A provider call failed. The message has no secret."""


class SandboxNotFoundError(SandboxError):
    """The provider has no sandbox with this id. The caller creates a new sandbox."""


class StartKind(StrEnum):
    CREATE = "create"
    RESUME = "resume"


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """What the orchestrator asks for. ``allow_out`` is the egress allowlist (host names).

    An empty ``allow_out`` means the provider default. E2B then blocks no host. The sama
    deployment always sets it (Section 16.2).
    """

    org_id: str
    session_id: str
    template: str
    cpu_count: int = DEFAULT_CPU_COUNT
    memory_mb: int = DEFAULT_MEMORY_MB
    idle_timeout_s: int = DEFAULT_IDLE_TIMEOUT_S
    allow_out: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.org_id or not self.session_id or not self.template:
            raise ValueError("org_id, session_id, and template must not be empty")
        if self.cpu_count < 1 or self.memory_mb < 512 or self.idle_timeout_s < 1:
            raise ValueError("cpu_count, memory_mb, and idle_timeout_s are too small")

    def labels(self) -> dict[str, str]:
        """Provider labels. Sama keys win over ``metadata``, so a caller cannot change them."""
        return {**self.metadata, "sama.org_id": self.org_id, "sama.session_id": self.session_id}


@dataclass(frozen=True, slots=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class SandboxStart:
    """One measured sandbox start. The orchestrator logs it (roadmap M1.6)."""

    provider: str
    sandbox_id: str
    session_id: str
    kind: StartKind
    duration_s: float

    def log(self) -> None:
        log.info(
            "sandbox start provider=%s kind=%s sandbox=%s session=%s duration_s=%.3f",
            self.provider,
            self.kind,
            self.sandbox_id,
            self.session_id,
            self.duration_s,
            extra={"sama_sandbox_start": self},
        )


class Stopwatch:
    def __init__(self) -> None:
        self._started = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self._started


@runtime_checkable
class SandboxProcess(Protocol):
    """A process in the sandbox with a stdin pipe and a stdout line stream."""

    async def write(self, data: str) -> None:
        """Writes to stdin. Raises ``SandboxError`` if the process has ended."""
        ...

    async def close_stdin(self) -> None: ...

    def lines(self) -> AsyncIterator[str]:
        """The stdout lines, without the newline. Ends when the process ends."""
        ...

    async def wait(self) -> int:
        """Waits for the end and returns the exit code."""
        ...

    async def kill(self) -> None:
        """Stops the process at once. Safe to call more than once."""
        ...

    def stderr_tail(self) -> str:
        """The last part of stderr, for error messages and logs."""
        ...


@runtime_checkable
class Sandbox(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def provider(self) -> str: ...

    @property
    def workdir(self) -> str:
        """The work folder: the default ``cwd`` of commands, and the folder of the agent."""
        ...

    async def exec(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        timeout_s: float = 60,
    ) -> ExecResult: ...

    async def start(
        self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, cwd: str | None = None
    ) -> SandboxProcess:
        """Starts a long-running process, for example the runner."""
        ...

    async def write_file(self, path: str, data: bytes) -> None: ...

    async def read_file(self, path: str) -> bytes:
        """Raises ``FileNotFoundError`` if the file does not exist."""
        ...

    async def remove_file(self, path: str) -> None:
        """Removes the file. No error if it does not exist."""
        ...

    async def keep_alive(self, seconds: int) -> None:
        """Keeps the sandbox running for at least ``seconds`` from now. The orchestrator calls
        it before a run, so the idle timeout does not pause the sandbox during the run."""
        ...

    async def pause(self) -> None:
        """Pauses the sandbox. Files stay. ``SandboxProvider.resume`` starts it again."""
        ...

    async def kill(self) -> None:
        """Deletes the sandbox and its files."""
        ...


@runtime_checkable
class SandboxProvider(Protocol):
    @property
    def name(self) -> str: ...

    async def create(self, spec: SandboxSpec) -> Sandbox: ...

    async def resume(self, sandbox_id: str, spec: SandboxSpec) -> Sandbox:
        """Resumes a paused sandbox. Raises ``SandboxNotFoundError`` if it is gone."""
        ...
