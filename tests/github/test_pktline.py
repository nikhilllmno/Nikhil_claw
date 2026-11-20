"""The pkt-line helpers of the git proxy (docs/architecture.md, Section 15.1)."""

from __future__ import annotations

import pytest

from sama.github.pktline import (
    FLUSH,
    MAX_PKT_DATA,
    ZERO_SHA,
    PktLineError,
    RefUpdate,
    encode,
    parse_push,
    read,
    report,
    rewrite_advertisement,
)

A = "a" * 40
B = "b" * 40


def packets(body: bytes) -> list[bytes | None]:
    out: list[bytes | None] = []
    offset = 0
    while offset < len(body):
        data, offset = read(body, offset)
        out.append(data)
    return out


def test_encode_and_read_round_trip() -> None:
    body = encode(b"hello\n") + FLUSH + encode(b"x")
    assert body[:4] == b"000a"
    assert packets(body) == [b"hello\n", None, b"x"]


def test_a_too_long_packet_is_refused() -> None:
    encode(b"x" * MAX_PKT_DATA)
    with pytest.raises(PktLineError):
        encode(b"x" * (MAX_PKT_DATA + 1))


@pytest.mark.parametrize("body", [b"00", b"zzzz", b"0010abc"])
def test_bad_data_is_refused(body: bytes) -> None:
    with pytest.raises(PktLineError):
        read(body, 0)


def test_delimiter_and_response_end_packets_have_no_data() -> None:
    assert read(b"0001", 0) == (None, 4)
    assert read(b"0002", 0) == (None, 4)


def advertisement(caps: str) -> bytes:
    return (
        encode(b"# service=git-receive-pack\n")
        + FLUSH
        + encode(f"{A} refs/heads/main\0{caps}\n".encode())
        + encode(f"{B} refs/heads/other\n".encode())
        + FLUSH
    )


def test_the_advertisement_loses_the_side_band_and_gets_no_thin() -> None:
    caps = (
        "report-status report-status-v2 delete-refs side-band-64k quiet atomic ofs-delta "
        "push-options push-cert=abc object-format=sha1 agent=git/github"
    )
    out = packets(rewrite_advertisement(advertisement(caps)))
    assert out[0] == b"# service=git-receive-pack\n"
    assert out[1] is None
    ref, _, kept = (out[2] or b"").partition(b"\0")
    assert ref == f"{A} refs/heads/main".encode()
    names = kept.strip().split(b" ")
    assert b"report-status" in names
    assert b"no-thin" in names
    assert b"object-format=sha1" in names
    for removed in (b"side-band-64k", b"report-status-v2", b"push-options", b"push-cert=abc"):
        assert removed not in names
    assert out[3] == f"{B} refs/heads/other\n".encode()
    assert out[4] is None


def test_an_advertisement_without_capabilities_is_refused() -> None:
    with pytest.raises(PktLineError):
        rewrite_advertisement(encode(b"# service=git-receive-pack\n") + FLUSH)


def push_body(*lines: str, caps: str = "report-status", pack: bytes = b"PACK") -> bytes:
    out = bytearray()
    for index, line in enumerate(lines):
        text = f"{line}\0{caps}" if index == 0 else line
        out += encode(text.encode() + b"\n")
    return bytes(out) + FLUSH + pack


def test_a_push_parses() -> None:
    push = parse_push(
        push_body(f"{A} {B} refs/heads/sama/x", f"{ZERO_SHA} {A} refs/heads/sama/y", caps="a b")
    )
    assert push.updates == (
        RefUpdate(A, B, "refs/heads/sama/x"),
        RefUpdate(ZERO_SHA, A, "refs/heads/sama/y"),
    )
    assert push.capabilities == frozenset({"a", "b"})
    assert push.pack == b"PACK"
    assert push.updates[1].creates
    assert not push.updates[0].creates
    assert RefUpdate(A, ZERO_SHA, "refs/heads/sama/x").deletes


@pytest.mark.parametrize(
    "line",
    [f"shallow {A}", f"{A} refs/heads/x", f"{A} {B}", f"{A[:-1]} {B} refs/heads/x", "nonsense"],
)
def test_bad_push_commands_are_refused(line: str) -> None:
    with pytest.raises(PktLineError):
        parse_push(push_body(line))


def test_a_push_without_commands_is_refused() -> None:
    with pytest.raises(PktLineError):
        parse_push(FLUSH + b"PACK")


def test_the_report_rejects_every_ref() -> None:
    updates = [RefUpdate(A, B, "refs/heads/main"), RefUpdate(A, B, "refs/heads/sama/x")]
    out = packets(report([("refs/heads/main", "no\npushes   to main")], updates))
    assert out == [
        b"unpack ok\n",
        b"ng refs/heads/main no pushes to main\n",
        b"ng refs/heads/sama/x another ref of the push was rejected\n",
        None,
    ]
