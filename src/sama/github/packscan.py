"""A size check of a git pack before ``git index-pack`` reads it (docs/architecture.md,
Section 15.1).

``git index-pack`` inflates each commit, tree, tag, and delta into memory. A small pack can
hold a very large object, because zlib compresses repeated bytes very well. So the proxy reads
the header of each object first and inflates it in small parts, with no copy of the output:

- A commit or a tag is at most ``MAX_COMMIT_BYTES``. A tree is at most ``MAX_TREE_BYTES``.
- A delta, its base, and its result are at most ``MAX_DELTA_BYTES`` each.
- A blob can be larger: ``git index-pack`` streams a blob that is larger than
  ``core.bigFileThreshold`` (the proxy sets ``INDEX_PACK_CONFIG``).
- All objects together inflate to at most ``MAX_INFLATED_BYTES``, and a pack has at most
  ``MAX_PACK_OBJECTS`` objects.

Each object must inflate to the size of its header. ``git index-pack`` checks the rest of the
pack (the object ids, the delta bases, and the checksum).
"""

from __future__ import annotations

import zlib
from typing import Final

MAX_COMMIT_BYTES: Final = 1024 * 1024
"""The largest commit or tag object in a push."""
MAX_TREE_BYTES: Final = 16 * 1024 * 1024
"""The largest tree object in a push."""
MAX_DELTA_BYTES: Final = 64 * 1024 * 1024
"""The largest delta, delta base, or delta result in a push."""
MAX_INFLATED_BYTES: Final = 1024 * 1024 * 1024
"""The most bytes that all the objects of one push inflate to."""
MAX_PACK_OBJECTS: Final = 200_000
"""The most objects in the pack of one push."""
INDEX_PACK_CONFIG: Final = (
    "-c",
    "core.bigFileThreshold=1m",
    "-c",
    "core.deltaBaseCacheLimit=16m",
)
"""Git settings for ``index-pack``: stream each blob above 1 MB, and keep a small cache."""

_PART: Final = 64 * 1024
_COMMIT, _TREE, _BLOB, _TAG, _OFS_DELTA, _REF_DELTA = 1, 2, 3, 4, 6, 7
_NAMES: Final = {_COMMIT: "commit", _TREE: "tree", _BLOB: "blob", _TAG: "tag"}
_LIMITS: Final = {_COMMIT: MAX_COMMIT_BYTES, _TAG: MAX_COMMIT_BYTES, _TREE: MAX_TREE_BYTES}


class PackScanError(Exception):
    """The pack is too large to check, or it is not a valid pack. No secret in the message."""


def scan_pack(pack: bytes) -> None:
    """Raises ``PackScanError`` when an object of the pack is too large or not valid."""
    if len(pack) < 12 or pack[:4] != b"PACK":
        raise PackScanError("the push has no valid pack")
    if int.from_bytes(pack[4:8], "big") not in (2, 3):
        raise PackScanError("the push has a pack of an unknown version")
    count = int.from_bytes(pack[8:12], "big")
    if count > MAX_PACK_OBJECTS:
        raise PackScanError(f"the push has more than {MAX_PACK_OBJECTS} objects")
    at = 12
    budget = MAX_INFLATED_BYTES
    for _ in range(count):
        kind, size, at = _header(pack, at)
        if kind == _OFS_DELTA:
            at = _skip_offset(pack, at)
        elif kind == _REF_DELTA:
            at += 20
        elif kind not in _NAMES:
            raise PackScanError("the push has an object of an unknown type")
        limit = _LIMITS.get(kind, MAX_DELTA_BYTES if kind in (_OFS_DELTA, _REF_DELTA) else None)
        if limit is not None and size > limit:
            name = _NAMES.get(kind, "delta")
            raise PackScanError(f"the push has a {name} that is larger than {limit} bytes")
        if size > budget:
            raise PackScanError(
                f"the objects of the push are larger than {MAX_INFLATED_BYTES} bytes"
            )
        budget -= size
        at = _inflate(pack, at, size, delta=kind in (_OFS_DELTA, _REF_DELTA))


def _header(pack: bytes, at: int) -> tuple[int, int, int]:
    """The type and the inflated size of the object at ``at``, and the offset of its data."""
    byte = _byte(pack, at)
    kind = (byte >> 4) & 7
    size = byte & 15
    shift = 4
    at += 1
    while byte & 0x80:
        if shift > 60:
            raise PackScanError("the push has an object with a bad size")
        byte = _byte(pack, at)
        size |= (byte & 0x7F) << shift
        shift += 7
        at += 1
    return kind, size, at


def _skip_offset(pack: bytes, at: int) -> int:
    for _ in range(10):
        byte = _byte(pack, at)
        at += 1
        if not byte & 0x80:
            return at
    raise PackScanError("the push has a delta with a bad offset")


def _byte(pack: bytes, at: int) -> int:
    if at >= len(pack):
        raise PackScanError("the pack of the push is not complete")
    return pack[at]


def _inflate(pack: bytes, at: int, size: int, *, delta: bool) -> int:
    """Inflates one object in parts and returns the offset after it. For a delta, it also
    checks the sizes of the base and of the result."""
    inflate = zlib.decompressobj()
    total = 0
    head = b""
    data = memoryview(pack)[at:]
    while not inflate.eof:
        source: bytes | memoryview = inflate.unconsumed_tail
        if not source and data:
            source, data = data[:_PART], data[_PART:]
        try:
            out = inflate.decompress(source, _PART)
        except zlib.error as error:
            raise PackScanError("the push has an object that does not inflate") from error
        if not source and not out and not inflate.eof:
            raise PackScanError("the pack of the push is not complete")
        total += len(out)
        if total > size:
            raise PackScanError("the push has an object that is larger than its header")
        if delta and len(head) < 20:
            head += out[: 20 - len(head)]
    if total != size:
        raise PackScanError("the push has an object that is smaller than its header")
    if delta:
        base, rest = _delta_size(head)
        result, _ = _delta_size(head[rest:])
        if max(base, result) > MAX_DELTA_BYTES:
            raise PackScanError(f"the push has a delta that is larger than {MAX_DELTA_BYTES} bytes")
    return len(pack) - len(data) - len(inflate.unused_data) - len(inflate.unconsumed_tail)


def _delta_size(head: bytes) -> tuple[int, int]:
    """A size at the start of the data of a delta, and the number of bytes that it uses."""
    size = 0
    for index, byte in enumerate(head[:10]):
        size |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            return size, index + 1
    raise PackScanError("the push has a delta with a bad size")
