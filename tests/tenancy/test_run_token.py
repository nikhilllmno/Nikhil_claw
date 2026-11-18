"""Tests of the run token (docs/architecture.md, Sections 4.2 and 16.2)."""

from __future__ import annotations

import jwt
import pytest

from sama.tenancy import RUN_TOKEN_MAX_TTL_S, InvalidRunTokenError, RunTokenSigner
from tests.support import make_scope

KEY = b"k" * 32
OTHER_KEY = b"o" * 32


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_issue_and_verify_round_trip_a_private_scope() -> None:
    signer = RunTokenSigner(KEY)
    scope = make_scope("run-7")
    claims = signer.verify(signer.issue(scope, ttl_s=600))
    assert claims.matches(scope)
    assert claims.user_id == "user-1"
    assert claims.run_id == "run-7"
    assert claims.token_id


def test_a_shared_scope_has_no_user() -> None:
    signer = RunTokenSigner(KEY)
    scope = make_scope(shared=True)
    claims = signer.verify(signer.issue(scope, ttl_s=60))
    assert claims.user_id is None
    assert claims.matches(scope)


def test_matches_rejects_another_run() -> None:
    signer = RunTokenSigner(KEY)
    claims = signer.verify(signer.issue(make_scope("run-1"), ttl_s=60))
    assert not claims.matches(make_scope("run-2"))
    assert not claims.matches(make_scope("run-1", shared=True))


def test_each_token_has_a_new_id() -> None:
    signer = RunTokenSigner(KEY)
    scope = make_scope()
    first = signer.verify(signer.issue(scope, ttl_s=60))
    second = signer.verify(signer.issue(scope, ttl_s=60))
    assert first.token_id != second.token_id


def test_a_short_key_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        RunTokenSigner(b"short")


@pytest.mark.parametrize("ttl_s", [0, -1, RUN_TOKEN_MAX_TTL_S + 1])
def test_ttl_must_be_in_range(ttl_s: float) -> None:
    with pytest.raises(ValueError, match="ttl_s"):
        RunTokenSigner(KEY).issue(make_scope(), ttl_s=ttl_s)


def test_an_expired_token_is_refused_after_the_leeway() -> None:
    clock = Clock()
    signer = RunTokenSigner(KEY, clock=clock)
    token = signer.issue(make_scope(), ttl_s=60)
    clock.now += 64
    signer.verify(token)  # within the leeway
    clock.now += 2
    with pytest.raises(InvalidRunTokenError, match="expired"):
        signer.verify(token)


def test_a_token_from_the_future_is_refused() -> None:
    clock = Clock()
    token = RunTokenSigner(KEY, clock=clock).issue(make_scope(), ttl_s=60)
    clock.now -= 60
    with pytest.raises(InvalidRunTokenError, match="not valid yet"):
        RunTokenSigner(KEY, clock=clock).verify(token)


def test_another_key_is_refused() -> None:
    token = RunTokenSigner(OTHER_KEY).issue(make_scope(), ttl_s=60)
    with pytest.raises(InvalidRunTokenError, match="not valid"):
        RunTokenSigner(KEY).verify(token)


def _forge(claims: dict[str, object], *, algorithm: str = "HS256") -> str:
    return jwt.encode(claims, KEY, algorithm=algorithm)


def _claims(now: int, **changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "iss": "sama",
        "aud": "sama-run",
        "sub": "run-1",
        "iat": now,
        "exp": now + 60,
        "jti": "j1",
        "org": "org-1",
        "wsp": "ws-1",
        "ses": "sess-1",
    }
    base.update(changes)
    return {k: v for k, v in base.items() if v is not None}


@pytest.mark.parametrize(
    "changes",
    [
        {"aud": "another-audience"},
        {"iss": "someone-else"},
        {"org": None},
        {"jti": None},
        {"sub": ""},
    ],
    ids=["audience", "issuer", "no-org", "no-jti", "empty-run"],
)
def test_a_bad_claim_is_refused(changes: dict[str, object]) -> None:
    clock = Clock()
    token = _forge(_claims(int(clock.now), **changes))
    with pytest.raises(InvalidRunTokenError):
        RunTokenSigner(KEY, clock=clock).verify(token)


def test_a_token_that_lives_too_long_is_refused() -> None:
    clock = Clock()
    now = int(clock.now)
    token = _forge(_claims(now, exp=now + RUN_TOKEN_MAX_TTL_S + 1))
    with pytest.raises(InvalidRunTokenError, match="too long"):
        RunTokenSigner(KEY, clock=clock).verify(token)


def test_the_none_algorithm_is_refused() -> None:
    clock = Clock()
    token = jwt.encode(_claims(int(clock.now)), key="", algorithm="none")
    with pytest.raises(InvalidRunTokenError):
        RunTokenSigner(KEY, clock=clock).verify(token)


def test_errors_and_repr_do_not_show_the_token_or_the_key() -> None:
    signer = RunTokenSigner(KEY)
    token = RunTokenSigner(OTHER_KEY).issue(make_scope(), ttl_s=60)
    with pytest.raises(InvalidRunTokenError) as info:
        signer.verify(token)
    assert token not in str(info.value)
    assert info.value.__cause__ is None
    assert "k" * 32 not in repr(signer)


def test_garbage_is_refused() -> None:
    with pytest.raises(InvalidRunTokenError):
        RunTokenSigner(KEY).verify("not-a-jwt")
