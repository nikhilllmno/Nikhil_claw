"""Connector hosts (docs/architecture.md, Section 10.3.2, M4.11).

The backend is a local fake: each host is a folder (``LocalProvider``), and ``/home/user`` in
a path is the ``home`` folder of the host. The real bridge (``host_bridge.py``) runs the fake
stdio server ``stdio_server.py``, so the tests cover the bridge, the stdio client, and the
host lifecycle: the lazy start, the idle pause, the resume, and the restart above the memory
limit.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from sama.connectors.host import (
    BRIDGE_PATH,
    CONNECTION_LABEL,
    HASH_LABEL,
    HOME,
    KIND,
    KIND_LABEL,
    ConnectorHosts,
    HostedServer,
    HostInfo,
)
from sama.connectors.host_bridge import MEMORY_EXIT
from sama.connectors.manifest import McpHosted
from sama.connectors.mcp_remote import McpError, render_result
from sama.connectors.mcp_stdio import McpProcessEndedError, McpSendFailedError
from sama.sandbox.base import SandboxError, SandboxProcess, SandboxSpec
from sama.sandbox.local import LocalProvider, LocalSandbox
from sama.vault.redact import default_redactor

SERVER = Path(__file__).with_name("stdio_server.py")
SECRET = "demo-secret-value-4242"
ORG = "org-1"


@dataclass
class HostSandbox(LocalSandbox):
    """A local sandbox where ``/home/user`` is its ``home`` folder."""

    backend: LocalHosts | None = None

    def _map(self, path: str) -> str:
        if path == HOME or path.startswith(HOME + "/"):
            return str(self.root / "home") + path[len(HOME) :]
        return path

    def _path(self, path: str) -> Path:
        return Path(self._map(path))

    async def start(
        self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, cwd: str | None = None
    ) -> SandboxProcess:
        assert self.backend is not None
        self.backend.started.append((list(argv), dict(env or {})))
        process = await super().start(
            [self._map(a) for a in argv], env=env, cwd=self._map(cwd) if cwd else None
        )
        return HookedProcess(process, self.backend, len(self.backend.started))

    async def pause(self) -> None:
        assert self.backend is not None
        self.backend.paused.append(self.id)

    async def keep_alive(self, seconds: int) -> None:
        assert self.backend is not None
        self.backend.kept.append(seconds)


class HookedProcess:
    """A process whose stdin writes go through ``LocalHosts.on_write``."""

    def __init__(self, inner: SandboxProcess, backend: LocalHosts, number: int) -> None:
        self._inner = inner
        self._backend = backend
        self.number = number

    async def write(self, data: str) -> None:
        await self._inner.write(data)
        if self._backend.on_write is not None:
            await self._backend.on_write(self.number)

    async def close_stdin(self) -> None:
        await self._inner.close_stdin()

    def lines(self) -> AsyncIterator[str]:
        return self._inner.lines()

    async def wait(self) -> int:
        return await self._inner.wait()

    async def kill(self) -> None:
        await self._inner.kill()

    def stderr_tail(self) -> str:
        return self._inner.stderr_tail()


@dataclass
class LocalHosts:
    """The fake ``HostBackend``."""

    root: Path
    created: list[SandboxSpec] = field(default_factory=list)
    resumed: list[str] = field(default_factory=list)
    paused: list[str] = field(default_factory=list)
    killed: list[str] = field(default_factory=list)
    started: list[tuple[list[str], dict[str, str]]] = field(default_factory=list)
    kept: list[int] = field(default_factory=list)
    on_write: Callable[[int], Awaitable[None]] | None = None
    """Runs after each stdin write, with the number of the process (1 is the first)."""

    @property
    def provider(self) -> LocalProvider:
        return LocalProvider(self.root)

    async def find(self, labels: Mapping[str, str]) -> list[HostInfo]:
        found = []
        for entry in sorted(self.root.iterdir()):
            have = json.loads((entry / "labels.json").read_text())
            if all(have.get(k) == v for k, v in labels.items()):
                found.append(HostInfo(entry.name, have))
        return found

    async def create(self, spec: SandboxSpec) -> HostSandbox:
        self.created.append(spec)
        made = await self.provider.create(spec)
        return HostSandbox(made.root, self)

    async def resume(self, sandbox_id: str, spec: SandboxSpec) -> HostSandbox:
        made = await self.provider.resume(sandbox_id, spec)
        self.resumed.append(sandbox_id)
        return HostSandbox(made.root, self)

    async def kill(self, sandbox_id: str) -> None:
        self.killed.append(sandbox_id)
        await LocalSandbox(self.root / sandbox_id).kill()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def server(
    connection_id: str = "c1",
    *,
    env: Mapping[str, str] | None = None,
    egress: tuple[str, ...] = ("api.demo.test",),
    memory_mb: int = 256,
    idle_s: int = 120,
) -> HostedServer:
    hosted = McpHosted(
        command=(sys.executable, str(SERVER)),
        env={"DEMO_TOKEN": "demo_token"},
        egress=egress,
        memory_mb=memory_mb,
        idle_s=idle_s,
    )
    return HostedServer(connection_id, ORG, hosted, {"DEMO_TOKEN": SECRET} if env is None else env)


@pytest.fixture
def backend(tmp_path: Path) -> LocalHosts:
    root = tmp_path / "hosts"
    root.mkdir()
    return LocalHosts(root)


@pytest.fixture
async def hosts(backend: LocalHosts) -> AsyncIterator[tuple[ConnectorHosts, Clock]]:
    clock = Clock()
    made = ConnectorHosts(backend, template="base", clock=clock, reaper_every_s=3600)
    yield made, clock
    await made.aclose()


async def echo(hosts: ConnectorHosts, srv: HostedServer, text: str = "hi") -> str:
    return render_result(await hosts.call(srv, "echo", {"text": text}))


def only_host(backend: LocalHosts) -> str:
    (entry,) = backend.root.iterdir()
    return entry.name


def pid_of(answer: str) -> str:
    return answer.rsplit("pid=", 1)[1].split()[0]


async def test_the_host_starts_on_the_first_call_and_keeps_the_server(
    backend: LocalHosts, hosts: tuple[ConnectorHosts, Clock]
) -> None:
    connector_hosts, _ = hosts
    srv = server()
    assert backend.created == []
    tools = await connector_hosts.list_tools(srv)
    assert [t.name for t in tools] == ["echo", "has_secret", "grow", "crash"]
    assert tools[0].read_only is True
    assert len(backend.created) == 1
    spec = backend.created[0]
    labels = spec.labels()
    assert labels[KIND_LABEL] == KIND
    assert labels[CONNECTION_LABEL] == "c1"
    assert labels["sama.org_id"] == ORG
    assert labels["sama.session_id"] == "host-c1"
    assert HASH_LABEL in labels
    # The egress allowlist is the egress of the manifest: a command has no registry.
    assert spec.allow_out == ("api.demo.test",)
    assert spec.open_egress is False
    assert spec.memory_mb == 512
    first = await echo(connector_hosts, srv)
    assert first.startswith("echo hi")
    second = await echo(connector_hosts, srv, "again")
    assert pid_of(first) == pid_of(second)
    assert len(backend.created) == 1
    assert len(backend.started) == 1
    assert "set=True length=22" in render_result(await connector_hosts.call(srv, "has_secret", {}))


async def test_an_idle_host_pauses_and_the_next_call_resumes_it(
    backend: LocalHosts, hosts: tuple[ConnectorHosts, Clock]
) -> None:
    connector_hosts, clock = hosts
    srv = server()
    first = await echo(connector_hosts, srv)
    clock.now += 60
    assert await connector_hosts.pause_idle() == 0
    clock.now += 61
    assert await connector_hosts.pause_idle() == 1
    assert backend.paused == [only_host(backend)]
    second = await echo(connector_hosts, srv)
    assert pid_of(first) != pid_of(second)
    assert len(backend.created) == 1
    assert len(backend.resumed) == 1


async def test_above_the_memory_limit_the_server_stops_and_the_next_call_starts_it(
    hosts: tuple[ConnectorHosts, Clock],
) -> None:
    connector_hosts, _ = hosts
    srv = server(memory_mb=64)
    first = await echo(connector_hosts, srv)
    with pytest.raises(McpProcessEndedError) as ended:
        await connector_hosts.call(srv, "grow", {"mb": 150})
    assert ended.value.code == MEMORY_EXIT
    assert "memory limit" in str(ended.value)
    second = await echo(connector_hosts, srv)
    assert pid_of(first) != pid_of(second)


async def test_a_crash_is_not_sent_again_and_the_next_call_starts_the_server(
    backend: LocalHosts, hosts: tuple[ConnectorHosts, Clock]
) -> None:
    connector_hosts, _ = hosts
    srv = server()
    await echo(connector_hosts, srv)
    with pytest.raises(McpProcessEndedError) as ended:
        await connector_hosts.call(srv, "crash", {})
    assert ended.value.code == 3
    assert len(backend.started) == 1
    await echo(connector_hosts, srv)
    assert len(backend.started) == 2
    with pytest.raises(McpError, match="refused tools/call"):
        await connector_hosts.call(srv, "nothing", {})


async def test_new_values_restart_the_server_and_new_egress_makes_a_new_host(
    backend: LocalHosts, hosts: tuple[ConnectorHosts, Clock]
) -> None:
    connector_hosts, _ = hosts
    await echo(connector_hosts, server())
    changed = server(env={"DEMO_TOKEN": "short"})
    assert "length=5" in render_result(await connector_hosts.call(changed, "has_secret", {}))
    assert len(backend.started) == 2
    assert len(backend.created) == 1
    await echo(connector_hosts, server(egress=("other.demo.test",)))
    assert len(backend.created) == 2
    assert len(backend.killed) == 1


async def test_a_new_control_plane_finds_the_host_by_its_labels(
    backend: LocalHosts, hosts: tuple[ConnectorHosts, Clock]
) -> None:
    connector_hosts, _ = hosts
    await echo(connector_hosts, server())
    await connector_hosts.aclose()
    again = ConnectorHosts(backend, template="base", reaper_every_s=3600)
    await echo(again, server())
    assert len(backend.created) == 1
    assert len(backend.resumed) == 1
    await again.stop("c1")
    assert list(backend.root.iterdir()) == []
    await again.aclose()


async def test_the_agent_sandbox_cannot_read_the_secrets_of_the_host(
    backend: LocalHosts, hosts: tuple[ConnectorHosts, Clock], tmp_path: Path
) -> None:
    connector_hosts, _ = hosts
    srv = server()
    assert SECRET not in repr(srv)
    assert "set=True" in render_result(await connector_hosts.call(srv, "has_secret", {}))
    spec = backend.created[0]
    # The secret is only in the env of the server process: not in the labels, the spec, the
    # argv, or the files of the host.
    assert SECRET not in json.dumps(spec.labels()) + repr(spec)
    argv, env = backend.started[0]
    assert argv[:3] == ["python3", "-u", BRIDGE_PATH]
    assert SECRET not in json.dumps(argv)
    assert env == {"DEMO_TOKEN": SECRET}
    host_root = backend.root / only_host(backend)
    for path in host_root.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes(), path
    # The host is not the sandbox of a session: the spec of a session in the same org does
    # not own it, so the orchestrator cannot resume it or run a command in it.
    agent = SandboxSpec(org_id=ORG, session_id="session-1", template="base")
    assert not agent.owns(spec.labels())
    with pytest.raises(SandboxError, match="another session"):
        await backend.provider.resume(host_root.name, agent)
    # A session sandbox does not get the env of the host.
    (tmp_path / "sessions").mkdir()
    session_box = await LocalProvider(tmp_path / "sessions").create(agent)
    result = await session_box.exec(["env"])
    assert SECRET not in result.stdout
    assert "DEMO_TOKEN" not in result.stdout
    assert SECRET not in os.environ.get("DEMO_TOKEN", "")


async def test_calls_at_the_same_time_start_one_server(
    backend: LocalHosts, hosts: tuple[ConnectorHosts, Clock]
) -> None:
    connector_hosts, _ = hosts
    srv = server()
    answers = await asyncio.gather(*(echo(connector_hosts, srv, str(i)) for i in range(5)))
    assert len({pid_of(a) for a in answers}) == 1
    assert len(backend.created) == 1
    assert len(backend.started) == 1


async def test_a_failed_write_of_a_tool_call_is_not_sent_again(
    backend: LocalHosts, hosts: tuple[ConnectorHosts, Clock]
) -> None:
    """The write reached the server, but its acknowledgement was lost: the call fails and is
    not sent again, because the server can have run it."""
    connector_hosts, _ = hosts
    srv = server()
    await echo(connector_hosts, srv)

    async def lost(number: int) -> None:
        raise SandboxError("stdin write failed: TimeoutException")

    backend.on_write = lost
    with pytest.raises(McpSendFailedError):
        await connector_hosts.call(srv, "echo", {"text": "once"})
    assert len(backend.started) == 1
    backend.on_write = None
    await echo(connector_hosts, srv)
    assert len(backend.started) == 2


async def test_a_late_failure_does_not_reset_the_server_of_another_call(
    backend: LocalHosts, hosts: tuple[ConnectorHosts, Clock]
) -> None:
    connector_hosts, _ = hosts
    srv = server()
    await echo(connector_hosts, srv)
    release = asyncio.Event()
    waiting = asyncio.Event()

    async def fail(number: int) -> None:
        if number != 1:
            return
        if not waiting.is_set():
            # The first failed write to the first server waits, then fails.
            waiting.set()
            await release.wait()
        raise SandboxError("stdin write failed")

    backend.on_write = fail
    late = asyncio.create_task(connector_hosts.call(srv, "echo", {"text": "late"}))
    await waiting.wait()
    # A second call fails on the first server too, and starts no new one (not sent again).
    with pytest.raises(McpSendFailedError):
        await connector_hosts.call(srv, "echo", {"text": "first"})
    # A third call starts the second server.
    fresh = await echo(connector_hosts, srv, "new")
    assert len(backend.started) == 2
    release.set()
    with pytest.raises(McpSendFailedError):
        await late
    # The late failure did not close the second server or forget its host.
    again = await echo(connector_hosts, srv, "again")
    assert pid_of(again) == pid_of(fresh)
    assert len(backend.started) == 2
    assert len(backend.created) == 1


async def test_e2b_does_not_pause_a_host_while_its_server_starts(
    backend: LocalHosts, hosts: tuple[ConnectorHosts, Clock]
) -> None:
    connector_hosts, _ = hosts
    srv = server(idle_s=60)
    # The E2B timeout covers the start time (240 s), not only the idle time.
    assert connector_hosts.spec(srv).idle_timeout_s == 360
    await echo(connector_hosts, srv)
    assert backend.kept == []
    await connector_hosts.restart(srv.connection_id)
    await echo(connector_hosts, srv)
    # The server starts again in the same host: first the host gets the lease.
    assert backend.kept == [360]
    assert len(backend.started) == 2


async def test_an_error_of_the_server_does_not_show_a_secret(
    hosts: tuple[ConnectorHosts, Clock],
) -> None:
    connector_hosts, _ = hosts
    default_redactor().add(SECRET)  # The vault adds each secret that it reads.
    try:
        with pytest.raises(McpError, match="refused tools/call: bad token") as refused:
            await connector_hosts.call(server(), "leak", {})
        assert SECRET not in str(refused.value)
    finally:
        default_redactor().forget(SECRET)
