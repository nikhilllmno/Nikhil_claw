"""Tests of the size check of a pack before ``git index-pack`` (Section 15.1)."""

from __future__ import annotations

import hashlib
import os
import subprocess
import zlib
from pathlib import Path

import pytest

from sama.github import packscan
from sama.github.packscan import (
    MAX_COMMIT_BYTES,
    MAX_DELTA_BYTES,
    MAX_TREE_BYTES,
    PackScanError,
    scan_pack,
)

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent",
}


GIT = ("git",)


def git(cwd: Path, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(
        [*GIT, *args], cwd=cwd, env=ENV, input=stdin, capture_output=True, check=True
    ).stdout


def header(kind: int, size: int) -> bytes:
    out = bytearray()
    byte = (kind << 4) | (size & 15)
    size >>= 4
    while size:
        out.append(byte | 0x80)
        byte = size & 0x7F
        size >>= 7
    out.append(byte)
    return bytes(out)


def varint(size: int) -> bytes:
    out = bytearray()
    while True:
        byte = size & 0x7F
        size >>= 7
        if size:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def pack_of(*entries: bytes) -> bytes:
    body = b"PACK" + (2).to_bytes(4, "big") + len(entries).to_bytes(4, "big") + b"".join(entries)
    return body + hashlib.sha1(body, usedforsecurity=False).digest()


def entry(kind: int, data: bytes, *, size: int | None = None, extra: bytes = b"") -> bytes:
    return header(kind, len(data) if size is None else size) + extra + zlib.compress(data, 9)


def real_pack(tmp_path: Path) -> bytes:
    """A pack that git makes, with commits, trees, blobs, a tag, and deltas."""
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q")
    text = "".join(f"line {n}\n" for n in range(3000))
    for round_ in range(3):
        (work / "a.txt").write_text(text + f"change {round_}\n")
        (work / "dir").mkdir(exist_ok=True)
        (work / "dir" / f"b{round_}.bin").write_bytes(os.urandom(2000))
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", f"commit {round_}")
    git(work, "tag", "-a", "v1", "-m", "a tag")
    objects = git(work, "rev-list", "--objects", "--all")
    names = b"\n".join(line.split(b" ")[0] for line in objects.splitlines())
    names += b"\n" + git(work, "rev-parse", "v1")
    return git(work, "pack-objects", "--stdout", "--delta-base-offset", stdin=names)


def test_a_pack_that_git_makes_passes(tmp_path: Path) -> None:
    pack = real_pack(tmp_path)
    scan_pack(pack)
    bare = tmp_path / "check.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(bare, *packscan.INDEX_PACK_CONFIG, "index-pack", "--stdin", stdin=pack)


def test_a_pack_with_ref_deltas_passes(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q")
    text = "".join(f"line {n}\n" for n in range(3000))
    for round_ in range(2):
        (work / "a.txt").write_text(text + f"change {round_}\n")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", f"commit {round_}")
    names = b"\n".join(
        line.split(b" ")[0] for line in git(work, "rev-list", "--objects", "--all").splitlines()
    )
    pack = git(work, "pack-objects", "--stdout", stdin=names)
    assert any(byte >> 4 & 7 == 7 for byte in pack[12:]), "the pack has a REF_DELTA"
    scan_pack(pack)


def test_an_empty_pack_passes() -> None:
    scan_pack(pack_of())


@pytest.mark.parametrize(
    ("kind", "limit", "name"),
    [(1, MAX_COMMIT_BYTES, "commit"), (4, MAX_COMMIT_BYTES, "tag"), (2, MAX_TREE_BYTES, "tree")],
)
def test_a_large_object_is_refused_from_its_header(kind: int, limit: int, name: str) -> None:
    """The header says the size, so the check refuses it before it inflates anything."""
    pack = pack_of(entry(kind, b"x", size=limit + 1))
    with pytest.raises(PackScanError, match=f"a {name} that is larger than {limit}"):
        scan_pack(pack)


def test_a_small_pack_with_a_very_large_commit_is_refused() -> None:
    """32 MB of zeros compress to about 32 KB. The header must say the true size, else the
    object does not inflate to its header."""
    data = b"tree " + b"0" * (32 * 1024 * 1024)
    pack = pack_of(entry(1, data))
    assert len(pack) < 64 * 1024
    with pytest.raises(PackScanError, match="larger than"):
        scan_pack(pack)
    lying = pack_of(entry(1, data, size=100))
    with pytest.raises(PackScanError, match="larger than its header"):
        scan_pack(lying)


def test_an_object_smaller_than_its_header_is_refused() -> None:
    with pytest.raises(PackScanError, match="smaller than its header"):
        scan_pack(pack_of(entry(3, b"abc", size=10)))


def test_a_large_blob_passes_because_git_streams_it() -> None:
    scan_pack(pack_of(entry(3, b"\0" * (MAX_DELTA_BYTES + 1))))


@pytest.mark.parametrize("which", ["base", "result"])
def test_a_delta_with_a_large_base_or_result_is_refused(which: str) -> None:
    big, small = MAX_DELTA_BYTES + 1, 10
    sizes = (big, small) if which == "base" else (small, big)
    data = varint(sizes[0]) + varint(sizes[1]) + b"\x01x"
    pack = pack_of(entry(7, data, extra=b"\0" * 20))
    with pytest.raises(PackScanError, match="a delta that is larger than"):
        scan_pack(pack)


def test_a_large_delta_is_refused_from_its_header() -> None:
    pack = pack_of(entry(6, b"x", size=MAX_DELTA_BYTES + 1, extra=b"\x01"))
    with pytest.raises(PackScanError, match="a delta that is larger than"):
        scan_pack(pack)


def test_the_inflated_total_has_a_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(packscan, "MAX_INFLATED_BYTES", 1000)
    pack = pack_of(entry(3, b"a" * 600), entry(3, b"b" * 600))
    with pytest.raises(PackScanError, match="larger than 1000 bytes"):
        scan_pack(pack)


def test_too_many_objects_are_refused() -> None:
    pack = b"PACK" + (2).to_bytes(4, "big") + (packscan.MAX_PACK_OBJECTS + 1).to_bytes(4, "big")
    with pytest.raises(PackScanError, match="more than"):
        scan_pack(pack)


@pytest.mark.parametrize(
    "pack",
    [
        b"",
        b"NOPE" + bytes(8),
        b"PACK" + (9).to_bytes(4, "big") + bytes(4),
        pack_of(entry(5, b"x")),
        pack_of(header(3, 5) + b"not zlib data at all"),
        pack_of(entry(3, b"abc"))[:-25],
        b"PACK" + (2).to_bytes(4, "big") + (1).to_bytes(4, "big") + b"\xff" * 12,
        pack_of(header(6, 1) + b"\xff" * 12),
    ],
    ids=["empty", "magic", "version", "type", "zlib", "cut", "size", "offset"],
)
def test_a_bad_pack_is_refused(pack: bytes) -> None:
    with pytest.raises(PackScanError):
        scan_pack(pack)
