"""OAuth 2.1 for remote MCP servers and the MCP driver (docs/architecture.md, Section 10.3)."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr

from sama.connectors.manifest import McpRemote
from sama.connectors.mcp_driver import McpNotReadyError, McpStateError
from sama.connectors.mcp_remote import McpAuthError
from sama.connectors.oauth import (
    AuthServer,
    OAuthError,
    OAuthFlow,
    OAuthGrantError,
    Pkce,
    authorization_url,
)
from sama.vault.store import Vault, VaultName, connection_secret
from tests.connectors.fake_mcp import GOOD_TOKEN, HOST, URL, FakeMcp, driver, outbound

ORG = uuid.uuid4()
REDIRECT = "https://app.sama.test/connections"


class MemoryVault:
    """The ``get`` and ``put`` of ``Vault``, in memory."""

    def __init__(self) -> None:
        self.entries: dict[str, str] = {}
        self.fresh_reads = 0

    async def get(
        self, org_id: uuid.UUID, name: VaultName, *, fresh: bool = False
    ) -> SecretStr | None:
        self.fresh_reads += int(fresh)
        value = self.entries.get(f"{org_id}/{name}")
        return None if value is None else SecretStr(value)

    async def put(
        self, org_id: uuid.UUID, name: VaultName, value: str, *, actor_user_id: Any = None
    ) -> None:
        self.entries[f"{org_id}/{name}"] = value

    def read(self, connection_id: uuid.UUID) -> dict[str, Any]:
        return cast(
            dict[str, Any], json.loads(self.entries[f"{ORG}/{connection_secret(connection_id)}"])
        )


def spec(auth: str = "oauth", **more: Any) -> McpRemote:
    return McpRemote(url=URL, auth=auth, **more)  # type: ignore[arg-type]


async def test_discovery_finds_the_authorization_server() -> None:
    flow = OAuthFlow(outbound(FakeMcp("oauth")))
    server = await flow.discover(URL)
    assert server.issuer == f"https://{HOST}"
    assert server.token_endpoint == f"https://{HOST}/token"
    assert server.resource == URL
    assert server.scope is None
    assert (await flow.discover(URL, scope="read")).scope == "read"
    assert AuthServer.from_config(server.config()) == server
    assert AuthServer.from_config({"issuer": "x"}) is None
    await flow.aclose()


async def test_discovery_without_metadata_uses_the_default_paths() -> None:
    fake = FakeMcp("bearer")
    flow = OAuthFlow(outbound(fake))
    server = await flow.discover(URL)
    assert server.authorization_endpoint == f"https://{HOST}/authorize"
    assert server.registration_endpoint == f"https://{HOST}/register"
    await flow.aclose()


def test_the_authorization_url_has_pkce_resource_and_state() -> None:
    server = AuthServer(
        issuer="https://a.test",
        authorization_endpoint="https://a.test/authorize?x=1",
        token_endpoint="https://a.test/token",
        registration_endpoint=None,
        resource=URL,
        scope="read write",
    )
    pkce = Pkce.new()
    url = authorization_url(server, _client("c1"), redirect_uri=REDIRECT, state="s.1", pkce=pkce)
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert query["x"] == "1"
    assert query["code_challenge"] == pkce.challenge != pkce.verifier
    assert (query["resource"], query["scope"], query["state"]) == (URL, "read write", "s.1")
    assert query["code_challenge_method"] == "S256"


def _client(client_id: str) -> Any:
    from sama.connectors.oauth import OAuthClient

    return OAuthClient(client_id)


async def test_register_needs_a_registration_endpoint() -> None:
    flow = OAuthFlow(outbound(FakeMcp("oauth")))
    server = await flow.discover(URL)
    without = AuthServer(**{**server.config(), "registration_endpoint": None})
    with pytest.raises(OAuthError, match="dynamic client registration"):
        await flow.register(without, redirect_uri=REDIRECT)
    await flow.aclose()


async def test_the_whole_oauth_flow_and_a_refresh() -> None:
    fake = FakeMcp("oauth")
    fake.client_secret = "dcr-secret-value"
    vault = MemoryVault()
    mcp = driver(fake, cast(Vault, vault))
    connection = uuid.uuid4()
    config, url = await mcp.begin_oauth(ORG, connection, spec(), redirect_uri=REDIRECT, actor=None)
    assert fake.registered[0]["redirect_uris"] == [REDIRECT]
    assert config["client_id"] == "client-1"
    assert config["redirect_uri"] == REDIRECT
    assert "dcr-secret-value" not in json.dumps(config)
    stored = vault.read(connection)
    assert set(stored) == {"state", "verifier", "client_secret"}
    with pytest.raises(McpNotReadyError):
        await mcp.discover(ORG, connection, spec(), {"oauth": config})
    code, state = fake.approve(url)
    assert state.startswith(f"{connection}.")
    # A wrong state changes nothing.
    with pytest.raises(McpStateError):
        await mcp.finish_oauth(
            ORG, connection, {"oauth": config}, state=state + "x", code=code, actor=None
        )
    assert vault.read(connection) == stored
    await mcp.finish_oauth(ORG, connection, {"oauth": config}, state=state, code=code, actor=None)
    stored = vault.read(connection)
    assert set(stored) == {"client_secret", "access_token", "refresh_token", "expires_at"}
    assert fake.token_requests[-1]["client_secret"] == "dcr-secret-value"
    # The state is used once.
    with pytest.raises(McpStateError):
        await mcp.finish_oauth(
            ORG, connection, {"oauth": config}, state=state, code=code, actor=None
        )
    tools = await mcp.discover(ORG, connection, spec(), {"oauth": config})
    assert len(tools) == 3
    # An old token is refreshed before the request.
    stored["expires_at"] = time.time() - 10
    vault.entries[f"{ORG}/{connection_secret(connection)}"] = json.dumps(stored)
    out = await mcp.call(ORG, connection, spec(), {"oauth": config}, "list_items", {})
    assert out.startswith("ran list_items")
    assert vault.read(connection)["access_token"] == "access-2"
    assert fake.token_requests[-1]["grant_type"] == "refresh_token"
    # The server refuses the token: one refresh, then the call runs.
    fake.tokens = set()
    fake.refresh_tokens = {"refresh-2"}
    out = await mcp.call(ORG, connection, spec(), {"oauth": config}, "list_items", {})
    assert out.startswith("ran list_items")
    assert vault.read(connection)["access_token"] == "access-3"
    # A refresh token that the server does not know: the connection must sign in again.
    fake.tokens = set()
    fake.refresh_tokens = set()
    with pytest.raises(McpAuthError):
        await mcp.call(ORG, connection, spec(), {"oauth": config}, "list_items", {})
    await mcp.aclose()


async def test_a_refused_sign_in_uses_up_the_state() -> None:
    fake = FakeMcp("oauth")
    vault = MemoryVault()
    mcp = driver(fake, cast(Vault, vault))
    connection = uuid.uuid4()
    config, url = await mcp.begin_oauth(ORG, connection, spec(), redirect_uri=REDIRECT, actor=None)
    _, state = fake.approve(url)
    await mcp.finish_oauth(ORG, connection, {"oauth": config}, state=state, code=None, actor=None)
    assert vault.read(connection) == {}
    bad_code = "code-unknown"
    config2, url2 = await mcp.begin_oauth(
        ORG, connection, spec(), redirect_uri=REDIRECT, actor=None
    )
    _, state2 = fake.approve(url2)
    with pytest.raises(OAuthGrantError):
        await mcp.finish_oauth(
            ORG, connection, {"oauth": config2}, state=state2, code=bad_code, actor=None
        )


async def test_bearer_and_header_credentials() -> None:
    vault = MemoryVault()
    connection = uuid.uuid4()
    fake = FakeMcp("bearer")
    mcp = driver(fake, cast(Vault, vault))
    with pytest.raises(McpNotReadyError):
        await mcp.discover(ORG, connection, spec("bearer"), {})
    await vault.put(ORG, connection_secret(connection), json.dumps({"token": GOOD_TOKEN}))
    assert len(await mcp.discover(ORG, connection, spec("bearer"), {})) == 3
    assert fake.seen[-1].headers["authorization"] == f"Bearer {GOOD_TOKEN}"
    # A refused bearer token is not refreshed.
    fake.tokens = set()
    with pytest.raises(McpAuthError):
        await mcp.call(ORG, connection, spec("bearer"), {}, "list_items", {})
    header = FakeMcp("header")
    mcp2 = driver(header, cast(Vault, vault))
    remote = spec("header", header="X-Api-Key")
    assert len(await mcp2.discover(ORG, connection, remote, {})) == 3
    assert header.seen[-1].headers["x-api-key"] == GOOD_TOKEN
    assert "authorization" not in header.seen[-1].headers
    await mcp.aclose()
    await mcp2.aclose()


async def test_the_driver_keeps_the_session_and_opens_a_new_one_when_it_is_gone() -> None:
    fake = FakeMcp("none")
    mcp = driver(fake, None)
    connection = uuid.uuid4()
    await mcp.discover(ORG, connection, spec("none"), {})
    await mcp.call(ORG, connection, spec("none"), {}, "list_items", {"q": "1"})
    await mcp.call(ORG, connection, spec("none"), {}, "list_items", {"q": "2"})
    assert fake.opened == 1
    fake.sessions.clear()
    out = await mcp.call(ORG, connection, spec("none"), {}, "list_items", {"q": "3"})
    assert out == 'ran list_items {"q": "3"}'
    assert fake.opened == 2
    # The call that found no session did not run.
    assert [c[1]["q"] for c in fake.calls] == ["1", "2", "3"]
    mcp.forget(connection)
    await mcp.call(ORG, connection, spec("none"), {}, "list_items", {})
    assert fake.opened == 3
    await mcp.aclose()
