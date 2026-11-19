"""Supabase Auth access tokens (docs/architecture.md, Section 11.4).

The API accepts the access token that Supabase Auth gives to a signed-in user. A project signs
its tokens with a shared secret (``HS256``, the legacy key) or with an asymmetric key
(``ES256`` or ``RS256``) whose public part is in the JWKS of the project. The verifier accepts
each algorithm only with its own key source, so a token cannot pick an HMAC check with a
public key.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx
import jwt
from pydantic import SecretStr

log = logging.getLogger("sama.api")

AUDIENCE: Final = "authenticated"
ASYMMETRIC_ALGORITHMS: Final = frozenset({"ES256", "RS256"})
LEEWAY_S: Final = 10.0
JWKS_TTL_S: Final = 600.0
JWKS_REFRESH_GAP_S: Final = 30.0
"""An unknown key id fetches the JWKS again, but at most once in this time."""


class AuthError(Exception):
    """The request has no valid access token. The message is safe to show."""


@dataclass(frozen=True, slots=True)
class Principal:
    """The Supabase Auth user of a request."""

    auth_user_id: uuid.UUID
    email: str | None


def jwks_url(supabase_url: str) -> str:
    return f"{supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"


def issuer(supabase_url: str) -> str:
    return f"{supabase_url.rstrip('/')}/auth/v1"


class JwtVerifier:
    """Checks a Supabase Auth access token and returns its user."""

    def __init__(
        self,
        *,
        secret: SecretStr | None = None,
        jwks_url: str | None = None,
        issuer: str | None = None,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if secret is None and jwks_url is None:
            raise ValueError("give the JWT secret, the JWKS URL, or both")
        self._secret = secret
        self._jwks_url = jwks_url
        self._issuer = issuer
        self._http = http
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()

    async def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as error:
            raise AuthError("the access token is not valid") from error
        algorithm = header.get("alg")
        key: Any
        if algorithm == "HS256" and self._secret is not None:
            key = self._secret.get_secret_value()
        elif algorithm in ASYMMETRIC_ALGORITHMS and self._jwks_url is not None:
            key = await self._public_key(header.get("kid"), str(algorithm))
        else:
            raise AuthError("the access token uses an algorithm that sama does not accept")
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[str(algorithm)],
                audience=AUDIENCE,
                issuer=self._issuer,
                leeway=LEEWAY_S,
                options={"require": ["exp", "sub", "aud"]},
            )
        except jwt.PyJWTError as error:
            raise AuthError("the access token is not valid") from error
        return _principal(claims)

    async def _public_key(self, kid: Any, algorithm: str) -> Any:
        if not isinstance(kid, str) or not kid:
            raise AuthError("the access token has no key id")
        async with self._lock:
            now = self._clock()
            stale = self._fetched_at is None or now - self._fetched_at > JWKS_TTL_S
            unknown = kid not in self._keys and (
                self._fetched_at is None or now - self._fetched_at > JWKS_REFRESH_GAP_S
            )
            if (stale or unknown) and self._jwks_url is not None:
                await self._fetch(self._jwks_url, now)
            found = self._keys.get(kid)
        if found is None or found.algorithm_name != algorithm:
            raise AuthError("the access token is signed with an unknown key")
        return found.key

    async def _fetch(self, url: str, now: float) -> None:
        try:
            if self._http is None:
                async with httpx.AsyncClient(timeout=10) as http:
                    response = await http.get(url)
            else:
                response = await self._http.get(url)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            # Keep the old keys: a short outage of Supabase must not sign out every user.
            log.warning("cannot read the JWKS: %s", type(error).__name__)
            if self._fetched_at is None:
                raise AuthError("sama cannot check the access token now") from error
            # Try again after the refresh gap, not at each request.
            self._fetched_at = now - JWKS_TTL_S + JWKS_REFRESH_GAP_S
            return
        keys: dict[str, jwt.PyJWK] = {}
        for entry in data.get("keys", []) if isinstance(data, Mapping) else []:
            try:
                parsed = jwt.PyJWK(entry)
            except jwt.PyJWTError:
                continue  # For example a key type that this verifier does not use.
            if isinstance(entry.get("kid"), str) and parsed.algorithm_name in ASYMMETRIC_ALGORITHMS:
                keys[entry["kid"]] = parsed
        self._keys = keys
        self._fetched_at = now


def _principal(claims: Mapping[str, Any]) -> Principal:
    if claims.get("role") != AUDIENCE:
        raise AuthError("the access token is not for a signed-in user")
    if claims.get("is_anonymous") is True:
        raise AuthError("sama does not accept anonymous users")
    try:
        user = uuid.UUID(str(claims["sub"]))
    except ValueError as error:
        raise AuthError("the access token is not valid") from error
    email = claims.get("email")
    return Principal(user, email if isinstance(email, str) else None)
