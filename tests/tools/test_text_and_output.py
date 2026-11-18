"""Page text, the output cap, and untrusted content (docs/architecture.md, Section 16.7)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from sama.tenancy import RunTokenClaims
from sama.tools.output import HEAD_BYTES, MAX_OUTPUT_BYTES, TAIL_BYTES, cap, untrusted
from sama.tools.text import UnsupportedContentError, charset_of, html_to_text, page_text

CLAIMS = RunTokenClaims(
    org_id="org-1",
    workspace_id="ws-1",
    session_id="sess-1",
    run_id="run-1",
    token_id="t-1",
    expires_at=2_000_000_000,
)


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


@dataclass
class RecordingSpill:
    files: dict[str, bytes] = field(default_factory=dict)
    fail: bool = False

    async def save(self, claims: RunTokenClaims, name: str, data: bytes) -> str | None:
        if self.fail:
            raise OSError("disk full")
        path = f"/work/.sama/spill/{name}"
        self.files[path] = data
        return path


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
