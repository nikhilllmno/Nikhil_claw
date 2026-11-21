"""Helpers that build SendGrid Inbound Parse requests for tests (Section 11.9)."""

from __future__ import annotations

import base64
import json
from email.message import EmailMessage

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

BOUNDARY = "xYzZY"


def key_pair() -> tuple[ec.EllipticCurvePrivateKey, str]:
    """A private key and its public key as SendGrid shows it (Base64 DER)."""
    private = ec.generate_private_key(ec.SECP256R1())
    der = private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, base64.b64encode(der).decode()


def sign(private: ec.EllipticCurvePrivateKey, timestamp: int, body: bytes) -> dict[str, str]:
    signature = private.sign(str(timestamp).encode() + body, ec.ECDSA(hashes.SHA256()))
    return {
        "X-Twilio-Email-Event-Webhook-Signature": base64.b64encode(signature).decode(),
        "X-Twilio-Email-Event-Webhook-Timestamp": str(timestamp),
    }


def form(fields: dict[str, str], files: dict[str, tuple[str, str, bytes]] | None = None) -> bytes:
    """A ``multipart/form-data`` body with the boundary ``BOUNDARY``."""
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            + value.encode()
            + b"\r\n"
        )
    for name, (filename, mime, data) in (files or {}).items():
        parts.append(
            (
                f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}";'
                f' filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'
            ).encode()
            + data
            + b"\r\n"
        )
    return b"".join(parts) + f"--{BOUNDARY}--\r\n".encode()


CONTENT_TYPE = f"multipart/form-data; boundary={BOUNDARY}"


def parse_fields(
    *,
    sender: str = "mia@example.com",
    to: str = "sama@mail.sama.test",
    subject: str = "Hello",
    text: str = "hi",
    message_id: str = "<m1@example.com>",
    in_reply_to: str = "",
    dkim: str = "{@example.com : pass}",
    spf: str = "pass",
) -> dict[str, str]:
    headers = f"Message-ID: {message_id}\nFrom: Mia <{sender}>\nTo: {to}\nSubject: {subject}\n"
    if in_reply_to:
        headers += f"In-Reply-To: {in_reply_to}\nReferences: {in_reply_to}\n"
    return {
        "headers": headers,
        "from": f"Mia <{sender}>",
        "to": to,
        "subject": subject,
        "text": text,
        "envelope": json.dumps({"to": [to], "from": sender}),
        "dkim": dkim,
        "SPF": spf,
    }


def raw_email(*, sender: str, to: str, subject: str, text: str, image: bytes | None) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = "<raw1@example.com>"
    message.set_content(text)
    if image is not None:
        message.add_attachment(image, maintype="image", subtype="png", filename="cat.png")
    return bytes(message)
