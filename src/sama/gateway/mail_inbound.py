"""Inbound email from the SendGrid Inbound Parse webhook (docs/architecture.md, Section 11.9).

- ``signed`` checks the signature of the security policy of the parse webhook: an ECDSA
  P-256 signature (SHA-256) of the timestamp and the raw body, with the public key of the
  policy (https://www.twilio.com/docs/sendgrid/for-developers/parsing-email/securing-your-parse-webhooks).
- ``parse`` reads the ``multipart/form-data`` body, in the default form and in the raw form
  (``send_raw``), into one ``MailIn``.
- ``authenticated`` checks that the domain of the ``From`` address passed DKIM, or passed SPF
  with the same envelope domain. A message that fails is dropped, because a ``From`` header
  alone is easy to forge.
- ``reply_text`` cuts the quoted text of a reply.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from email.message import EmailMessage, Message
from email.parser import BytesParser, HeaderParser
from email.policy import default
from email.utils import getaddresses, parseaddr
from typing import Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_der_public_key, load_pem_public_key

SIGNATURE_HEADER: Final = "x-twilio-email-event-webhook-signature"
TIMESTAMP_HEADER: Final = "x-twilio-email-event-webhook-timestamp"
MAX_SKEW_S: Final = 600
"""A signed request older or newer than this is refused, so a copy cannot come back later."""
MAX_FIELDS: Final = 200
_TAG: Final = re.compile(r"t([0-9a-f]{16})")
_REFERENCE: Final = re.compile(r"<sama\.([0-9a-f]{16})\.")
_DKIM: Final = re.compile(r"@([A-Za-z0-9.-]+)\s*:\s*([A-Za-z]+)")


class BadKey(ValueError):
    """The public key is not an ECDSA P-256 key."""


def public_key(text: str) -> ec.EllipticCurvePublicKey:
    """The key of a security policy: Base64 DER (as SendGrid shows it) or PEM."""
    value = text.strip()
    try:
        if value.startswith("-----BEGIN"):
            key = load_pem_public_key(value.encode())
        else:
            key = load_der_public_key(base64.b64decode(value, validate=True))
    except (ValueError, binascii.Error, TypeError):
        raise BadKey("the public key cannot be read") from None
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise BadKey("the public key is not an ECDSA P-256 key")
    return key


def signed(
    key: ec.EllipticCurvePublicKey,
    signature: str | None,
    timestamp: str | None,
    body: bytes,
    *,
    now: float,
) -> bool:
    """True when SendGrid signed the request and its time is within ``MAX_SKEW_S``."""
    if not signature or not timestamp or not timestamp.isdigit():
        return False
    if abs(now - int(timestamp)) > MAX_SKEW_S:
        return False
    try:
        raw = base64.b64decode(signature, validate=True)
        key.verify(raw, timestamp.encode() + body, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, binascii.Error):
        return False
    return True


@dataclass(frozen=True, slots=True)
class MailFile:
    name: str
    mime_type: str
    data: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class MailIn:
    message_id: str
    sender: str
    """The ``From`` address, in lower case."""
    subject: str
    text: str
    recipients: tuple[str, ...]
    """The envelope recipients and the ``To`` and ``Cc`` addresses, in lower case."""
    references: tuple[str, ...]
    """``In-Reply-To`` and ``References``."""
    envelope_from: str
    spf: str
    dkim: str
    files: tuple[MailFile, ...] = ()

    @property
    def domain(self) -> str:
        return self.sender.rpartition("@")[2]


def _form(body: bytes, content_type: str) -> list[Message]:
    """The parts of a ``multipart/form-data`` body."""
    if "multipart/form-data" not in content_type.lower():
        return []
    head = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    message = BytesParser(policy=default).parsebytes(head + body)
    if not message.is_multipart():
        return []
    parts = message.get_payload()
    return list(parts)[:MAX_FIELDS] if isinstance(parts, list) else []


def _field_name(part: Message) -> str:
    return str(part.get_param("name", header="content-disposition") or "")


def _bytes(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else b""


def _text(part: Message) -> str:
    return _bytes(part).decode("utf-8", errors="replace")


def _addresses(*values: str) -> list[str]:
    return [address.lower() for _, address in getaddresses(list(values)) if "@" in address]


def _references(in_reply_to: str, references: str) -> tuple[str, ...]:
    found = re.findall(r"<[^<>\s]+>", f"{in_reply_to} {references}")
    return tuple(dict.fromkeys(found))


def _plain(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
    return re.sub(r"<[^>]+>", "", text)


def _raw(raw: bytes) -> tuple[EmailMessage, str, list[MailFile]]:
    message = BytesParser(policy=default).parsebytes(raw)
    assert isinstance(message, EmailMessage)  # noqa: S101 (the default policy gives one)
    body = message.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        content = body.get_content()
        text = content if body.get_content_subtype() == "plain" else _plain(str(content))
    files = [
        MailFile(
            str(part.get_filename() or "file"),
            part.get_content_type(),
            _bytes(part),
        )
        for part in message.iter_attachments()
    ]
    return message, str(text), files


def parse(body: bytes, content_type: str) -> MailIn | None:
    """The email of a parse webhook body, or ``None`` for a body without a sender."""
    fields: dict[str, str] = {}
    uploads: dict[str, MailFile] = {}
    for part in _form(body, content_type):
        name = _field_name(part)
        if part.get_filename():
            uploads[name] = MailFile(
                str(part.get_filename()), part.get_content_type(), _bytes(part)
            )
        elif name and name not in fields:
            fields[name] = _text(part)
    files: list[MailFile]
    if "email" in fields:
        # ``send_raw``: the whole message as MIME.
        raw_part = next(p for p in _form(body, content_type) if _field_name(p) == "email")
        headers, text, files = _raw(_bytes(raw_part))
    else:
        headers = HeaderParser(policy=default).parsestr(fields.get("headers", ""))
        text = fields.get("text") or _plain(fields.get("html", ""))
        info: dict[str, object] = {}
        try:
            loaded = json.loads(fields.get("attachment-info") or "{}")
            info = loaded if isinstance(loaded, dict) else {}
        except ValueError:
            info = {}
        files = []
        for name, upload in uploads.items():
            meta = info.get(name)
            mime = (
                str(meta.get("type") or upload.mime_type)
                if isinstance(meta, dict)
                else upload.mime_type
            )
            files.append(MailFile(upload.name, mime, upload.data))
    sender = parseaddr(str(headers.get("from") or fields.get("from") or ""))[1].lower()
    if "@" not in sender:
        return None
    envelope: dict[str, object] = {}
    try:
        loaded_envelope = json.loads(fields.get("envelope") or "{}")
        envelope = loaded_envelope if isinstance(loaded_envelope, dict) else {}
    except ValueError:
        envelope = {}
    envelope_to = envelope.get("to")
    listed = [str(a) for a in envelope_to] if isinstance(envelope_to, list) else []
    recipients = _addresses(
        *listed,
        str(headers.get("to") or fields.get("to") or ""),
        str(headers.get("cc") or fields.get("cc") or ""),
    )
    return MailIn(
        message_id=str(headers.get("message-id") or "").strip(),
        sender=sender,
        subject=str(headers.get("subject") or fields.get("subject") or "").strip(),
        text=text,
        recipients=tuple(dict.fromkeys(recipients)),
        references=_references(
            str(headers.get("in-reply-to") or ""), str(headers.get("references") or "")
        ),
        envelope_from=str(envelope.get("from") or "").lower(),
        spf=fields.get("SPF", "").strip().lower(),
        dkim=fields.get("dkim", ""),
        files=tuple(files),
    )


def _aligned(domain: str, other: str) -> bool:
    """Relaxed alignment: the same domain or a subdomain of it."""
    domain, other = domain.lower().rstrip("."), other.lower().rstrip(".")
    return bool(other) and (domain == other or domain.endswith(f".{other}"))


def authenticated(mail: MailIn) -> bool:
    """True when DKIM passed for the domain of ``From``, or SPF passed with the same envelope
    domain (Section 11.9)."""
    for domain, result in _DKIM.findall(mail.dkim):
        if result.lower() == "pass" and _aligned(mail.domain, domain):
            return True
    envelope_domain = mail.envelope_from.rpartition("@")[2]
    return mail.spf == "pass" and _aligned(mail.domain, envelope_domain)


def addressed_to(mail: MailIn, address: str) -> str | None:
    """The thread tag when ``mail`` went to ``address`` or to a ``+tag`` form of it, ``""``
    without a tag, and ``None`` when ``mail`` did not go to ``address``."""
    local, _, domain = address.lower().partition("@")
    found: str | None = None
    for recipient in mail.recipients:
        name, _, host = recipient.lower().partition("@")
        base, plus, tag = name.partition("+")
        if host != domain or base != local:
            continue
        match = _TAG.fullmatch(tag) if plus else None
        if match is not None:
            return match.group(1)
        found = ""
    return found


def thread_of(mail: MailIn, tag: str) -> str | None:
    """The thread token of the mail: the ``+tag`` of the address, else a ``Message-ID`` of
    sama in the references."""
    if tag:
        return tag
    for reference in reversed(mail.references):
        match = _REFERENCE.match(reference)
        if match is not None:
            return match.group(1)
    return None


_QUOTE_START: Final = (
    re.compile(r"^On .{0,300}wrote:\s*$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^_{10,}\s*$"),
    re.compile(r"^From: .+$"),
)


def reply_text(text: str) -> str:
    """The new text of a reply: the lines before the quoted message."""
    lines: list[str] = []
    raw = text.replace("\r\n", "\n").split("\n")
    for index, line in enumerate(raw):
        stripped = line.strip()
        if stripped.startswith(">"):
            break
        joined = f"{stripped} {raw[index + 1].strip()}" if index + 1 < len(raw) else stripped
        if any(p.match(stripped) for p in _QUOTE_START) or (
            stripped.startswith("On ") and _QUOTE_START[0].match(joined)
        ):
            break
        if stripped == "--":
            break
        lines.append(line.rstrip())
    return "\n".join(lines).strip()
