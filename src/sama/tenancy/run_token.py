"""Run tokens (docs/architecture.md, Sections 4.2 and 16.2).

A run token is the only credential in the sandbox. The orchestrator issues one token per run
invocation. The token binds ``org_id``, ``workspace_id``, ``session_id``, ``run_id``, and the
user of a private session, and it expires after the run. The model proxy (M1.7) and the sama
MCP server (M1.8) accept only a valid run token, and they take the tenant scope from the token,
never from the request.

The token is a JWT signed with HS256. Only the control plane holds the signing key.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from typing import Any, Final

import jwt
from pydantic import BaseModel, ConfigDict, Field

from sama.runtime.contract import Audience, RunScope

ISSUER: Final = "sama"
AUDIENCE: Final = "sama-run"
ALGORITHM: Final = "HS256"
MIN_KEY_BYTES: Final = 32
RUN_TOKEN_MAX_TTL_S: Final = 2 * 60 * 60
"""A run has at most 1 hour of wall time (Section 16.2). The rest is margin for the start."""
_LEEWAY_S: Final = 5


class InvalidRunTokenError(Exception):
    """The token is not a valid run token. The message never contains the token."""


class RunTokenClaims(BaseModel):
    """The verified content of a run token."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    user_id: str | None = None
    token_id: str = Field(min_length=1)
    expires_at: int

    def matches(self, scope: RunScope) -> bool:
        """True if the token is for exactly this run scope."""
        return (
            self.org_id == scope.org_id
            and self.workspace_id == scope.workspace_id
            and self.session_id == scope.session_id
            and self.run_id == scope.run_id
            and self.user_id == scope.user_id
        )


class RunTokenSigner:
    """Issues and verifies run tokens with one secret key."""

    def __init__(self, key: bytes, *, clock: Callable[[], float] = time.time) -> None:
        if len(key) < MIN_KEY_BYTES:
            raise ValueError(f"the run token key must have at least {MIN_KEY_BYTES} bytes")
        self._key = key
        self._clock = clock

    def __repr__(self) -> str:
        return "RunTokenSigner(key=<hidden>)"

    def issue(self, scope: RunScope, *, ttl_s: float) -> str:
        if not 0 < ttl_s <= RUN_TOKEN_MAX_TTL_S:
            raise ValueError(f"ttl_s must be more than 0 and at most {RUN_TOKEN_MAX_TTL_S}")
        now = int(self._clock())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": scope.run_id,
            "iat": now,
            "exp": now + int(ttl_s),
            "jti": secrets.token_urlsafe(12),
            "org": scope.org_id,
            "wsp": scope.workspace_id,
            "ses": scope.session_id,
        }
        if scope.audience is Audience.PRIVATE:
            claims["usr"] = scope.user_id
        return jwt.encode(claims, self._key, algorithm=ALGORITHM)

    def verify(self, token: str) -> RunTokenClaims:
        try:
            raw = jwt.decode(
                token,
                self._key,
                algorithms=[ALGORITHM],
                audience=AUDIENCE,
                issuer=ISSUER,
                # The signer checks the time with its own clock, so tests can move the clock.
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub", "jti"],
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
            expires_at, issued_at = int(raw["exp"]), int(raw["iat"])
            if expires_at - issued_at > RUN_TOKEN_MAX_TTL_S:
                raise InvalidRunTokenError("the run token lives too long")
            now = self._clock()
            if now > expires_at + _LEEWAY_S:
                raise InvalidRunTokenError("the run token expired")
            if now < issued_at - _LEEWAY_S:
                raise InvalidRunTokenError("the run token is not valid yet")
            return RunTokenClaims(
                org_id=raw["org"],
                workspace_id=raw["wsp"],
                session_id=raw["ses"],
                run_id=raw["sub"],
                user_id=raw.get("usr"),
                token_id=raw["jti"],
                expires_at=expires_at,
            )
        except (jwt.PyJWTError, KeyError, TypeError, ValueError):
            raise InvalidRunTokenError("the run token is not valid") from None
