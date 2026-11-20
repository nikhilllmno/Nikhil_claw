"""Signed webhook deliveries (docs/architecture.md, Section 12.2).

Sama uses the Standard Webhooks scheme (https://www.standardwebhooks.com), the same scheme as
Composio. A delivery has three headers:

- ``webhook-id``: the id of the delivery. A retry sends the same id again.
- ``webhook-timestamp``: the time of the delivery, in Unix seconds.
- ``webhook-signature``: one or more space-separated ``v1,<base64>`` values. Each value is an
  HMAC-SHA256 of ``<webhook-id>.<webhook-timestamp>.<raw body>``.

A delivery is valid when one signature matches (compared in constant time) and the timestamp is
at most ``TOLERANCE_S`` from now. The receiver rejects a second delivery with the same id, so a
captured delivery cannot run two times.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import time
from collections.abc import Callable, Mapping
from typing import Final

from fastapi import HTTPException, Request

TOLERANCE_S: Final = 300
"""The largest difference between the timestamp of a delivery and the clock of sama."""
MAX_BODY_BYTES: Final = 1024 * 1024
"""The largest body of a delivery. A longer body gets 413."""
MAX_ID_CHARS: Final = 200
SECRET_PREFIX: Final = "whsec_"  # noqa: S105 (a prefix, not a secret)
KEY_BYTES: Final = 32
MAX_SIGNATURES: Final = 10
"""A header with more signatures is not valid: a sender rotates one key at a time."""


class SignatureError(Exception):
    """The delivery is not signed correctly. The message is safe to log."""


def new_secret() -> str:
    """A new signing secret for a webhook trigger: ``whsec_`` and a base64 key of 32 bytes."""
    return SECRET_PREFIX + base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


def standard_key(secret: str) -> bytes:
    """The HMAC key of a Standard Webhooks secret: the base64 part after ``whsec_``."""
    encoded = secret.removeprefix(SECRET_PREFIX)
    try:
        key = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise SignatureError("the signing secret is not valid") from None
    if not key:
        raise SignatureError("the signing secret is not valid")
    return key


def composio_key(secret: str) -> bytes:
    """The HMAC key of a Composio webhook secret: the UTF-8 bytes of the secret, as the
    Composio SDK uses it."""
    return secret.encode("utf-8")


def sign(key: bytes, delivery_id: str, timestamp: int, body: bytes) -> str:
    """The ``webhook-signature`` value of a delivery. Tests and senders use it."""
    digest = hmac.new(key, f"{delivery_id}.{timestamp}.".encode() + body, hashlib.sha256)
    return "v1," + base64.b64encode(digest.digest()).decode("ascii")


def delivery_id(headers: Mapping[str, str]) -> str:
    """The ``webhook-id`` of a delivery. It is the replay key."""
    value = headers.get("webhook-id", "").strip()
    if not value or len(value) > MAX_ID_CHARS or not value.isprintable():
        raise SignatureError("the webhook-id header is missing or not valid")
    return value


def verify(
    keys: list[bytes],
    headers: Mapping[str, str],
    body: bytes,
    *,
    clock: Callable[[], float] = time.time,
    tolerance_s: int = TOLERANCE_S,
) -> str:
    """Checks the signature and the age of a delivery with each key, and returns its
    ``webhook-id``. Raises ``SignatureError`` when no key gives a valid signature."""
    identifier = delivery_id(headers)
    raw_timestamp = headers.get("webhook-timestamp", "").strip()
    if not raw_timestamp.isdigit() or len(raw_timestamp) > 12:
        raise SignatureError("the webhook-timestamp header is missing or not valid")
    timestamp = int(raw_timestamp)
    if abs(clock() - timestamp) > tolerance_s:
        raise SignatureError("the delivery is too old or in the future")
    given = headers.get("webhook-signature", "").split()
    if not given or len(given) > MAX_SIGNATURES:
        raise SignatureError("the webhook-signature header is missing or not valid")
    for key in keys:
        expected = sign(key, identifier, timestamp, body).encode("ascii")
        # Compare with every value, so the time does not show which value matched.
        matched = False
        for value in given:
            matched |= hmac.compare_digest(expected, value.encode("utf-8", errors="replace"))
        if matched:
            return identifier
    raise SignatureError("the signature does not match")


async def read_body(request: Request, limit: int = MAX_BODY_BYTES) -> bytes:
    """The raw body of a delivery: the signature covers the bytes as the sender sent them. The
    limit holds while the body streams, so a chunked body with no Content-Length cannot make
    the process read more than ``limit`` bytes."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise HTTPException(status_code=413, detail="the body is too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail="the body is too large")
        chunks.append(chunk)
    return b"".join(chunks)
