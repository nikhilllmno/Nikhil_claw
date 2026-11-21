"""The pure rules of sign-up: emails, slugs, plans, and the policy (Section 17.2)."""

from __future__ import annotations

import pytest

from sama.orgs import SignupPolicy, plan_of
from sama.orgs.emails import DISPOSABLE_DOMAINS, blocked_domain, normalize_email
from sama.orgs.plans import FREE, SIGNUP_PLAN, TEAM
from sama.orgs.service import hash_token, slug_from_name


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" Ana@Example.TEST ", "ana@example.test"),
        ("a.b+tag@mail.example.co", "a.b+tag@mail.example.co"),
        ("no-at.example.test", None),
        ("two@@example.test", None),
        ("ana@localhost", None),
        ("ana @example.test", None),
        ("a" * 65 + "@example.test", None),
        ("", None),
    ],
)
def test_an_email_is_normalized_or_refused(value: str, expected: str | None) -> None:
    assert normalize_email(value) == expected


def test_a_blocked_domain_covers_its_subdomains() -> None:
    blocked = {"spam.test"}
    assert blocked_domain("x@spam.test", blocked)
    assert blocked_domain("x@mx.spam.test", blocked)
    assert not blocked_domain("x@notspam.test", blocked)
    assert not blocked_domain("x@test", {"test"})
    assert "mailinator.com" in DISPOSABLE_DOMAINS


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Acme Labs", "acme-labs"),
        ("  Ünïcode Café!! ", "n-code-caf"),
        ("ab", "org"),
        ("Admin", "org"),
        ("x" * 60, "x" * 40),
    ],
)
def test_a_slug_comes_from_the_name(name: str, slug: str) -> None:
    assert slug_from_name(name) == slug


def test_a_token_hash_has_32_bytes() -> None:
    assert len(hash_token("abc")) == 32
    assert hash_token("abc") != hash_token("abd")


def test_a_new_org_gets_the_free_plan_and_old_orgs_keep_team() -> None:
    assert SIGNUP_PLAN is FREE
    assert plan_of("team") is TEAM
    assert plan_of("free") is FREE
    # An unknown plan gets the smallest caps, not none.
    assert plan_of("gold") is FREE
    assert TEAM.max_members is None
    assert FREE.max_members is not None


def test_the_policy_reads_the_environment() -> None:
    assert SignupPolicy.from_env({}).mode == "invite_only"
    assert SignupPolicy.from_env({"SAMA_SIGNUP": "OPEN"}).mode == "open"
    with pytest.raises(ValueError, match="SAMA_SIGNUP"):
        SignupPolicy.from_env({"SAMA_SIGNUP": "yes"})
