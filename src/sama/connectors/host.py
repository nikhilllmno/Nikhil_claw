"""Connector hosts: one sandbox for each hosted stdio MCP connection (docs/architecture.md,
Section 10.3.2).

A connector host is an E2B sandbox that runs one MCP server and nothing else:

- it is not the sandbox of a session. Its labels name the connection, not a session, so the
  orchestrator never finds it, and the agent cannot run a command in it;
- its egress allowlist is the egress of the manifest and the package registry;
- the secrets of the connection are environment variables of the server process only. They
  are not in the labels, in the files, or in the argv;
- it starts on the first call, pauses after ``idle_s`` without a call, and resumes on the next
  call. A server above its memory limit stops (the bridge watches it), and the next call
  starts it again.

The control plane keeps the hosts in memory. On a cold start it finds a host by its labels,
so sama needs no table for them.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol, TypeVar

from sama.connectors.manifest import McpHosted
from sama.connectors.mcp_remote import McpUnavailableError, RemoteTool
from sama.connectors.mcp_stdio import START_TIMEOUT_S, McpNotSentError, StdioMcpClient
from sama.sandbox.base import (
    Sandbox,
    SandboxError,
    SandboxNotFoundError,
    SandboxOutdatedError,
    SandboxSpec,
)

log: Final = logging.getLogger("sama.connectors.host")

DEFAULT_HOST_TEMPLATE: Final = "base"
"""The E2B template of a connector host. The public ``base`` template has Node.js (npx) and
Python 3, and it has no sama code and no sama secret."""
HOME: Final = "/home/user"
BRIDGE_PATH: Final = f"{HOME}/.sama/bridge.py"
BRIDGE_SOURCE: Final = (Path(__file__).with_name("host_bridge.py")).read_text(encoding="utf-8")
KIND_LABEL: Final = "sama.kind"
KIND: Final = "connector_host"
CONNECTION_LABEL: Final = "sama.connection_id"
HASH_LABEL: Final = "sama.host_hash"
KEEP_ALIVE_EVERY_S: Final = 60.0
"""A call extends the E2B timeout at most once in this time."""
REAPER_EVERY_S: Final = 30.0
E2B_MARGIN_S: Final = 120
"""The E2B timeout is the idle time plus this margin. E2B pauses the host if the reaper does
not."""

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class HostInfo:
    """A host that the backend found by its labels."""

    sandbox_id: str
    labels: Mapping[str, str]


class HostBackend(Protocol):
    """The sandboxes of the hosts: E2B in production, a fake in the tests."""

    async def find(self, labels: Mapping[str, str]) -> list[HostInfo]:
        """The sandboxes (running or paused) whose labels include ``labels``."""
        ...

    async def create(self, spec: SandboxSpec) -> Sandbox: ...

    async def resume(self, sandbox_id: str, spec: SandboxSpec) -> Sandbox: ...

    async def kill(self, sandbox_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class HostedServer:
    """What one connection runs. ``env`` holds the secrets, so it is not in ``repr``."""

    connection_id: str
    org_id: str
    hosted: McpHosted
    env: Mapping[str, str] = field(default_factory=dict, repr=False)

    def bridge_config(self) -> dict[str, Any]:
        """The argument of the bridge. It has no secrets."""
        hosted = self.hosted
        config: dict[str, Any] = {"memory_mb": hosted.memory_mb}
        if hosted.command is not None:
            config["command"] = list(hosted.command)
        elif hosted.package is not None:
            config.update(hosted.package.model_dump())
        return config

    def process_hash(self) -> str:
        """Changes when the server must start again: the program, the memory, or the env."""
        data = json.dumps([self.bridge_config(), sorted(self.env.items())], sort_keys=True)
        return hashlib.sha256(data.encode()).hexdigest()


@dataclass(slots=True)
class _Host:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sandbox: Sandbox | None = None
    sandbox_id: str | None = None
    """Also set when the host is paused."""
    box_hash: str | None = None
    client: StdioMcpClient | None = None
    process_hash: str | None = None
    idle_s: int = 600
    last_used: float = 0.0
    kept_alive: float = 0.0
    active: int = 0


class ConnectorHosts:
    """Starts, pauses, and stops the connector hosts, and speaks MCP to their servers."""

    def __init__(
        self,
        backend: HostBackend,
        *,
        template: str = DEFAULT_HOST_TEMPLATE,
        clock: Callable[[], float] = time.monotonic,
        reaper_every_s: float = REAPER_EVERY_S,
    ) -> None:
        self._backend = backend
        self._template = template
        self._clock = clock
        self._reaper_every_s = reaper_every_s
        self._hosts: dict[str, _Host] = {}
        self._reaper: asyncio.Task[None] | None = None

    # -- the API of the connection service -------------------------------------------------

    async def list_tools(self, server: HostedServer) -> list[RemoteTool]:
        """Starts the server if it does not run, and asks for its tools."""
        return await self._use(server, lambda client: client.list_tools(), retry=True)

    async def call(
        self, server: HostedServer, tool: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        """One ``tools/call``. A request that reached the server is not sent again."""
        return await self._use(server, lambda client: client.call_tool(tool, arguments))

    async def stop(self, connection_id: str) -> None:
        """Stops the server and kills the host (the connection is gone)."""
        host = self._hosts.pop(connection_id, None)
        if host is not None:
            async with host.lock:
                await self._close_client(host)
                if host.sandbox_id is not None:
                    await self._kill(host.sandbox_id)
        with contextlib.suppress(SandboxError):
            for info in await self._backend.find(self._labels(connection_id)):
                await self._kill(info.sandbox_id)

    async def restart(self, connection_id: str) -> None:
        """Stops the server. The next call starts it with the new values of the form."""
        host = self._hosts.get(connection_id)
        if host is not None:
            async with host.lock:
                await self._close_client(host)

    async def pause_idle(self) -> int:
        """Pauses each host without a call for its idle time. Returns how many it paused."""
        paused = 0
        now = self._clock()
        for host in list(self._hosts.values()):
            if host.sandbox is None or host.active or host.lock.locked():
                continue
            if now - host.last_used < host.idle_s:
                continue
            async with host.lock:
                if host.sandbox is None or host.active:
                    continue
                await self._close_client(host)
                try:
                    await host.sandbox.pause()
                    paused += 1
                except SandboxError as error:
                    log.warning("connector host pause failed: %s", error)
                host.sandbox = None
        return paused

    async def aclose(self) -> None:
        """Stops the reaper and the servers. The hosts stay: E2B pauses them."""
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        for host in self._hosts.values():
            await self._close_client(host)

    def spec(self, server: HostedServer) -> SandboxSpec:
        """The sandbox spec of the host. Its labels name the connection and hold no secret."""
        return SandboxSpec(
            org_id=server.org_id,
            session_id=f"host-{server.connection_id}",
            template=self._template,
            memory_mb=max(512, server.hosted.memory_mb),
            idle_timeout_s=server.hosted.idle_s + E2B_MARGIN_S,
            allow_out=server.hosted.allow_out,
            metadata={
                KIND_LABEL: KIND,
                CONNECTION_LABEL: server.connection_id,
                HASH_LABEL: self._box_hash(server),
            },
        )

    # -- internals -------------------------------------------------------------------------

    async def _use(
        self,
        server: HostedServer,
        action: Callable[[StdioMcpClient], Awaitable[T]],
        *,
        retry: bool = False,
    ) -> T:
        self._start_reaper()
        host = self._hosts.setdefault(server.connection_id, _Host())
        host.idle_s = server.hosted.idle_s
        for attempt in (1, 2):
            async with host.lock:
                try:
                    client = await self._ready(host, server)
                except SandboxError as error:
                    host.sandbox = None
                    await self._close_client(host)
                    if attempt == 2:
                        raise McpUnavailableError(
                            f"the connector host did not start: {error}"
                        ) from None
                    continue
                host.active += 1
            try:
                result = await action(client)
            except McpUnavailableError as error:
                if not client.alive:
                    async with host.lock:
                        if host.client is client:
                            await self._close_client(host)
                    if isinstance(error, McpNotSentError):
                        # The host can be paused or gone: find it again on the retry.
                        host.sandbox = None
                if (
                    attempt == 1
                    and not client.alive
                    and (retry or isinstance(error, McpNotSentError))
                ):
                    continue
                raise
            finally:
                host.active -= 1
                host.last_used = self._clock()
            await self._keep_alive(host)
            return result
        raise McpUnavailableError("the connector host did not start")  # pragma: no cover

    async def _ready(self, host: _Host, server: HostedServer) -> StdioMcpClient:
        """The client of a running server. Starts the host and the server if needed."""
        box_hash = self._box_hash(server)
        if host.box_hash is not None and host.box_hash != box_hash:
            await self._close_client(host)
            if host.sandbox_id is not None:
                await self._kill(host.sandbox_id)
            host.sandbox, host.sandbox_id = None, None
        if host.sandbox is None:
            await self._close_client(host)
            host.sandbox = await self._sandbox(host, server, box_hash)
            host.box_hash = box_hash
            host.kept_alive = self._clock()
        process_hash = server.process_hash()
        if host.client is not None and (not host.client.alive or host.process_hash != process_hash):
            await self._close_client(host)
        if host.client is None:
            host.client = await self._start_server(host.sandbox, server)
            host.process_hash = process_hash
        return host.client

    async def _sandbox(self, host: _Host, server: HostedServer, box_hash: str) -> Sandbox:
        spec = self.spec(server)
        candidates: list[str] = []
        if host.sandbox_id is not None:
            candidates.append(host.sandbox_id)
        else:
            for info in await self._backend.find(self._labels(server.connection_id)):
                if (
                    info.labels.get(HASH_LABEL) == box_hash
                    and info.labels.get("sama.org_id") == server.org_id
                    and not candidates
                ):
                    candidates.append(info.sandbox_id)
                else:
                    await self._kill(info.sandbox_id)  # Old settings, or a second copy.
        for sandbox_id in candidates:
            try:
                sandbox = await self._backend.resume(sandbox_id, spec)
            except SandboxOutdatedError as outdated:
                await outdated.sandbox.kill()
            except SandboxNotFoundError:
                pass
            else:
                host.sandbox_id = sandbox.id
                return sandbox
        sandbox = await self._backend.create(spec)
        host.sandbox_id = sandbox.id
        log.info("connector host created for connection %s", server.connection_id)
        return sandbox

    async def _start_server(self, sandbox: Sandbox, server: HostedServer) -> StdioMcpClient:
        await sandbox.write_file(BRIDGE_PATH, BRIDGE_SOURCE.encode())
        process = await sandbox.start(
            ["python3", "-u", BRIDGE_PATH, json.dumps(server.bridge_config())],
            env=dict(server.env),
            cwd=HOME,
        )
        client = StdioMcpClient(process)
        try:
            await client.initialize(timeout_s=START_TIMEOUT_S)
        except BaseException:
            await client.close()
            raise
        return client

    async def _keep_alive(self, host: _Host) -> None:
        now = self._clock()
        if host.sandbox is None or now - host.kept_alive < KEEP_ALIVE_EVERY_S:
            return
        host.kept_alive = now
        try:
            await host.sandbox.keep_alive(host.idle_s + E2B_MARGIN_S)
        except SandboxError as error:
            log.info("connector host keep_alive failed: %s", error)

    async def _close_client(self, host: _Host) -> None:
        client, host.client, host.process_hash = host.client, None, None
        if client is not None:
            await client.close()

    async def _kill(self, sandbox_id: str) -> None:
        try:
            await self._backend.kill(sandbox_id)
        except SandboxError as error:
            log.warning("connector host kill failed: %s", error)

    def _box_hash(self, server: HostedServer) -> str:
        """Changes when the host must be made again: the template or the egress."""
        data = json.dumps([self._template, list(server.hosted.allow_out)])
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    @staticmethod
    def _labels(connection_id: str) -> dict[str, str]:
        return {KIND_LABEL: KIND, CONNECTION_LABEL: connection_id}

    def _start_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap())

    async def _reap(self) -> None:
        while True:
            await asyncio.sleep(self._reaper_every_s)
            try:
                await self.pause_idle()
            except Exception:
                log.exception("connector host reaper failed")


class E2BHostBackend:
    """The hosts in E2B: the sama E2B provider, plus a search by labels and a kill by id."""

    def __init__(self, api_key: str) -> None:
        from sama.sandbox.e2b import E2BProvider

        self._api_key = api_key
        self._provider = E2BProvider(api_key=api_key)

    async def find(self, labels: Mapping[str, str]) -> list[HostInfo]:
        from e2b import AsyncSandbox, SandboxException
        from e2b.sandbox.sandbox_api import SandboxQuery

        found: list[HostInfo] = []
        try:
            pages = AsyncSandbox.list(
                query=SandboxQuery(metadata=dict(labels)), api_key=self._api_key
            )
            while pages.has_next and len(found) < 100:
                for item in await pages.next_items():
                    found.append(HostInfo(item.sandbox_id, dict(item.metadata or {})))
        except SandboxException as error:
            raise SandboxError(f"list failed: {type(error).__name__}") from None
        return found

    async def create(self, spec: SandboxSpec) -> Sandbox:
        return await self._provider.create(spec)

    async def resume(self, sandbox_id: str, spec: SandboxSpec) -> Sandbox:
        return await self._provider.resume(sandbox_id, spec)

    async def kill(self, sandbox_id: str) -> None:
        from e2b import AsyncSandbox, SandboxException

        try:
            await AsyncSandbox.kill(sandbox_id, api_key=self._api_key)
        except SandboxException as error:
            raise SandboxError(f"kill failed: {type(error).__name__}") from None
