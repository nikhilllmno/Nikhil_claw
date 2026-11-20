"""The local sandbox provider: a folder on the host. For development and tests only.

It gives **no isolation**: the runner runs as the host user and can read the host files and
use the host network. It gives only a clean environment: a process gets ``PATH``, ``HOME``
(a folder in the sandbox), ``LANG``, and the variables of the caller. No variable of the host
process goes in, so a test can prove that the runner gets no provider key.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sama.sandbox.base import (
    ExecResult,
    SandboxError,
    SandboxFileTooLargeError,
    SandboxNotFoundError,
    SandboxOutdatedError,
    SandboxProcess,
    SandboxSpec,
    log_start,
)
from sama.sandbox.process import SubprocessProcess, run_command

_LABELS: Final = "labels.json"


@dataclass
class LocalSandbox:
    root: Path

    @property
    def id(self) -> str:
        return self.root.name

    @property
    def workdir(self) -> str:
        return str(self.root / "work")

    def _env(self, env: Mapping[str, str] | None) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.root / "home"),
            "LANG": "C.UTF-8",
            **(env or {}),
        }

    def _path(self, path: str) -> Path:
        return Path(self.workdir) / path

    async def exec(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        timeout_s: float = 60,
    ) -> ExecResult:
        code, out, err = await run_command(
            argv, env=self._env(env), cwd=cwd or self.workdir, timeout_s=timeout_s
        )
        return ExecResult(code, out.decode(errors="replace"), err.decode(errors="replace"))

    async def start(
        self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, cwd: str | None = None
    ) -> SandboxProcess:
        try:
            return await SubprocessProcess.start(argv, self._env(env), cwd or self.workdir)
        except OSError as error:
            raise SandboxError(f"the process did not start: {error}") from None

    async def write_file(self, path: str, data: bytes) -> None:
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def read_file(self, path: str, *, max_bytes: int | None = None) -> bytes:
        if max_bytes is None:
            return self._path(path).read_bytes()
        with self._path(path).open("rb") as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise SandboxFileTooLargeError(path)
        return data

    async def remove_file(self, path: str) -> None:
        self._path(path).unlink(missing_ok=True)

    async def keep_alive(self, seconds: int) -> None:
        """A folder does not time out."""

    async def pause(self) -> None:
        """A folder has nothing to pause."""

    async def kill(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


@dataclass
class LocalProvider:
    """Each sandbox is a folder in ``root``."""

    root: Path

    @property
    def name(self) -> str:
        return "local"

    async def create(self, spec: SandboxSpec) -> LocalSandbox:
        started = time.monotonic()
        root = self.root / f"local-{uuid.uuid4().hex[:12]}"
        (root / "work").mkdir(parents=True)
        (root / "home").mkdir()
        (root / _LABELS).write_text(json.dumps(spec.labels()))
        sandbox = LocalSandbox(root)
        log_start(self.name, "create", sandbox.id, spec.session_id, started)
        return sandbox

    async def resume(self, sandbox_id: str, spec: SandboxSpec) -> LocalSandbox:
        started = time.monotonic()
        root = self.root / sandbox_id
        if root.parent != self.root or not (root / _LABELS).is_file():
            raise SandboxNotFoundError(sandbox_id)
        labels = json.loads((root / _LABELS).read_text())
        if not spec.owns(labels):
            raise SandboxError("the sandbox belongs to another session")
        if spec.outdated(labels):
            raise SandboxOutdatedError(sandbox_id, LocalSandbox(root))
        log_start(self.name, "resume", sandbox_id, spec.session_id, started)
        return LocalSandbox(root)
