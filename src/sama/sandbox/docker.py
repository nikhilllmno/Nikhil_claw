"""The Docker sandbox provider, for local development and CI (docs/architecture.md, D5).

It drives the ``docker`` CLI, so it needs no Python Docker client. One container per session
runs ``sleep infinity`` from the sama image (``sandbox/Dockerfile``). ``pause`` stops the
container and ``resume`` starts it again; the files stay. Docker has no egress allowlist, so
``SandboxSpec.allow_out`` is not enforced here. Use E2B where egress matters.

A process started with ``start`` runs in its own process group in the container, and the
group id goes to a file in ``/tmp``. ``kill`` stops that group in the container. Killing only
the host ``docker exec`` client would leave the command running.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from sama.sandbox.base import (
    SANDBOX_USER,
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
from sama.sandbox.process import SubprocessProcess, run_command

DEFAULT_IMAGE: Final = "sama-runner:dev"
_GROUP_SCRIPT: Final = 'echo $$ > "$0"; "$@"; code=$?; rm -f "$0"; exit $code'
"""Runs the command as a child of this shell. ``setsid`` makes the shell the leader of a new
process group, so the group id is ``$$``."""
_KILL_SCRIPT: Final = (
    'for _ in $(seq "$1"); do [ -s "$0" ] && break; sleep 0.1; done; '
    'pid=$(cat "$0" 2>/dev/null) && kill -s KILL -- "-$pid" 2>/dev/null; rm -f "$0"'
)
"""Kills the group in the group file. It waits ``$1`` times 0.1 s for the file first, because
a process that just started can still be without it. The dash ``kill`` needs ``-s KILL --``
for a group. It refuses ``-KILL -- -pid``."""
_KILL_TIMEOUT_S: Final = 5.0


async def _docker(
    *args: str,
    stdin: bytes | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: float = 120,
) -> tuple[int, bytes, bytes]:
    return await run_command(["docker", *args], env=env, stdin=stdin, timeout_s=timeout_s)


def _failed(what: str, err: bytes) -> SandboxError:
    return SandboxError(f"{what} failed: {err.decode(errors='replace').strip()}")


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
class DockerProcess:
    """A process in a container. The host side is the ``docker exec`` client."""

    _client: SubprocessProcess
    _container: str
    _group_file: str

    async def write(self, data: str) -> None:
        await self._client.write(data)

    async def close_stdin(self) -> None:
        await self._client.close_stdin()

    def lines(self) -> AsyncIterator[str]:
        return self._client.lines()

    async def wait(self) -> int:
        return await self._client.wait()

    async def kill(self) -> None:
        """Kills the process group in the container, then the client on the host.

        It kills the group also when the client already ended: a client that lost its
        connection does not stop the command in the container. The group file is gone only
        when the command ended, so then the script does nothing.
        """
        waits = "10" if self._client.returncode is None else "0"
        try:
            with contextlib.suppress(SandboxError):
                await _docker(
                    "exec",
                    "--user",
                    SANDBOX_USER,
                    self._container,
                    "sh",
                    "-c",
                    _KILL_SCRIPT,
                    self._group_file,
                    waits,
                    timeout_s=_KILL_TIMEOUT_S,
                )
        finally:
            await self._client.kill()

    def stderr_tail(self) -> str:
        return self._client.stderr_tail()


@dataclass
class DockerSandbox:
    container: str

    @property
    def id(self) -> str:
        return self.container

    @property
    def workdir(self) -> str:
        return WORKDIR

    def _exec_args(
        self, env: Mapping[str, str] | None, cwd: str | None, *, stdin: bool = False
    ) -> tuple[list[str], dict[str, str]]:
        env_args, cli_env = _env_args(env)
        flags = ["-i"] if stdin else []
        args = ["exec", *flags, "--user", SANDBOX_USER, "--workdir", cwd or WORKDIR, *env_args]
        return [*args, self.container], cli_env

    async def exec(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        timeout_s: float = 60,
    ) -> ExecResult:
        args, cli_env = self._exec_args(env, cwd)
        code, out, err = await _docker(*args, *argv, env=cli_env, timeout_s=timeout_s)
        return ExecResult(code, out.decode(errors="replace"), err.decode(errors="replace"))

    async def start(
        self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, cwd: str | None = None
    ) -> SandboxProcess:
        args, cli_env = self._exec_args(env, cwd, stdin=True)
        group_file = f"/tmp/sama-process-{uuid.uuid4().hex}.pgid"  # noqa: S108 -- in the container
        wrapped = ["setsid", "-w", "sh", "-c", _GROUP_SCRIPT, group_file, *argv]
        try:
            client = await SubprocessProcess.start(["docker", *args, *wrapped], cli_env)
        except OSError as error:
            raise SandboxError(f"the process did not start: {error}") from None
        return DockerProcess(client, self.container, group_file)

    async def write_file(self, path: str, data: bytes) -> None:
        script = f'mkdir -p -- "$(dirname -- {shlex.quote(path)})" && cat > {shlex.quote(path)}'
        code, _, err = await _docker(
            "exec", "-i", "--user", SANDBOX_USER, self.container, "sh", "-c", script, stdin=data
        )
        if code != 0:
            raise _failed("write_file", err)

    async def read_file(self, path: str, *, max_bytes: int | None = None) -> bytes:
        read = "cat --" if max_bytes is None else f"head -c {max_bytes + 1} --"
        script = f"test -f {shlex.quote(path)} || exit 44; {read} {shlex.quote(path)}"
        code, out, err = await _docker(
            "exec", "--user", SANDBOX_USER, self.container, "sh", "-c", script
        )
        if code == 44:
            raise FileNotFoundError(path)
        if code != 0:
            raise _failed("read_file", err)
        if max_bytes is not None and len(out) > max_bytes:
            raise SandboxFileTooLargeError(path)
        return out

    async def remove_file(self, path: str) -> None:
        code, _, err = await _docker(
            "exec", "--user", SANDBOX_USER, self.container, "rm", "-f", path
        )
        if code != 0:
            raise _failed("remove_file", err)

    async def keep_alive(self, seconds: int) -> None:
        """Docker has no idle timeout, so the container runs until ``pause``."""

    async def pause(self) -> None:
        code, _, err = await _docker("stop", "--time", "2", self.container)
        if code != 0:
            raise _failed("pause", err)

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
        started = time.monotonic()
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
            raise _failed("docker run", err)
        sandbox = DockerSandbox(out.decode().strip())
        log_start(self.name, "create", sandbox.id, spec.session_id, started)
        return sandbox

    async def resume(self, sandbox_id: str, spec: SandboxSpec) -> DockerSandbox:
        started = time.monotonic()
        code, out, _ = await _docker("inspect", "--format", "{{json .Config.Labels}}", sandbox_id)
        if code != 0:
            raise SandboxNotFoundError(sandbox_id)
        labels = json.loads(out) or {}
        if not spec.owns(labels):
            raise SandboxError("the sandbox belongs to another session")
        code, _, err = await _docker("start", sandbox_id)
        if code != 0:
            raise _failed("docker start", err)
        if spec.outdated(labels):
            raise SandboxOutdatedError(sandbox_id, DockerSandbox(sandbox_id))
        log_start(self.name, "resume", sandbox_id, spec.session_id, started)
        return DockerSandbox(sandbox_id)
