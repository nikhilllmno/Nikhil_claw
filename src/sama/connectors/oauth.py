"""OAuth 2.1 for remote MCP servers (docs/architecture.md, Section 10.3).

sama is a public OAuth client of the server. The flow follows the MCP authorization
specification (version 2025-06-18):

1. Discovery. sama sends ``initialize`` without a token. The ``401`` answer names the protected
   resource metadata (RFC 9728) in ``WWW-Authenticate``. Else sama reads
   ``/.well-known/oauth-protected-resource`` of the server. The metadata names the
   authorization server, and sama reads its metadata (RFC 8414, or OpenID Connect discovery).
   A server without the metadata gets the default paths ``/authorize``, ``/token``, and
   ``/register`` of its origin.
2. Registration. sama registers a client with dynamic client registration (RFC 7591). The
   redirect URI is the Connections page of the web app.
3. Authorization. The user opens the authorization URL. It has PKCE (``S256``), a ``state``,
   and the ``resource`` parameter (RFC 8707) with the URL of the MCP server. A server that
   names its PKCE methods without ``S256`` is refused.
4. The web app gets the code and gives it to the API. The API checks the ``state`` and
   exchanges the code with the PKCE verifier.
5. The access token goes into the ``Authorization`` header. sama refreshes it before it
   expires, and after a ``401``.

Every request goes through ``OutboundClient`` (Section 16.4). The tokens, the client secret,
and the PKCE verifier are in the vault entry of the connection, never in the row or a log.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlencode, urlsplit

from sama.connectors.errors import short_error
from sama.connectors.mcp_remote import (
    INITIALIZE_PARAMS,
    McpError,
    McpUnavailableError,
)
from sama.tools.outbound import BlockedUrlError, FetchError, FetchResult, OutboundClient, UrlPolicy

POLICY: Final = UrlPolicy(
    max_redirects=0, max_bytes=1_000_000, hop_timeout_s=20.0, total_timeout_s=30.0
)
REFRESH_MARGIN_S: Final = 60.0
"""sama refreshes a token this many seconds before it expires."""
_RESOURCE_METADATA: Final = re.compile(r'resource_metadata="([^"]+)"')
_SCOPE: Final = re.compile(r'scope="([^"]+)"')


class OAuthError(McpError):
    """The OAuth flow cannot go on. The message is short and safe to show."""


class OAuthUnavailableError(McpUnavailableError):
    """The authorization server did not answer. Try again later."""


class OAuthGrantError(OAuthError):
    """The server refused the code or the refresh token (``invalid_grant``). Connect again."""


@dataclass(frozen=True, slots=True)
class AuthServer:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    resource: str
    """The ``resource`` parameter: the URL of the MCP server (RFC 8707)."""
    scope: str | None
    """The scope to ask for, or ``None`` for the default scope of the server."""

    def config(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "registration_endpoint": self.registration_endpoint,
            "resource": self.resource,
            "scope": self.scope,
        }

    @classmethod
    def from_config(cls, data: object) -> AuthServer | None:
        if not isinstance(data, Mapping):
            return None
        try:
            return cls(
                issuer=str(data["issuer"]),
                authorization_endpoint=str(data["authorization_endpoint"]),
                token_endpoint=str(data["token_endpoint"]),
                registration_endpoint=_opt(data.get("registration_endpoint")),
                resource=str(data["resource"]),
                scope=_opt(data.get("scope")),
            )
        except KeyError:
            return None


@dataclass(frozen=True, slots=True)
class OAuthClient:
    client_id: str
    client_secret: str | None = None
    auth_method: str = "none"


@dataclass(frozen=True, slots=True)
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: float | None
    """Seconds since the epoch, or ``None`` if the server did not say."""

    def fresh(self, now: float | None = None) -> bool:
        if self.expires_at is None:
            return True
        return (now if now is not None else time.time()) < self.expires_at - REFRESH_MARGIN_S


@dataclass(frozen=True, slots=True)
class Pkce:
    verifier: str
    challenge: str

    @classmethod
    def new(cls) -> Pkce:
        verifier = secrets.token_urlsafe(48)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return cls(verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii"))


def authorization_url(
    server: AuthServer, client: OAuthClient, *, redirect_uri: str, state: str, pkce: Pkce
) -> str:
    query = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
        "resource": server.resource,
    }
    if server.scope:
        query["scope"] = server.scope
    separator = "&" if urlsplit(server.authorization_endpoint).query else "?"
    return f"{server.authorization_endpoint}{separator}{urlencode(query)}"


class OAuthFlow:
    """The HTTP steps of the flow."""

    def __init__(self, http: OutboundClient | None = None) -> None:
        self._http = http or OutboundClient(POLICY)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def discover(self, mcp_url: str, *, scope: str | None = None) -> AuthServer:
        """The authorization server of the MCP server, and the resource and scope to ask
        for. ``scope`` wins over the scopes that the server names."""
        challenge = await self._challenge(mcp_url)
        metadata_url = None
        hinted_scope = None
        if challenge:
            found = _RESOURCE_METADATA.search(challenge)
            metadata_url = found.group(1) if found else None
            hinted = _SCOPE.search(challenge)
            hinted_scope = hinted.group(1) if hinted else None
        parts = urlsplit(mcp_url)
        origin = _origin(mcp_url)
        path = parts.path.rstrip("/")
        candidates = [metadata_url] if metadata_url else []
        if path:
            candidates.append(f"{origin}/.well-known/oauth-protected-resource{path}")
        candidates.append(f"{origin}/.well-known/oauth-protected-resource")
        resource = mcp_url
        issuer = origin
        named_scopes: list[str] = []
        for candidate in candidates:
            document = await self._json(candidate)
            if document is None:
                continue
            servers = document.get("authorization_servers")
            if not isinstance(servers, list) or not servers or not isinstance(servers[0], str):
                raise OAuthError("the server names no authorization server")
            issuer = servers[0].rstrip("/")
            named = document.get("resource")
            if isinstance(named, str) and named:
                if _origin(named) != origin:
                    raise OAuthError("the server names a resource on another host")
                resource = named
            supported = document.get("scopes_supported")
            if isinstance(supported, list):
                named_scopes = [s for s in supported if isinstance(s, str)]
            break
        server = await self._server_metadata(issuer)
        chosen = scope or hinted_scope or (" ".join(named_scopes) if named_scopes else None)
        return AuthServer(
            issuer=issuer,
            authorization_endpoint=server["authorization_endpoint"],
            token_endpoint=server["token_endpoint"],
            registration_endpoint=server.get("registration_endpoint"),
            resource=resource,
            scope=chosen,
        )

    async def register(self, server: AuthServer, *, redirect_uri: str) -> OAuthClient:
        """Dynamic client registration (RFC 7591)."""
        if not server.registration_endpoint:
            raise OAuthError(
                "the authorization server has no dynamic client registration, so sama cannot "
                "connect it"
            )
        body = {
            "client_name": "sama",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        answer = await self._send("POST", server.registration_endpoint, json=body)
        if answer.status_code not in (200, 201):
            raise OAuthError(f"the client registration failed ({answer.status_code})")
        data = _object(answer)
        client_id = data.get("client_id") if data else None
        if not isinstance(client_id, str) or not client_id:
            raise OAuthError("the client registration gave no client id")
        secret = data.get("client_secret") if data else None
        method = data.get("token_endpoint_auth_method") if data else None
        if not isinstance(secret, str) or not secret:
            return OAuthClient(client_id)
        if method not in ("client_secret_basic", "client_secret_post"):
            method = "client_secret_post"
        return OAuthClient(client_id, secret, str(method))

    async def exchange(
        self,
        server: AuthServer,
        client: OAuthClient,
        *,
        code: str,
        verifier: str,
        redirect_uri: str,
    ) -> Tokens:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "resource": server.resource,
        }
        return await self._token(server, client, form, old_refresh=None)

    async def refresh(self, server: AuthServer, client: OAuthClient, refresh_token: str) -> Tokens:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "resource": server.resource,
        }
        return await self._token(server, client, form, old_refresh=refresh_token)

    async def _token(
        self,
        server: AuthServer,
        client: OAuthClient,
        form: dict[str, str],
        *,
        old_refresh: str | None,
    ) -> Tokens:
        headers: dict[str, str] = {"accept": "application/json"}
        form["client_id"] = client.client_id
        if client.client_secret:
            if client.auth_method == "client_secret_basic":
                pair = f"{client.client_id}:{client.client_secret}".encode()
                headers["authorization"] = "Basic " + base64.b64encode(pair).decode("ascii")
            else:
                form["client_secret"] = client.client_secret
        answer = await self._send("POST", server.token_endpoint, headers=headers, form=form)
        data = _object(answer) or {}
        if answer.status_code >= 500 or answer.status_code == 429:
            raise OAuthUnavailableError(f"the token endpoint answered {answer.status_code}")
        if answer.status_code != 200:
            error = data.get("error")
            if error in ("invalid_grant", "invalid_client", "unauthorized_client"):
                raise OAuthGrantError(f"the server refused the grant ({error}). Connect again")
            raise OAuthError(
                short_error(f"the token request failed: {error or answer.status_code}")
            )
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise OAuthError("the token answer has no access token")
        token_type = str(data.get("token_type") or "bearer").lower()
        if token_type != "bearer":  # noqa: S105 - a token type, not a password
            raise OAuthError(f"the server gave a {token_type} token, not a bearer token")
        refresh = data.get("refresh_token")
        expires = data.get("expires_in")
        return Tokens(
            access_token=access,
            refresh_token=refresh if isinstance(refresh, str) and refresh else old_refresh,
            expires_at=(
                time.time() + float(expires)
                if isinstance(expires, (int, float)) and expires > 0
                else None
            ),
        )

    async def _challenge(self, mcp_url: str) -> str | None:
        """The ``WWW-Authenticate`` header of an ``initialize`` without a token."""
        body = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": INITIALIZE_PARAMS,
        }
        headers = {"accept": "application/json, text/event-stream"}
        answer = await self._send(
            "POST", mcp_url, headers=headers, json=body, enough=lambda _data: True
        )
        if answer.status_code in (401, 403):
            return answer.headers.get("www-authenticate") or ""
        return None

    async def _server_metadata(self, issuer: str) -> dict[str, str]:
        parts = urlsplit(issuer)
        origin = _origin(issuer)
        path = parts.path.rstrip("/")
        if path:
            candidates = [
                f"{origin}/.well-known/oauth-authorization-server{path}",
                f"{origin}/.well-known/openid-configuration{path}",
                f"{origin}{path}/.well-known/openid-configuration",
            ]
        else:
            candidates = [
                f"{origin}/.well-known/oauth-authorization-server",
                f"{origin}/.well-known/openid-configuration",
            ]
        for candidate in candidates:
            document = await self._json(candidate)
            if document is None:
                continue
            methods = document.get("code_challenge_methods_supported")
            if isinstance(methods, list) and "S256" not in methods:
                raise OAuthError("the authorization server does not support PKCE with S256")
            found: dict[str, str] = {}
            for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
                value = document.get(key)
                if isinstance(value, str) and value:
                    if urlsplit(value).scheme != "https":
                        raise OAuthError(f"the {key} of the authorization server is not https")
                    found[key] = value
            if "authorization_endpoint" not in found or "token_endpoint" not in found:
                raise OAuthError("the authorization server metadata is not complete")
            return found
        # A server of MCP version 2025-03-26 can have no metadata: the default paths.
        return {
            "authorization_endpoint": f"{origin}/authorize",
            "token_endpoint": f"{origin}/token",
            "registration_endpoint": f"{origin}/register",
        }

    async def _json(self, url: str) -> dict[str, Any] | None:
        """A JSON document, or ``None`` for a ``404`` (or another client error)."""
        answer = await self._send("GET", url, headers={"accept": "application/json"})
        if answer.status_code >= 500 or answer.status_code == 429:
            raise OAuthUnavailableError(f"{_origin(url)} answered {answer.status_code}")
        if answer.status_code != 200:
            return None
        return _object(answer)

    async def _send(self, method: str, url: str, **kwargs: Any) -> FetchResult:
        try:
            return await self._http.request(method, url, **kwargs)
        except BlockedUrlError as error:
            raise OAuthError(f"the URL policy blocked {_origin(url)}: {error}") from None
        except FetchError as error:
            raise OAuthUnavailableError(str(error)) from None


def new_state(connection_id: str) -> str:
    """``<connection id>.<random>``: the API finds the connection, then compares the rest."""
    return f"{connection_id}.{secrets.token_urlsafe(24)}"


def _object(answer: FetchResult) -> dict[str, Any] | None:
    try:
        data = json.loads(answer.content)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _opt(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = [
    "AuthServer",
    "OAuthClient",
    "OAuthError",
    "OAuthFlow",
    "OAuthGrantError",
    "OAuthUnavailableError",
    "Pkce",
    "Tokens",
    "authorization_url",
    "new_state",
]
