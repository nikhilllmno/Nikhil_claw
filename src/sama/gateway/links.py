"""Signed links for channels without buttons (docs/architecture.md, Section 11.9).

- An **approval link** in an email lets the linked user decide an approval from the mail
  client. The token holds the approval, the choice, the user, and the expiry, with an
  HMAC-SHA256 of the email link secret of the integration.
- A **media link** lets Twilio read an image or a voice note that a run made, so that WhatsApp
  can send it. The query holds the expiry and an HMAC-SHA256 of the WhatsApp link secret.

The secrets are random values in the vault. A link never holds a secret of a provider.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Final, Literal

APPROVAL_LINK_S: Final = 24 * 3600
"""How long an approval link works. The approval itself can expire sooner."""
MEDIA_LINK_S: Final = 3600
"""How long a media link works. Twilio reads the file within seconds."""

Choice = Literal["y", "n", "a"]
"""Approve, deny, or approve and always allow for the user (M3.2)."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(secret: str, message: bytes) -> bytes:
    return hmac.new(secret.encode(), message, hashlib.sha256).digest()


@dataclass(frozen=True, slots=True)
class ApprovalLink:
    approval_id: uuid.UUID
    choice: Choice
    user_id: uuid.UUID
    expires: int


def approval_token(
    secret: str,
    approval_id: uuid.UUID,
    choice: Choice,
    user_id: uuid.UUID,
    *,
    now: float,
    ttl_s: int = APPROVAL_LINK_S,
) -> str:
    """The token of one approval link."""
    body = json.dumps(
        {"a": approval_id.hex, "c": choice, "u": user_id.hex, "e": int(now) + ttl_s},
        separators=(",", ":"),
    ).encode()
    return f"{_b64(body)}.{_b64(_mac(secret, body))}"


def read_approval_token(secret: str, token: str, *, now: float) -> ApprovalLink | None:
    """The link of a valid token that has not expired, else ``None``."""
    head, _, mac = token.partition(".")
    try:
        body, given = _unb64(head), _unb64(mac)
    except (ValueError, binascii.Error):
        return None
    if not hmac.compare_digest(_mac(secret, body), given):
        return None
    try:
        data = json.loads(body)
        link = ApprovalLink(
            uuid.UUID(hex=str(data["a"])), data["c"], uuid.UUID(hex=str(data["u"])), int(data["e"])
        )
    except (ValueError, KeyError, TypeError):
        return None
    if link.choice not in ("y", "n", "a") or link.expires < now:
        return None
    return link


def media_signature(
    secret: str, integration_id: uuid.UUID, media_id: uuid.UUID, expires: int
) -> str:
    message = f"{integration_id}:{media_id}:{expires}".encode()
    return _b64(_mac(secret, message))


def media_valid(
    secret: str,
    integration_id: uuid.UUID,
    media_id: uuid.UUID,
    expires: int,
    given: str,
    *,
    now: float,
) -> bool:
    expected = media_signature(secret, integration_id, media_id, expires)
    return expires >= now and hmac.compare_digest(expected, given)
