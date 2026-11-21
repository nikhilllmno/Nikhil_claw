"""The SSH tunnel route, against a real bastion in docker (docs/architecture.md, Sections 10.6
and 16.4).

Runs only with ``SAMA_TEST_SSH_URL`` (for example ``ssh://sama@localhost:52222``) and
``SAMA_TEST_SSH_KEY_FILE`` (the private key of that user). The bastion reaches the databases
by their names on the docker network: ``SAMA_TEST_SSH_MYSQL_URL`` (default
``mysql://root:rootpw@sama-mysql:3306``) and ``SAMA_TEST_SSH_POSTGRES_URL`` (default
``postgresql://postgres:postgres@sama-pgvector:5432``). ``scripts/test-databases.sh`` starts
them all.
"""

from __future__ import annotations

import ipaddress
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import asyncssh
import pytest

from sama.connectors.database import DatabaseGateway
from sama.connectors.http_api import DriverError
from sama.connectors.manifest import Catalog, Manifest
from sama.tools.outbound import (
    BlockedUrlError,
    IPAddress,
    OutboundClient,
    UrlPolicy,
    system_resolver,
)
from tests.connectors.support import CATALOG
from tests.db.test_database_engines import CLAIMS

TARGETS = {
    "mysql": ("SAMA_TEST_SSH_MYSQL_URL", "mysql://root:rootpw@sama-mysql:3306", "mysql"),
    "postgres": (
        "SAMA_TEST_SSH_POSTGRES_URL",
        "postgresql://postgres:postgres@sama-pgvector:5432",
        "postgres",
    ),
}


def manifest(name: str) -> Manifest:
    found = Catalog.load(CATALOG).get(name)
    assert found is not None
    return found


BASTION = "bastion.test"
"""The name of the bastion in the forms. The resolver of the tests gives the address."""


@dataclass
class Bastion:
    address: str
    port: int
    user: str
    private_key: str
    host_key: str

    def values(self, connector: str) -> dict[str, str]:
        env, default, database = TARGETS[connector]
        parts = urlsplit(os.environ.get(env) or default)
        return {
            "host": parts.hostname or "",
            "port": str(parts.port),
            "database": database,
            "user": parts.username or "",
            "password": parts.password or "",
            "sslmode": "disable",
            "ssh_host": BASTION,
            "ssh_port": str(self.port),
            "ssh_user": self.user,
            "ssh_private_key": self.private_key,
            "ssh_host_key": self.host_key,
        }


def _read(path: str) -> str:
    return Path(path).read_text()


@pytest.fixture
async def bastion() -> Bastion:
    url = os.environ.get("SAMA_TEST_SSH_URL")
    key_file = os.environ.get("SAMA_TEST_SSH_KEY_FILE")
    if not url or not key_file:
        pytest.skip("set SAMA_TEST_SSH_URL and SAMA_TEST_SSH_KEY_FILE (scripts/test-databases.sh)")
    parts = urlsplit(url)
    host, port = parts.hostname or "localhost", parts.port or 22
    key = await asyncssh.get_server_host_key(host, port)
    assert key is not None
    address = (await system_resolver(host, port))[0]
    return Bastion(
        str(address),
        port,
        parts.username or "",
        _read(key_file),
        key.export_public_key().decode().strip(),
    )


@pytest.fixture
async def gw(bastion: Bastion) -> AsyncIterator[DatabaseGateway]:
    # Only the bastion is a private host that the gateway can use. In the app no host is.
    async def resolve(host: str, port: int) -> list[IPAddress]:
        if host == BASTION:
            return [ipaddress.ip_address(bastion.address)]
        return list(await system_resolver(host, port))

    gateway = DatabaseGateway(
        allow_private=frozenset({BASTION}), require_tls=True, resolver=resolve
    )
    try:
        yield gateway
    finally:
        await gateway.aclose()


async def run(
    gw: DatabaseGateway,
    connector: str,
    values: dict[str, str],
    operation: str,
    connection_id: uuid.UUID | None = None,
    **arguments: Any,
) -> str:
    found = manifest(connector)
    tool = next(t for t in found.tools if t.name == f"db_{connector}_{operation}")
    return await gw.run(CLAIMS, connection_id or uuid.uuid4(), found, tool, values, arguments)


def tunnel_port(gw: DatabaseGateway, connection_id: uuid.UUID) -> int:
    return gw._tunnels._tunnels[str(connection_id)].target.port


@pytest.mark.parametrize("connector", ["mysql", "postgres"])
async def test_a_query_goes_through_the_tunnel(
    gw: DatabaseGateway, bastion: Bastion, connector: str
) -> None:
    values = bastion.values(connector)
    connection_id = uuid.uuid4()
    answer = await run(gw, connector, values, "query", connection_id, sql="select 41 + 1 as n")
    assert "42" in answer
    assert gw._tunnels.open_count() == 1
    # The second call uses the same tunnel.
    port = tunnel_port(gw, connection_id)
    await run(gw, connector, values, "query", connection_id, sql="select 1 as n")
    assert tunnel_port(gw, connection_id) == port
    await gw.test(manifest(connector), values)


async def test_another_connection_and_web_extract_cannot_use_the_tunnel(
    gw: DatabaseGateway, bastion: Bastion
) -> None:
    values = bastion.values("mysql")
    connection_id = uuid.uuid4()
    await run(gw, "mysql", values, "query", connection_id, sql="select 1 as n")
    port = tunnel_port(gw, connection_id)
    other = {
        **values,
        "host": "127.0.0.1",
        "port": str(port),
        "ssh_host": "",
        "ssh_user": "",
        "ssh_private_key": "",
        "ssh_host_key": "",
        "sslmode": "require",
    }
    # Another connection of the same gateway, with the local end of the tunnel as its host.
    for host in ("127.0.0.1", "localhost", "[::ffff:127.0.0.1]"):
        with pytest.raises(DriverError, match=r"public|not valid|host"):
            await run(gw, "mysql", {**other, "host": host}, "query", sql="select 1 as n")
    # The tunnel of the first connection is still the only tunnel, and it still works.
    assert gw._tunnels.open_count() == 1
    await run(gw, "mysql", values, "query", connection_id, sql="select 1 as n")
    # web_extract and every other outbound request use OutboundClient, which refuses loopback.
    client = OutboundClient(UrlPolicy(allow_http=True))
    try:
        for url in (f"http://127.0.0.1:{port}/", f"http://localhost:{port}/"):
            with pytest.raises(BlockedUrlError):
                await client.check(url)
    finally:
        await client.aclose()


async def test_a_wrong_host_key_is_refused(gw: DatabaseGateway, bastion: Bastion) -> None:
    other = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode().strip()
    values = {**bastion.values("mysql"), "ssh_host_key": other}
    with pytest.raises(DriverError, match="host key"):
        await run(gw, "mysql", values, "query", sql="select 1 as n")
    assert gw._tunnels.open_count() == 0


async def test_a_fingerprint_is_a_host_key(gw: DatabaseGateway, bastion: Bastion) -> None:
    key = asyncssh.import_public_key(bastion.host_key)
    values = {**bastion.values("mysql"), "ssh_host_key": key.get_fingerprint("sha256")}
    assert "1" in await run(gw, "mysql", values, "query", sql="select 1 as n")


async def test_a_wrong_ssh_key_is_refused(gw: DatabaseGateway, bastion: Bastion) -> None:
    other = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()
    values = {**bastion.values("mysql"), "ssh_private_key": other}
    with pytest.raises(DriverError, match="refused the login"):
        await run(gw, "mysql", values, "query", sql="select 1 as n")


async def test_the_public_gateway_refuses_a_private_bastion(bastion: Bastion) -> None:
    gateway = DatabaseGateway()
    try:
        values = {**bastion.values("mysql"), "ssh_host": bastion.address}
        with pytest.raises(DriverError, match="public"):
            await gateway.test(manifest("mysql"), values)
    finally:
        await gateway.aclose()
