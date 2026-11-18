"""The Docker sandbox provider, for local development and CI (docs/architecture.md, D5).

It drives the ``docker`` CLI, so it needs no Python Docker client. One container per session
runs ``sleep infinity`` from the sama image (``sandbox/Dockerfile``). ``pause`` stops the
container and ``resume`` starts it again; the files stay. Docker has no egress allowlist, so
``SandboxSpec.allow_out`` is not enforced here. Use E2B where egress matters.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from sama.sandbox.base import (
    WORKDIR,
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

DEFAULT_IMAGE: Final = "sama-runner:dev"
_USER: Final = "user"


async def _docker(
    *args: str, stdin: bytes | None = None, timeout_s: float = 120
) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(stdin), timeout=timeout_s)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise SandboxError(f"docker {args[0]} timed out after {timeout_s} s") from None
    return process.returncode or 0, out, err


def _env_args(env: Mapping[str, str] | None) -> tuple[list[str], dict[str, str]]:
    """``-e NAME`` arguments and the CLI environment that holds the values.

    The values never go on the command line, so ``ps`` on the host does not show a run token.
    """
    if not env:
        return [], dict(os.environ)
    args: list[str] = []
    for name in env:
        args += ["-e", name]
    return args, {**os.environ, **env}


@dataclass
class DockerSandbox:
    container: str

    @property
    def id(self) -> str:
        return self.container

    @property
    def provider(self) -> str:
        return "docker"

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
        env_args, cli_env = _env_args(env)
        process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "--user",
            _USER,
            "--workdir",
            cwd or WORKDIR,
            *env_args,
            self.container,
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=cli_env,
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
        env_args, cli_env = _env_args(env)
        args = ["exec", "-i", "--user", _USER, "--workdir", cwd or WORKDIR, *env_args]
        return await SubprocessProcess.start(["docker", *args, self.container, *argv], cli_env)

    async def write_file(self, path: str, data: bytes) -> None:
        script = f'mkdir -p -- "$(dirname -- {shlex.quote(path)})" && cat > {shlex.quote(path)}'
        code, _, err = await _docker(
            "exec", "-i", "--user", _USER, self.container, "sh", "-c", script, stdin=data
        )
        if code != 0:
            raise SandboxError(f"write_file failed: {err.decode(errors='replace').strip()}")

    async def read_file(self, path: str) -> bytes:
        script = f"test -f {shlex.quote(path)} || exit 44; cat -- {shlex.quote(path)}"
        code, out, err = await _docker("exec", "--user", _USER, self.container, "sh", "-c", script)
        if code == 44:
            raise FileNotFoundError(path)
        if code != 0:
            raise SandboxError(f"read_file failed: {err.decode(errors='replace').strip()}")
        return out

    async def remove_file(self, path: str) -> None:
        code, _, err = await _docker("exec", "--user", _USER, self.container, "rm", "-f", path)
        if code != 0:
            raise SandboxError(f"remove_file failed: {err.decode(errors='replace').strip()}")

    async def keep_alive(self, seconds: int) -> None:
        """Docker has no idle timeout, so the container runs until ``pause``."""

    async def pause(self) -> None:
        code, _, err = await _docker("stop", "--time", "2", self.container)
        if code != 0:
            raise SandboxError(f"pause failed: {err.decode(errors='replace').strip()}")

    async def kill(self) -> None:
        await _docker("rm", "--force", self.container)


@dataclass
class DockerProvider:
    """``network`` is a Docker network name, for example ``host`` in tests."""

    image: str = DEFAULT_IMAGE
    network: str | None = None

    @property
    def name(self) -> str:
        return "docker"

    async def create(self, spec: SandboxSpec) -> DockerSandbox:
        watch = Stopwatch()
        args = [
            "run",
            "--detach",
            "--init",
            f"--cpus={spec.cpu_count}",
            f"--memory={spec.memory_mb}m",
        ]
        for key, value in spec.labels().items():
            args += ["--label", f"{key}={value}"]
        if self.network:
            args += ["--network", self.network]
        args += [self.image, "sleep", "infinity"]
        code, out, err = await _docker(*args)
        if code != 0:
            raise SandboxError(f"docker run failed: {err.decode(errors='replace').strip()}")
        sandbox = DockerSandbox(out.decode().strip())
        SandboxStart(
            self.name, sandbox.id, spec.session_id, StartKind.CREATE, watch.elapsed()
        ).log()
        return sandbox

    async def resume(self, sandbox_id: str, spec: SandboxSpec) -> DockerSandbox:
        watch = Stopwatch()
        owner = '{{index .Config.Labels "sama.org_id"}} {{index .Config.Labels "sama.session_id"}}'
        code, out, _ = await _docker("inspect", "--format", owner, sandbox_id)
        if code != 0:
            raise SandboxNotFoundError(sandbox_id)
        if out.decode().split() != [spec.org_id, spec.session_id]:
            raise SandboxError("the sandbox belongs to another session")
        code, _, err = await _docker("start", sandbox_id)
        if code != 0:
            raise SandboxError(f"docker start failed: {err.decode(errors='replace').strip()}")
        SandboxStart(
            self.name, sandbox_id, spec.session_id, StartKind.RESUME, watch.elapsed()
        ).log()
        return DockerSandbox(sandbox_id)
