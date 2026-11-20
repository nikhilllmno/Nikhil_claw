"""SSH tunnels of database connections (docs/architecture.md, Section 10.6).

A connection with an SSH host goes through the bastion of the customer. The gateway checks
the bastion like a database host (a public address, no second DNS answer), checks the host
key of the bastion against the key in the form, and logs in with the private key from the
vault. The bastion connects to the database host, which can have a private address.

The tunnel of a connection listens on ``127.0.0.1`` (a free port) of the gateway process.
Only that connection uses the port: every other database connection and every other
outbound request refuses loopback addresses (Section 16.4). The tunnels are kept for each
connection and close when idle, as the pools do.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

import asyncssh

from sama.connectors.dbcore import Target
from sama.connectors.dbsettings import SshSettings
from sama.connectors.http_api import DriverError, DriverUnavailableError

log: Final = logging.getLogger("sama.connectors.tunnel")

CONNECT_TIMEOUT_S: Final = 10.0
IDLE_S: Final = 300.0


class HostKeyMismatchError(DriverError):
    """The bastion sent another host key."""


def host_key_matches(expected: str, key: asyncssh.SSHKey) -> bool:
    """Is ``key`` the key of the form? ``expected`` is a public key or a SHA256
    fingerprint."""
    if expected.startswith("SHA256:"):
        return key.get_fingerprint("sha256").rstrip("=") == expected.rstrip("=")
    try:
        wanted = asyncssh.import_public_key(expected)
    except (asyncssh.KeyImportError, ValueError):
        return False
    return bool(wanted.public_data == key.public_data)


class _Client(asyncssh.SSHClient):
    def __init__(self, expected: str) -> None:
        self.expected = expected
        self.seen: str | None = None

    def validate_host_public_key(self, host: str, addr: str, port: int, key: Any) -> bool:
        self.seen = key.get_fingerprint("sha256")
        return host_key_matches(self.expected, key)


async def open_ssh(
    ssh: SshSettings, address: str, *, timeout_s: float = CONNECT_TIMEOUT_S
) -> asyncssh.SSHClientConnection:
    """An SSH connection to the checked address of the bastion. Raises ``DriverError``."""
    try:
        key = asyncssh.import_private_key(ssh.private_key)
    except (asyncssh.KeyImportError, ValueError):
        raise DriverError(
            "the SSH private key cannot be read (it must have no passphrase)"
        ) from None
    client = _Client(ssh.host_key)
    try:
        connection, _ = await asyncio.wait_for(
            asyncssh.create_connection(
                lambda: client,
                address,
                ssh.port,
                username=ssh.user,
                client_keys=[key],
                known_hosts=([], [], []),  # The client checks the key; no file is read.
                agent_path=None,
                config=None,
                password=None,
                kbdint_auth=False,
                gss_auth=False,
                host_based_auth=False,
                connect_timeout=timeout_s,
                login_timeout=timeout_s,
                keepalive_interval=30,
            ),
            timeout_s + 2,
        )
    except asyncssh.HostKeyNotVerifiable:
        raise HostKeyMismatchError(
            f"the SSH host key of {ssh.host} does not match the host key of the connection "
            f"(the bastion sent {client.seen or 'another key'})"
        ) from None
    except asyncssh.PermissionDenied:
        raise DriverError(f"the SSH bastion {ssh.host} refused the login of {ssh.user}") from None
    except asyncssh.DisconnectError as error:
        raise DriverUnavailableError(
            f"the SSH bastion {ssh.host} closed the connection: {error.reason[:200]}"
        ) from None
    except (OSError, TimeoutError) as error:
        raise DriverUnavailableError(
            f"the SSH bastion {ssh.host}:{ssh.port} cannot be reached ({type(error).__name__})"
        ) from None
    return connection


async def forward(
    connection: asyncssh.SSHClientConnection, dest_host: str, dest_port: int
) -> asyncssh.SSHListener:
    """A listener on ``127.0.0.1`` (a free port) that goes to the destination."""
    try:
        return await connection.forward_local_port("127.0.0.1", 0, dest_host, dest_port)
    except (OSError, asyncssh.Error) as error:
        raise DriverUnavailableError(
            f"the SSH tunnel cannot start ({type(error).__name__})"
        ) from None


@dataclass(slots=True)
class _Tunnel:
    fingerprint: str
    connection: asyncssh.SSHClientConnection
    listener: asyncssh.SSHListener
    used_at: float
    borrowers: int = 0
    retired: bool = False

    @property
    def target(self) -> Target:
        return Target("127.0.0.1", self.listener.get_port())


class Tunnels:
    """The SSH tunnels of the gateway, one for each connection (by key)."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        idle_s: float = IDLE_S,
    ) -> None:
        self._clock = clock
        self._idle_s = idle_s
        self._tunnels: dict[str, _Tunnel] = {}
        self._lock = asyncio.Lock()
        self._key_locks: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        async with self._lock:
            tunnels, self._tunnels = list(self._tunnels.values()), {}
        for tunnel in tunnels:
            await _close(tunnel)

    @contextlib.asynccontextmanager
    async def route(
        self,
        key: str,
        fingerprint: str,
        opener: Callable[[], Awaitable[tuple[asyncssh.SSHClientConnection, asyncssh.SSHListener]]],
    ) -> AsyncIterator[Target]:
        """The local end of the tunnel of ``key``. ``opener`` makes a new tunnel when no
        tunnel with this fingerprint is open. The tunnel stays open while it is used."""
        tunnel = await self._borrow(key, fingerprint, opener)
        try:
            yield tunnel.target
        finally:
            await asyncio.shield(self._give_back(tunnel))

    async def _borrow(
        self,
        key: str,
        fingerprint: str,
        opener: Callable[[], Awaitable[tuple[asyncssh.SSHClientConnection, asyncssh.SSHListener]]],
    ) -> _Tunnel:
        # One lock for each key: a slow bastion does not stop the tunnels of other
        # connections.
        async with self._key_locks.setdefault(key, asyncio.Lock()):
            now = self._clock()
            stale: list[_Tunnel] = []
            async with self._lock:
                for other, entry in list(self._tunnels.items()):
                    expired = now - entry.used_at > self._idle_s and entry.borrowers == 0
                    changed = other == key and entry.fingerprint != fingerprint
                    if expired or changed or entry.connection.is_closed():
                        self._tunnels.pop(other)
                        entry.retired = True
                        if entry.borrowers == 0:
                            stale.append(entry)
                found = self._tunnels.get(key)
                if found is not None:
                    found.used_at = now
                    found.borrowers += 1
            for old in stale:
                await asyncio.shield(_close(old))
            if found is None:
                connection, listener = await opener()
                found = _Tunnel(fingerprint, connection, listener, self._clock(), borrowers=1)
                async with self._lock:
                    self._tunnels[key] = found
            return found

    async def _give_back(self, tunnel: _Tunnel) -> None:
        async with self._lock:
            tunnel.borrowers -= 1
            tunnel.used_at = self._clock()
            close = tunnel.retired and tunnel.borrowers == 0
        if close:
            await _close(tunnel)

    async def discard(self, key: str) -> None:
        """Closes the tunnel of ``key`` when it has no borrowers (for example after a test of
        a form)."""
        async with self._lock:
            entry = self._tunnels.pop(key, None)
            self._key_locks.pop(key, None)
            if entry is None:
                return
            entry.retired = True
            close = entry.borrowers == 0
        if close:
            await _close(entry)

    def open_count(self) -> int:
        return len(self._tunnels)


async def _close(tunnel: _Tunnel) -> None:
    try:
        tunnel.listener.close()
        tunnel.connection.close()
        await asyncio.wait_for(tunnel.connection.wait_closed(), 5)
    except Exception:  # a tunnel that does not close is dropped
        log.warning("ssh tunnel close failed")
