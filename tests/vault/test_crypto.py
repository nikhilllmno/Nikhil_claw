"""Tests of the envelope encryption of the vault (docs/architecture.md, Section 16.3)."""

from __future__ import annotations

import base64
import dataclasses

import pytest

from sama.vault.crypto import KEY_BYTES, Sealed, VaultError, VaultKey, seal, unseal

KEY = VaultKey(b"k" * KEY_BYTES)


def test_a_sealed_value_opens_with_the_same_key_and_context() -> None:
    sealed = seal(KEY, "sk-the-value", context=b"org-1:brave")
    assert sealed.key_id == KEY.key_id
    assert b"sk-the-value" not in sealed.ciphertext + sealed.wrapped_key
    assert unseal(KEY, sealed, context=b"org-1:brave") == "sk-the-value"


def test_each_seal_uses_a_new_data_key() -> None:
    one, two = (seal(KEY, "value", context=b"c") for _ in range(2))
    assert one.wrapped_key != two.wrapped_key
    assert one.ciphertext != two.ciphertext


def test_another_context_does_not_open() -> None:
    sealed = seal(KEY, "value", context=b"org-1:brave")
    with pytest.raises(VaultError):
        unseal(KEY, sealed, context=b"org-2:brave")


def test_another_key_does_not_open() -> None:
    sealed = seal(KEY, "value", context=b"c")
    other = VaultKey(b"o" * KEY_BYTES)
    with pytest.raises(VaultError, match="vault key"):
        unseal(other, sealed, context=b"c")
    # The same id with other key bytes still fails, on the tag.
    with pytest.raises(VaultError):
        unseal(other, dataclasses.replace(sealed, key_id=other.key_id), context=b"c")


@pytest.mark.parametrize("field", ["wrapped_key", "ciphertext"])
def test_a_changed_byte_does_not_open(field: str) -> None:
    sealed = seal(KEY, "value", context=b"c")
    data = bytearray(getattr(sealed, field))
    data[-1] ^= 1
    changed = (
        dataclasses.replace(sealed, wrapped_key=bytes(data))
        if field == "wrapped_key"
        else dataclasses.replace(sealed, ciphertext=bytes(data))
    )
    with pytest.raises(VaultError):
        unseal(KEY, changed, context=b"c")
    with pytest.raises(VaultError):
        unseal(KEY, Sealed(sealed.key_id, b"short", sealed.ciphertext), context=b"c")


def test_the_key_reads_both_base64_alphabets() -> None:
    raw = bytes(range(250, 256)) * 5 + b"xy"
    assert VaultKey.from_text(base64.b64encode(raw).decode()).secret == raw
    assert VaultKey.from_text(base64.urlsafe_b64encode(raw).decode()).secret == raw
    generated = VaultKey.from_text(VaultKey.generate())
    assert len(generated.secret) == KEY_BYTES


@pytest.mark.parametrize("text", ["not base64!", base64.b64encode(b"short").decode()])
def test_a_bad_key_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="the vault key must"):
        VaultKey.from_text(text)


def test_the_key_id_does_not_show_the_key() -> None:
    assert len(KEY.key_id) == 16
    assert KEY.key_id != VaultKey(b"o" * KEY_BYTES).key_id
    assert "kkkk" not in repr(KEY)
