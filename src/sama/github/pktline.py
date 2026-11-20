"""The pkt-line format of the git smart HTTP protocol (docs/architecture.md, Section 15.1).

The git proxy reads the push commands and changes the capabilities that the server
advertises. It does not read a pack with Python: ``git index-pack`` does that.

A pkt-line has a length of 4 hex digits, then the data. The length includes the 4 digits.
``0000`` is a flush packet, ``0001`` is a delimiter packet, and ``0002`` is a response-end
packet.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

FLUSH: Final = b"0000"
MAX_PKT_DATA: Final = 65516
ZERO_SHA: Final = "0" * 40

ADDED_PUSH_CAPABILITIES: Final = ("no-thin",)
"""The client must send a pack that has all its delta bases, so the proxy can read it alone."""
REMOVED_PUSH_CAPABILITIES: Final = frozenset(
    {"side-band", "side-band-64k", "report-status-v2", "push-cert", "push-options"}
)
"""Without a side band, the proxy can write the push result itself. Without ``push-cert`` and
``push-options``, the request has only the commands and the pack."""

_SHA = re.compile(r"[0-9a-f]{40}")


class PktLineError(ValueError):
    """The data is not valid pkt-line data. The message has no secret."""


def encode(data: bytes) -> bytes:
    if len(data) > MAX_PKT_DATA:
        raise PktLineError("a pkt-line is too long")
    return f"{len(data) + 4:04x}".encode() + data


def read(buffer: bytes, offset: int) -> tuple[bytes | None, int]:
    """One packet at ``offset``: ``(data, next offset)``. ``data`` is ``None`` for a flush,
    delimiter, or response-end packet."""
    head = buffer[offset : offset + 4]
    if len(head) < 4:
        raise PktLineError("the pkt-line data ends too early")
    try:
        size = int(head, 16)
    except ValueError:
        raise PktLineError("a pkt-line has no valid length") from None
    if size < 4:
        return None, offset + 4
    end = offset + size
    if end > len(buffer):
        raise PktLineError("the pkt-line data ends too early")
    return buffer[offset + 4 : end], end


def rewrite_advertisement(body: bytes) -> bytes:
    """The ref advertisement of ``git-receive-pack`` with the changed capabilities.

    The capabilities are on the first ref line, after a NUL byte. Every other packet stays
    the same.
    """
    out = bytearray()
    offset = 0
    changed = False
    while offset < len(body):
        data, offset = read(body, offset)
        if data is None:
            out += body[offset - 4 : offset]
            continue
        if not changed and b"\0" in data:
            ref, _, caps = data.partition(b"\0")
            end = b"\n" if caps.endswith(b"\n") else b""
            kept = [c for c in caps.strip().split(b" ") if c and _name(c) not in _REMOVED]
            kept += [c.encode() for c in ADDED_PUSH_CAPABILITIES if c.encode() not in kept]
            data = ref + b"\0" + b" ".join(kept) + end
            changed = True
        out += encode(data)
    if not changed:
        raise PktLineError("the advertisement has no capabilities")
    return bytes(out)


_REMOVED: Final = frozenset(c.encode() for c in REMOVED_PUSH_CAPABILITIES)


def _name(capability: bytes) -> bytes:
    return capability.split(b"=", 1)[0]


@dataclass(frozen=True, slots=True)
class RefUpdate:
    old: str
    new: str
    ref: str

    @property
    def deletes(self) -> bool:
        return self.new == ZERO_SHA

    @property
    def creates(self) -> bool:
        return self.old == ZERO_SHA


@dataclass(frozen=True, slots=True)
class PushRequest:
    updates: tuple[RefUpdate, ...]
    capabilities: frozenset[str]
    pack: bytes
    """The pack data after the commands. Empty when the push only deletes refs."""


def parse_push(body: bytes) -> PushRequest:
    """The commands, the capabilities, and the pack of a ``git-receive-pack`` request."""
    offset = 0
    updates: list[RefUpdate] = []
    capabilities: frozenset[str] = frozenset()
    while True:
        data, offset = read(body, offset)
        if data is None:
            break
        line = data.rstrip(b"\n")
        if not updates:
            line, _, caps = line.partition(b"\0")
            capabilities = frozenset(c.decode("ascii", "replace") for c in caps.split() if c)
        text = line.decode("utf-8", "replace")
        if text.startswith("shallow "):
            raise PktLineError("a push from a shallow clone is not allowed")
        parts = text.split(" ")
        if len(parts) != 3 or not _SHA.fullmatch(parts[0]) or not _SHA.fullmatch(parts[1]):
            raise PktLineError("a push command is not valid")
        updates.append(RefUpdate(parts[0], parts[1], parts[2]))
    if not updates:
        raise PktLineError("the push has no commands")
    return PushRequest(tuple(updates), capabilities, body[offset:])


def report(rejected: Iterable[tuple[str, str]], updates: Iterable[RefUpdate]) -> bytes:
    """A ``report-status`` result that rejects every ref of the push.

    ``rejected`` gives the reason for some refs. The other refs get a general reason, because
    the proxy sends a push to GitHub as a whole or not at all.
    """
    reasons = dict(rejected)
    out = bytearray(encode(b"unpack ok\n"))
    for update in updates:
        reason = reasons.get(update.ref, "another ref of the push was rejected")
        out += encode(f"ng {update.ref} {_one_line(reason)}\n".encode())
    out += FLUSH
    return bytes(out)


def _one_line(text: str) -> str:
    return " ".join(text.split())[:900]
