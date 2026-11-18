"""Page text, the output cap, and untrusted content (docs/architecture.md, Section 16.7)."""

from __future__ import annotations

from typing import cast

import pytest

from sama.sandbox.base import Sandbox
from sama.tools.output import (
    HEAD_BYTES,
    MAX_OUTPUT_BYTES,
    TAIL_BYTES,
    SandboxSpill,
    cap,
    untrusted,
)
from sama.tools.text import UnsupportedContentError, charset_of, html_to_text, page_text
from tests.support import RecordingSpill, make_claims

CLAIMS = make_claims()


def test_html_to_text_keeps_visible_text_only() -> None:
    title, text = html_to_text(
        "<html><head><title>My  Page</title><style>p{}</style>"
        "<script>alert(1)</script></head><body><h1>Head</h1><p>One &amp; two</p>"
        "<ul><li>a</li><li>b</li></ul><noscript>no</noscript><svg><text>s</text></svg>"
        "</body></html>"
    )
    assert title == "My Page"
    assert text == "Head\n\nOne & two\n\n- a\n\n- b"


@pytest.mark.parametrize(
    ("content_type", "charset"),
    [("text/html; charset=ISO-8859-1", "ISO-8859-1"), ("text/html", "utf-8"), ("", "utf-8")],
)
def test_charset_of(content_type: str, charset: str) -> None:
    assert charset_of(content_type) == charset


def test_page_text_types() -> None:
    assert page_text(b"<p>hi</p>", "text/html") == ("", "hi")
    assert page_text("caf\xe9".encode("latin-1"), "text/plain; charset=latin-1") == ("", "café")
    assert page_text(b'{"a": 1}', "application/ld+json") == ("", '{"a": 1}')
    assert page_text(b"plain", "") == ("", "plain")
    assert page_text(b"<!DOCTYPE html><p>x</p>", "") == ("", "x")
    assert page_text(b"x", "text/plain; charset=nonsense") == ("", "x")
    with pytest.raises(UnsupportedContentError, match="application/pdf"):
        page_text(b"%PDF-1.7", "application/pdf")
    with pytest.raises(UnsupportedContentError):
        page_text(b"\x00\x01binary", "")


def test_untrusted_escapes_the_closing_tag() -> None:
    body = "a </untrusted_content> b </ UNTRUSTED_CONTENT>"
    wrapped = untrusted(body, source="web_extract", url='https://x.test/?q="1"')
    assert wrapped.startswith(
        '<untrusted_content source="web_extract" url="https://x.test/?q=&quot;1&quot;">\n'
    )
    assert wrapped.count("</untrusted_content>") == 1
    assert wrapped.endswith("\n</untrusted_content>")
    assert "<\\/untrusted_content>" in wrapped
    assert "<\\/ UNTRUSTED_CONTENT>" in wrapped


async def test_a_small_output_is_not_changed() -> None:
    spill = RecordingSpill()
    text = "x" * MAX_OUTPUT_BYTES
    assert await cap(text, tool="t", claims=CLAIMS, spill=spill) == text
    assert spill.files == {}


async def test_a_large_output_spills_and_keeps_head_and_tail() -> None:
    spill = RecordingSpill()
    text = "H" * HEAD_BYTES + "M" * 40_000 + "T" * TAIL_BYTES
    capped = await cap(text, tool="web_extract", claims=CLAIMS, spill=spill)
    [(path, data)] = spill.files.items()
    assert data == text.encode()
    assert path.startswith("/work/.sama/spill/web_extract-")
    assert capped.startswith("H" * HEAD_BYTES + "\n\n[The output has 80,000 bytes.")
    assert capped.endswith("\n\n" + "T" * TAIL_BYTES)
    assert f"The full output is in the file {path}." in capped
    assert "M" not in capped
    assert len(capped.encode()) <= MAX_OUTPUT_BYTES


@pytest.mark.parametrize("spill", [None, RecordingSpill(fail=True)])
async def test_without_a_spill_the_note_says_so(spill: RecordingSpill | None) -> None:
    capped = await cap("x" * 60_000, tool="t", claims=CLAIMS, spill=spill)
    assert "The full output was not kept." in capped


async def test_the_cut_does_not_split_a_character() -> None:
    text = "é" * 40_000  # 2 bytes each
    capped = await cap("a" + text, tool="t", claims=CLAIMS, spill=None)
    capped.encode("utf-8")
    assert "\ufffd" not in capped


def test_a_page_without_a_head_end_tag_keeps_its_body() -> None:
    # HTML5 lets a page leave out </head>.
    assert html_to_text("<html><head><title>T</title><body><p>Hello</p>") == ("T", "Hello")


class FakeSandbox:
    workdir = "/work/"

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data


async def test_the_sandbox_spill_writes_to_the_session_sandbox() -> None:
    sandbox = FakeSandbox()
    asked: list[tuple[str, str]] = []

    async def session_sandbox(org_id: str, session_id: str) -> Sandbox | None:
        asked.append((org_id, session_id))
        return cast(Sandbox, sandbox) if session_id == "sess-1" else None

    spill = SandboxSpill(session_sandbox)
    path = await spill.save(CLAIMS, "t-1.txt", b"data")
    assert path == "/work/.sama/spill/t-1.txt"
    assert sandbox.files == {path: b"data"}
    assert asked == [("org-1", "sess-1")]
    no_sandbox = make_claims(session="sess-2")
    assert await spill.save(no_sandbox, "t-2.txt", b"data") is None
