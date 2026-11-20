"""The credentials and the sessions of remote MCP connections (docs/architecture.md,
Section 10.3).

``McpDriver`` makes the headers of a connection from its vault entry, keeps one MCP session
for each connection in memory, refreshes OAuth tokens, and runs the OAuth steps. The
connection service keeps the rows and the states.

The vault entry ``connection:<id>`` is a JSON object of secret strings only, so the redactor
knows each value (``sama.vault.store.secret_parts``):

- ``bearer`` and ``header``: ``token`` (the form field).
- ``oauth``: ``access_token``, ``refresh_token``, ``client_secret``, and, while the user signs
  in, ``state`` and ``verifier``. ``expires_at`` is a number.

The row config has the parts that are not secret: the endpoints of the authorization server,
the client id, and the redirect URI.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from collections.abc import Mapping
from typing import Any, Final

from pydantic import SecretStr

from sama.connectors.manifest import TOKEN_FIELD, McpRemote
from sama.connectors.mcp_remote import (
    McpAuthError,
    McpClient,
    McpError,
    McpSession,
    McpSessionGoneError,
    RemoteTool,
)
from sama.connectors.oauth import (
    AuthServer,
    OAuthClient,
    OAuthError,
    OAuthFlow,
    OAuthGrantError,
    Pkce,
    Tokens,
    authorization_url,
    new_state,
)
from sama.vault.crypto import VaultError
from sama.vault.store import Vault, connection_secret

log: Final = logging.getLogger("sama.connectors.mcp")


class McpNotReadyError(McpError):
    """The connection has no credential yet (no form, or no finished sign-in)."""


class McpStateError(McpError):
    """The ``state`` of an OAuth callback is not the open sign-in of the connection."""


class McpDriver:
    """One for the process. ``vault`` holds the credentials. Without it, only servers with
    ``auth: none`` can connect."""

    def __init__(
        self,
        vault: Vault | None,
        *,
        client: McpClient | None = None,
        oauth: OAuthFlow | None = None,
    ) -> None:
        self._vault = vault
        self._client = client or McpClient()
        self._oauth = oauth or OAuthFlow()
        self._sessions: dict[uuid.UUID, tuple[str, McpSession]] = {}
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._oauth.aclose()

    async def check_url(self, url: str) -> None:
        """Raises ``BlockedUrlError`` or ``FetchError`` if the outbound URL policy refuses the
        URL."""
        await self._client.check(url)

    def forget(self, connection_id: uuid.UUID) -> None:
        """Drops the session of a connection, for example after a delete or a new sign-in."""
        self._sessions.pop(connection_id, None)
        self._locks.pop(connection_id, None)

    # -- tools ------------------------------------------------------------------------------

    async def discover(
        self,
        org_id: uuid.UUID,
        connection_id: uuid.UUID,
        spec: McpRemote,
        config: Mapping[str, Any],
    ) -> list[RemoteTool]:
        """Starts a new session and reads the tool list. This is the health check."""
        for attempt in range(2):
            headers = await self._headers(org_id, connection_id, spec, config, renew=attempt > 0)
            try:
                session = await self._client.open(spec.url, headers)
                tools = await self._client.list_tools(spec.url, headers, session)
            except McpAuthError:
                if spec.auth != "oauth" or attempt > 0:
                    raise
                continue
            self._sessions[connection_id] = (_fingerprint(spec.url, headers), session)
            return tools
        raise McpAuthError("the server refused the credential")  # pragma: no cover

    async def call(
        self,
        org_id: uuid.UUID,
        connection_id: uuid.UUID,
        spec: McpRemote,
        config: Mapping[str, Any],
        remote: str,
        arguments: Mapping[str, Any],
    ) -> str:
        """Runs one remote tool. A forgotten session starts again once. A refused OAuth token
        is refreshed once. The server did not run the call in both cases."""
        renew = False
        for attempt in range(3):
            headers = await self._headers(org_id, connection_id, spec, config, renew=renew)
            renew = False
            mark = _fingerprint(spec.url, headers)
            cached = self._sessions.get(connection_id)
            try:
                if cached is not None and cached[0] == mark:
                    session = cached[1]
                else:
                    session = await self._client.open(spec.url, headers)
                    self._sessions[connection_id] = (mark, session)
                return await self._client.call_tool(spec.url, headers, session, remote, arguments)
            except McpSessionGoneError:
                self._sessions.pop(connection_id, None)
                if attempt == 2:
                    raise
            except McpAuthError:
                self._sessions.pop(connection_id, None)
                if spec.auth != "oauth" or attempt == 2:
                    raise
                renew = True
        raise McpError("the call did not finish")  # pragma: no cover

    # -- OAuth ------------------------------------------------------------------------------

    async def begin_oauth(
        self,
        org_id: uuid.UUID,
        connection_id: uuid.UUID,
        spec: McpRemote,
        *,
        redirect_uri: str,
        actor: uuid.UUID | None,
    ) -> tuple[dict[str, Any], str]:
        """Discovers the authorization server, registers a client, and stores the PKCE
        verifier and the state. Returns the ``oauth`` config of the row and the sign-in URL."""
        vault = self._require_vault()
        server = await self._oauth.discover(spec.url, scope=spec.scope)
        client = await self._oauth.register(server, redirect_uri=redirect_uri)
        pkce = Pkce.new()
        state = new_state(str(connection_id))
        secret: dict[str, Any] = {"state": state, "verifier": pkce.verifier}
        if client.client_secret:
            secret["client_secret"] = client.client_secret
        await vault.put(
            org_id, connection_secret(connection_id), json.dumps(secret), actor_user_id=actor
        )
        self.forget(connection_id)
        config = {
            **server.config(),
            "client_id": client.client_id,
            "auth_method": client.auth_method,
            "redirect_uri": redirect_uri,
        }
        url = authorization_url(server, client, redirect_uri=redirect_uri, state=state, pkce=pkce)
        return config, url

    async def finish_oauth(
        self,
        org_id: uuid.UUID,
        connection_id: uuid.UUID,
        config: Mapping[str, Any],
        *,
        state: str,
        code: str | None,
        actor: uuid.UUID | None,
    ) -> None:
        """Checks the state, then exchanges the code. The state and the verifier are used
        once: a second callback with the same state fails. ``code=None`` (the user refused)
        only uses them up."""
        vault = self._require_vault()
        secret = await self._secret(org_id, connection_id, fresh=True)
        expected = secret.get("state")
        verifier = secret.get("verifier")
        if (
            not isinstance(expected, str)
            or not isinstance(verifier, str)
            or not hmac.compare_digest(expected.encode(), state.encode())
        ):
            raise McpStateError("the sign-in answer does not match this connection. Connect again")
        oauth = config.get("oauth")
        server = AuthServer.from_config(oauth)
        if server is None or not isinstance(oauth, Mapping):
            raise OAuthError("the connection has no authorization server. Connect again")
        client_secret = secret.get("client_secret")
        kept = {"client_secret": client_secret} if isinstance(client_secret, str) else {}
        name = connection_secret(connection_id)
        await vault.put(org_id, name, json.dumps(kept), actor_user_id=actor)
        if code is None:
            return
        client = OAuthClient(
            str(oauth.get("client_id")),
            client_secret if isinstance(client_secret, str) else None,
            str(oauth.get("auth_method") or "none"),
        )
        tokens = await self._oauth.exchange(
            server,
            client,
            code=code,
            verifier=verifier,
            redirect_uri=str(oauth.get("redirect_uri") or ""),
        )
        await vault.put(
            org_id, name, json.dumps({**kept, **_token_fields(tokens)}), actor_user_id=actor
        )
        self.forget(connection_id)

    # -- credentials ------------------------------------------------------------------------

    async def _headers(
        self,
        org_id: uuid.UUID,
        connection_id: uuid.UUID,
        spec: McpRemote,
        config: Mapping[str, Any],
        *,
        renew: bool,
    ) -> dict[str, str]:
        if spec.auth == "none":
            return {}
        if spec.auth in ("bearer", "header"):
            token = (await self._secret(org_id, connection_id)).get(TOKEN_FIELD)
            if not isinstance(token, str) or not token:
                raise McpNotReadyError("the connection has no token yet")
            if spec.auth == "bearer":
                return {"authorization": f"Bearer {token}"}
            if spec.header is None:
                raise McpError("the connector has no credential header")
            return {spec.header: token}
        tokens = await self._tokens(org_id, connection_id, config, renew=renew)
        return {"authorization": f"Bearer {tokens.access_token}"}

    async def _tokens(
        self,
        org_id: uuid.UUID,
        connection_id: uuid.UUID,
        config: Mapping[str, Any],
        *,
        renew: bool,
    ) -> Tokens:
        current = _tokens_of(await self._secret(org_id, connection_id))
        if current is None:
            raise McpNotReadyError("the sign-in is not finished")
        if current.fresh() and not renew:
            return current
        lock = self._locks.setdefault(connection_id, asyncio.Lock())
        async with lock:
            # Another task can have refreshed the token while this one waited.
            secret = await self._secret(org_id, connection_id, fresh=True)
            latest = _tokens_of(secret)
            if latest is None:
                raise McpNotReadyError("the sign-in is not finished")
            if latest.access_token != current.access_token and latest.fresh():
                return latest
            if latest.refresh_token is None:
                raise McpAuthError("the access token expired and there is no refresh token")
            oauth = config.get("oauth")
            server = AuthServer.from_config(oauth)
            if server is None or not isinstance(oauth, Mapping):
                raise McpAuthError("the connection has no authorization server")
            client_secret = secret.get("client_secret")
            client = OAuthClient(
                str(oauth.get("client_id")),
                client_secret if isinstance(client_secret, str) else None,
                str(oauth.get("auth_method") or "none"),
            )
            try:
                tokens = await self._oauth.refresh(server, client, latest.refresh_token)
            except OAuthGrantError as error:
                raise McpAuthError(str(error)) from None
            kept = {k: v for k, v in secret.items() if k == "client_secret"}
            await self._require_vault().put(
                org_id,
                connection_secret(connection_id),
                json.dumps({**kept, **_token_fields(tokens)}),
            )
            return tokens

    async def _secret(
        self, org_id: uuid.UUID, connection_id: uuid.UUID, *, fresh: bool = False
    ) -> dict[str, Any]:
        if self._vault is None:
            return {}
        try:
            stored: SecretStr | None = await self._vault.get(
                org_id, connection_secret(connection_id), fresh=fresh
            )
        except VaultError:
            log.error("the vault entry of connection=%s does not decrypt", connection_id)
            return {}
        if stored is None:
            return {}
        try:
            data = json.loads(stored.get_secret_value())
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _require_vault(self) -> Vault:
        if self._vault is None:
            raise McpError("the vault is off in this process")
        return self._vault


def _token_fields(tokens: Tokens) -> dict[str, Any]:
    out: dict[str, Any] = {"access_token": tokens.access_token}
    if tokens.refresh_token:
        out["refresh_token"] = tokens.refresh_token
    if tokens.expires_at is not None:
        out["expires_at"] = tokens.expires_at
    return out


def _tokens_of(secret: Mapping[str, Any]) -> Tokens | None:
    access = secret.get("access_token")
    if not isinstance(access, str) or not access:
        return None
    refresh = secret.get("refresh_token")
    expires = secret.get("expires_at")
    return Tokens(
        access_token=access,
        refresh_token=refresh if isinstance(refresh, str) and refresh else None,
        expires_at=float(expires) if isinstance(expires, (int, float)) else None,
    )


def _fingerprint(url: str, headers: Mapping[str, str]) -> str:
    """A hash of the URL and the credential. A new credential starts a new session."""
    data = json.dumps([url, sorted(headers.items())])
    return hashlib.sha256(data.encode()).hexdigest()


__all__ = ["McpDriver", "McpNotReadyError", "McpStateError"]
