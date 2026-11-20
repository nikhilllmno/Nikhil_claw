"""Tests of the error text that sama keeps and shows (``short_error``)."""

from __future__ import annotations

from sama.connectors.errors import MAX_ERROR_CHARS, short_error
from sama.vault.redact import default_redactor


def test_a_secret_with_spaces_is_redacted_before_the_text_is_put_on_one_line() -> None:
    secret = "zq  word\twith gaps 41"
    default_redactor().add(secret)
    text = short_error(f"the server said:\n  bad password {secret}\n")
    assert "zq" not in text
    assert "gaps" not in text
    assert text.startswith("the server said: bad password ")
    assert "\n" not in text
    default_redactor().forget(secret)


def test_a_long_error_is_cut() -> None:
    text = short_error("x" * (MAX_ERROR_CHARS + 50))
    assert len(text) == MAX_ERROR_CHARS
    assert text.endswith("...")
