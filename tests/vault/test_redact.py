"""Tests of the redactor (docs/architecture.md, Section 16.3)."""

from __future__ import annotations

import time

import pytest

from sama.vault.redact import REDACTED, Redactor, env_secrets

# Test tokens, built so that a secret scanner does not see a real key in this file.
GITHUB = "ghp_" + "a1B2" * 9
OPENAI = "sk-" + "proj-" + "x9" * 12
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
SLACK = "xoxb-" + "1234567890-abcdefghij"
TELEGRAM = "123456789:AA" + "b" * 33
GOOGLE = "AIza" + "c" * 35
STRIPE = "sk_" + "live_" + "d" * 24
JWT = "eyJ" + "h" * 12 + ".eyJ" + "p" * 12 + "." + "s" * 12
PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"


@pytest.mark.parametrize(
    "secret", [GITHUB, OPENAI, AWS, SLACK, TELEGRAM, GOOGLE, STRIPE, JWT, PRIVATE_KEY]
)
def test_a_known_form_is_redacted(secret: str) -> None:
    out = Redactor().text(f"the key is {secret} now")
    assert secret not in out
    assert out == f"the key is {REDACTED} now"


def test_a_bearer_token_and_a_url_password_keep_their_prefix() -> None:
    redactor = Redactor()
    token = "abcdefghijklmnopqrstuvwxyz0123"
    assert redactor.text(f"Authorization: Bearer {token}") == f"Authorization: Bearer {REDACTED}"
    url = "postgresql://sama:pa55w0rd-x@db.example.test:5432/sama"
    assert redactor.text(f"url={url}") == (
        f"url=postgresql://sama:{REDACTED}@db.example.test:5432/sama"
    )


def test_a_known_value_is_redacted_everywhere() -> None:
    redactor = Redactor(["plain-secret-value", None, "short"])
    out = redactor.text("a plain-secret-value and plain-secret-value, short stays")
    assert out == f"a {REDACTED} and {REDACTED}, short stays"


def test_the_longest_known_value_wins() -> None:
    redactor = Redactor(["value-one", "value-one-and-more"])
    assert redactor.text("x value-one-and-more y") == f"x {REDACTED} y"


@pytest.mark.parametrize(
    "unit",
    [
        "-----BEGIN PRIVATE KEY-----\n",
        "postgresql://u:",
        "eyJabcdefghijklmn-",
        "Bearer ",
        "sk-",
    ],
)
def test_hostile_text_takes_linear_time(unit: str) -> None:
    redactor = Redactor(["known-value-1"])
    small, large = unit * 2_000, unit * 20_000
    elapsed = []
    for text in (small, large):
        started = time.perf_counter()
        redactor.text(text)
        elapsed.append(time.perf_counter() - started)
    # Quadratic work takes about 100 times longer for 10 times the text.
    assert elapsed[1] < 1.0
    assert elapsed[1] < 30 * elapsed[0] + 0.05


def test_known_values_only_grow() -> None:
    redactor = Redactor(["fixed-value-1"])
    redactor.add("vault-value-1")
    redactor.add("vault-value-2", None, "short")
    assert redactor.text("fixed-value-1 vault-value-1 vault-value-2").count(REDACTED) == 3
    assert redactor.text("short") == "short"
    redactor.forget("fixed-value-1")
    assert redactor.text("fixed-value-1") == "fixed-value-1"


def test_json_values_are_redacted_at_every_depth() -> None:
    redactor = Redactor(["known-secret-1"])
    value = {
        "command": "curl -H 'x: known-secret-1'",
        "nested": [{"token": GITHUB}, 3, None, True],
        "known-secret-1": "key",
        "tuple": ("known-secret-1",),
    }
    assert redactor.json(value) == {
        "command": f"curl -H 'x: {REDACTED}'",
        "nested": [{"token": REDACTED}, 3, None, True],
        REDACTED: "key",
        "tuple": [REDACTED],
    }


def test_normal_text_is_not_changed() -> None:
    text = "Run `git status` with sk-short and the id 12345:AB, see https://example.test/a?b=c"
    assert Redactor().text(text) == text


def test_env_secrets_reads_the_set_names_and_url_passwords() -> None:
    env = {
        "A": " value-a ",
        "B": "",
        "URL": "postgresql://u:url-pass@h/db",
    }
    assert env_secrets("A", "B", "C", "URL", env=env) == [
        "value-a",
        "postgresql://u:url-pass@h/db",
        "url-pass",
    ]
