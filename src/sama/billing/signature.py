"""The signature of a Stripe webhook delivery (docs/architecture.md, Section 17.5).

Stripe sends the header ``Stripe-Signature: t=<unix seconds>,v1=<hex>[,v1=<hex>...]``. Each
``v1`` value is an HMAC-SHA256 of ``<t>.<raw body>``. The key is the UTF-8 bytes of the
endpoint secret (``whsec_...``). A delivery is valid when one ``v1`` value matches (compared in
constant time) and ``t`` is at most ``TOLERANCE_S`` from now. The ``billing_events`` table
stops a second application of the same event.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable
from typing import Final

from sama.triggers.signature import TOLERANCE_S, SignatureError

MAX_HEADER_CHARS: Final = 2000
MAX_SIGNATURES: Final = 10

__all__ = ["SignatureError", "sign", "verify"]


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """The ``Stripe-Signature`` header of a delivery. Tests use it."""
    digest = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={digest.hexdigest()}"


def verify(
    secret: str,
    header: str | None,
    body: bytes,
    *,
    clock: Callable[[], float] = time.time,
    tolerance_s: int = TOLERANCE_S,
) -> int:
    """Checks the signature and the age of a delivery, and returns its timestamp. Raises
    ``SignatureError`` when the delivery is not valid. The messages are safe to log."""
    if not secret:
        raise SignatureError("the webhook secret is not set")
    if not header or len(header) > MAX_HEADER_CHARS:
        raise SignatureError("the Stripe-Signature header is missing or not valid")
    timestamps: list[str] = []
    given: list[str] = []
    for part in header.split(","):
        name, _, value = part.strip().partition("=")
        if name == "t":
            timestamps.append(value)
        elif name == "v1":
            given.append(value)
    if len(timestamps) != 1 or not timestamps[0].isdigit() or len(timestamps[0]) > 12:
        raise SignatureError("the Stripe-Signature header has no valid timestamp")
    if not given or len(given) > MAX_SIGNATURES:
        raise SignatureError("the Stripe-Signature header has no v1 signature")
    timestamp = int(timestamps[0])
    if abs(clock() - timestamp) > tolerance_s:
        raise SignatureError("the delivery is too old or in the future")
    expected = sign(secret, timestamp, body).partition(",v1=")[2].encode("ascii")
    matched = False
    # Compare with every value, so the time does not show which value matched.
    for value in given:
        matched |= hmac.compare_digest(expected, value.encode("utf-8", errors="replace"))
    if not matched:
        raise SignatureError("the signature does not match")
    return timestamp
