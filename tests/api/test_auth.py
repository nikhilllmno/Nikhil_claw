"""Tests of the Supabase Auth token check (docs/architecture.md, Section 11.4)."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from pydantic import SecretStr

from sama.api.auth import AuthError, JwtVerifier, issuer, jwks_url
from tests.api.conftest import ISSUER, SECRET, token

USER = uuid.uuid4()
JWKS = "https://project.example.test/auth/v1/.well-known/jwks.json"


def test_the_urls_come_from_the_project_url() -> None:
    assert issuer("https://abc.supabase.co/") == "https://abc.supabase.co/auth/v1"
    assert jwks_url("https://abc.supabase.co") == JWKS.replace(
        "project.example.test", "abc.supabase.co"
    )


def test_the_verifier_needs_a_key_source() -> None:
    with pytest.raises(ValueError, match="JWT secret"):
        JwtVerifier()


async def test_a_valid_hs256_token_gives_the_user() -> None:
    verifier = JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER)
    principal = await verifier.verify(token(USER))
    assert (principal.auth_user_id, principal.email) == (USER, "mia@example.test")


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: token(USER, expires_in=-60), "not valid"),
        (lambda: token(USER, aud="other"), "not valid"),
        (lambda: token(USER, iss="https://evil.example.test/auth/v1"), "not valid"),
        (lambda: token(USER, secret="another-secret-with-32-bytes-or-more"), "not valid"),
        (lambda: token(USER, exp=None), "not valid"),
        (lambda: token(USER, role="anon"), "signed-in user"),
        (lambda: token(USER, role="service_role"), "signed-in user"),
        (lambda: token(USER, is_anonymous=True), "anonymous"),
        (lambda: token("not-a-uuid"), "not valid"),
        (lambda: "not.a.token", "not valid"),
        (
            lambda: jwt.encode({"sub": str(USER)}, key="", algorithm="none"),
            "algorithm",
        ),
    ],
)
async def test_a_bad_token_is_refused(make: Any, message: str) -> None:
    verifier = JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER)
    with pytest.raises(AuthError, match=message):
        await verifier.verify(make())


async def test_hs256_needs_the_secret() -> None:
    """With only a JWKS, an HMAC token cannot use a public key as the secret."""
    verifier = JwtVerifier(jwks_url=JWKS, issuer=ISSUER)
    with pytest.raises(AuthError, match="algorithm"):
        await verifier.verify(token(USER))


class Jwks:
    """A fake JWKS endpoint. ``keys`` maps a key id to a private key."""

    def __init__(self) -> None:
        self.keys: dict[str, Any] = {}
        self.calls = 0
        self.down = False

    def add(self, kid: str, kind: str = "ES256") -> None:
        self.keys[kid] = (
            ec.generate_private_key(ec.SECP256R1())
            if kind == "ES256"
            else rsa.generate_private_key(public_exponent=65537, key_size=2048)
        )

    def sign(self, kid: str, **claims: Any) -> str:
        key = self.keys[kid]
        algorithm = "ES256" if isinstance(key, ec.EllipticCurvePrivateKey) else "RS256"
        body = {
            "sub": str(USER),
            "aud": "authenticated",
            "role": "authenticated",
            "iss": ISSUER,
            "exp": int(time.time() + 300),
            **claims,
        }
        return jwt.encode(body, key, algorithm=algorithm, headers={"kid": kid})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        assert str(request.url) == JWKS
        if self.down:
            return httpx.Response(503)
        keys = []
        for kid, key in self.keys.items():
            algorithm = "ES256" if isinstance(key, ec.EllipticCurvePrivateKey) else "RS256"
            entry = json.loads(
                jwt.algorithms.get_default_algorithms()[algorithm].to_jwk(key.public_key())
            )
            keys.append({**entry, "kid": kid, "alg": algorithm, "use": "sig"})
        keys.append({"kty": "oct", "kid": "hmac", "k": "c2VjcmV0"})  # never used for a check
        return httpx.Response(200, json={"keys": keys})


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def jwks_verifier(jwks: Jwks, clock: Clock) -> JwtVerifier:
    http = httpx.AsyncClient(transport=httpx.MockTransport(jwks.handler))
    return JwtVerifier(jwks_url=JWKS, issuer=ISSUER, http=http, clock=clock)


@pytest.mark.parametrize("kind", ["ES256", "RS256"])
async def test_an_asymmetric_token_uses_the_jwks(kind: str) -> None:
    jwks, clock = Jwks(), Clock()
    jwks.add("k1", kind)
    verifier = jwks_verifier(jwks, clock)
    assert (await verifier.verify(jwks.sign("k1"))).auth_user_id == USER
    assert (await verifier.verify(jwks.sign("k1"))).auth_user_id == USER
    assert jwks.calls == 1, "the keys are cached"


async def test_a_rotated_key_is_fetched_but_not_at_each_request() -> None:
    jwks, clock = Jwks(), Clock()
    jwks.add("k1")
    verifier = jwks_verifier(jwks, clock)
    await verifier.verify(jwks.sign("k1"))
    jwks.add("k2")
    with pytest.raises(AuthError, match="unknown key"):
        await verifier.verify(jwks.sign("k2"))  # inside the refresh gap
    assert jwks.calls == 1
    clock.now += 31
    assert (await verifier.verify(jwks.sign("k2"))).auth_user_id == USER
    assert jwks.calls == 2
    with pytest.raises(AuthError, match="unknown key"):
        await verifier.verify(_forged_kid(jwks))
    assert jwks.calls == 2


def _forged_kid(jwks: Jwks) -> str:
    """A token signed by k1 but labelled with a key id that the JWKS does not have."""
    body = {"sub": str(USER), "aud": "authenticated", "role": "authenticated", "iss": ISSUER}
    return jwt.encode(
        {**body, "exp": int(time.time() + 300)},
        jwks.keys["k1"],
        algorithm="ES256",
        headers={"kid": "k9"},
    )


async def test_a_key_with_another_algorithm_is_refused() -> None:
    jwks, clock = Jwks(), Clock()
    jwks.add("k1", "RS256")
    verifier = jwks_verifier(jwks, clock)
    other = Jwks()
    other.add("k1", "ES256")
    with pytest.raises(AuthError, match="unknown key"):
        await verifier.verify(other.sign("k1"))


async def test_a_jwks_outage_keeps_the_old_keys() -> None:
    jwks, clock = Jwks(), Clock()
    jwks.add("k1")
    verifier = jwks_verifier(jwks, clock)
    await verifier.verify(jwks.sign("k1"))
    jwks.down = True
    clock.now += 601
    assert (await verifier.verify(jwks.sign("k1"))).auth_user_id == USER
    assert jwks.calls == 2
    assert (await verifier.verify(jwks.sign("k1"))).auth_user_id == USER
    assert jwks.calls == 2, "no new fetch inside the refresh gap"


async def test_no_jwks_at_the_start_refuses_the_token() -> None:
    jwks, clock = Jwks(), Clock()
    jwks.add("k1")
    jwks.down = True
    verifier = jwks_verifier(jwks, clock)
    with pytest.raises(AuthError, match="cannot check"):
        await verifier.verify(jwks.sign("k1"))


async def test_a_failed_first_fetch_waits_for_the_refresh_gap() -> None:
    jwks, clock = Jwks(), Clock()
    jwks.add("k1")
    jwks.down = True
    verifier = jwks_verifier(jwks, clock)
    for _ in range(3):
        with pytest.raises(AuthError, match="cannot check"):
            await verifier.verify(jwks.sign("k1"))
    assert jwks.calls == 1, "a failed fetch also starts the refresh gap"
    jwks.down = False
    clock.now += 31
    assert (await verifier.verify(jwks.sign("k1"))).auth_user_id == USER
    assert jwks.calls == 2


async def test_forged_key_ids_in_an_outage_fetch_once_per_gap() -> None:
    jwks, clock = Jwks(), Clock()
    jwks.add("k1")
    verifier = jwks_verifier(jwks, clock)
    await verifier.verify(jwks.sign("k1"))
    jwks.down = True
    clock.now += 31
    for _ in range(5):
        with pytest.raises(AuthError, match="unknown key"):
            await verifier.verify(_forged_kid(jwks))
    assert jwks.calls == 2


async def test_a_known_key_does_not_wait_for_a_slow_fetch() -> None:
    jwks, clock = Jwks(), Clock()
    jwks.add("k1")
    release = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        if jwks.calls >= 1:
            jwks.calls += 1
            await release.wait()
            return httpx.Response(503)
        return jwks.handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    verifier = JwtVerifier(jwks_url=JWKS, issuer=ISSUER, http=http, clock=clock)
    await verifier.verify(jwks.sign("k1"))
    clock.now += 601  # the keys are stale, so the next request fetches them again
    fetching = asyncio.create_task(verifier.verify(_forged_kid(jwks)))
    await asyncio.sleep(0.01)
    assert jwks.calls == 2, "the forged token holds the fetch"
    principal = await asyncio.wait_for(verifier.verify(jwks.sign("k1")), timeout=1)
    assert principal.auth_user_id == USER
    release.set()
    with pytest.raises(AuthError, match="unknown key"):
        await fetching


async def test_the_principal_has_the_expiry() -> None:
    verifier = JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER)
    before = time.time()
    principal = await verifier.verify(token(USER, expires_in=120))
    assert principal.expires_at is not None
    assert before + 110 < principal.expires_at < before + 130


async def test_a_token_without_a_key_id_is_refused() -> None:
    jwks, clock = Jwks(), Clock()
    jwks.add("k1")
    verifier = jwks_verifier(jwks, clock)
    unlabelled = jwt.encode(
        {"sub": str(USER), "aud": "authenticated", "exp": int(time.time() + 60)},
        jwks.keys["k1"],
        algorithm="ES256",
    )
    with pytest.raises(AuthError, match="key id"):
        await verifier.verify(unlabelled)
