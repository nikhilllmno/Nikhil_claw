"""Envelope encryption for the sama vault (docs/architecture.md, Section 16.3).

Each value gets its own random data key. AES-256-GCM encrypts the value with the data key, and
the vault key (``SAMA_VAULT_KEY``) encrypts the data key. Both steps bind the ``context`` (the
org and the name of the entry) as associated data, so an entry copied to another org or name
does not decrypt. A KMS can replace the vault key later: only the wrap of the data key changes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import secrets
from dataclasses import dataclass, field
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES: Final = 32
NONCE_BYTES: Final = 12


class VaultError(Exception):
    """A vault entry cannot be read. The message never holds a secret."""


@dataclass(frozen=True, slots=True)
class VaultKey:
    """The key that wraps the data keys. ``key_id`` names the key without showing it."""

    secret: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.secret) != KEY_BYTES:
            raise ValueError(f"the vault key must have {KEY_BYTES} bytes")

    @property
    def key_id(self) -> str:
        return hashlib.sha256(b"sama-vault-key-id:" + self.secret).hexdigest()[:16]

    @classmethod
    def from_text(cls, value: str) -> VaultKey:
        """Reads a key in base64 (standard or URL-safe alphabet)."""
        text = value.strip()
        try:
            raw = base64.b64decode(text.replace("-", "+").replace("_", "/"), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("the vault key must be base64") from None
        return cls(raw)

    @staticmethod
    def generate() -> str:
        """A new random key in base64, for ``SAMA_VAULT_KEY``."""
        return base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode()


@dataclass(frozen=True, slots=True)
class Sealed:
    key_id: str
    wrapped_key: bytes
    """The nonce and the encrypted data key."""
    ciphertext: bytes
    """The nonce and the encrypted value."""


def seal(key: VaultKey, value: str, *, context: bytes) -> Sealed:
    data_key = AESGCM.generate_key(bit_length=KEY_BYTES * 8)
    value_nonce, key_nonce = os.urandom(NONCE_BYTES), os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(data_key).encrypt(value_nonce, value.encode(), context)
    wrapped = AESGCM(key.secret).encrypt(key_nonce, data_key, context)
    return Sealed(key.key_id, key_nonce + wrapped, value_nonce + ciphertext)


def unseal(key: VaultKey, sealed: Sealed, *, context: bytes) -> str:
    if sealed.key_id != key.key_id:
        raise VaultError(
            f"the entry uses the vault key {sealed.key_id}, not the current key {key.key_id}"
        )
    try:
        data_key = AESGCM(key.secret).decrypt(
            sealed.wrapped_key[:NONCE_BYTES], sealed.wrapped_key[NONCE_BYTES:], context
        )
        plain = AESGCM(data_key).decrypt(
            sealed.ciphertext[:NONCE_BYTES], sealed.ciphertext[NONCE_BYTES:], context
        )
    except (InvalidTag, ValueError):
        raise VaultError(
            "the entry does not decrypt; it is changed or in the wrong place"
        ) from None
    return plain.decode()
