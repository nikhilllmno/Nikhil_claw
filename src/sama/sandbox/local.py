"""The local sandbox provider: a folder on the host. For development and tests only.

It gives **no isolation**: the runner runs as the host user and can read the host files and
use the host network. It gives only a clean environment: a process gets ``PATH``, ``HOME``
(a folder in the sandbox), ``LANG``, and the variables of the caller. No variable of the host
process goes in, so a test can prove that the runner gets no provider key.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sama.sandbox.base import (
    ExecResult,
    SandboxError,
    SandboxNotFoundError,
    SandboxProcess,
    SandboxSpec,
    SandboxStart,
    StartKind,
    Stopwatch,
)
from sama.sandbox.process import SubprocessProcess

_LABELS: Final = "labels.json"


@dataclass
class LocalSandbox:
    root: Path

    @property
    def id(self) -> str:
        return self.root.name

    @property
    def provider(self) -> str:
        return "local"

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
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env(env),
            cwd=cwd or self.workdir,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise SandboxError(f"the command timed out after {timeout_s} s") from None
        return ExecResult(process.returncode or 0, out.decode(errors="replace"), err.decode())

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

    async def read_file(self, path: str) -> bytes:
        return self._path(path).read_bytes()

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
        watch = Stopwatch()
        root = self.root / f"local-{uuid.uuid4().hex[:12]}"
        (root / "work").mkdir(parents=True)
        (root / "home").mkdir()
        (root / _LABELS).write_text(json.dumps(spec.labels()))
        sandbox = LocalSandbox(root)
        SandboxStart(
            self.name, sandbox.id, spec.session_id, StartKind.CREATE, watch.elapsed()
        ).log()
        return sandbox

    async def resume(self, sandbox_id: str, spec: SandboxSpec) -> LocalSandbox:
        watch = Stopwatch()
        root = self.root / sandbox_id
        if root.parent != self.root or not (root / _LABELS).is_file():
            raise SandboxNotFoundError(sandbox_id)
        labels = json.loads((root / _LABELS).read_text())
        if [labels.get("sama.org_id"), labels.get("sama.session_id")] != [
            spec.org_id,
            spec.session_id,
        ]:
            raise SandboxError("the sandbox belongs to another session")
        SandboxStart(
            self.name, sandbox_id, spec.session_id, StartKind.RESUME, watch.elapsed()
        ).log()
        return LocalSandbox(root)
