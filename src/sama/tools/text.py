"""Readable text from a fetched page, for ``web_extract``."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Final

_SKIP: Final = frozenset({"script", "style", "noscript", "template", "svg", "canvas", "iframe"})
_BLOCK: Final = frozenset(
    {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "fieldset",
        "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
        "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tr", "ul",
    }
)  # fmt: skip
_TEXT_TYPES: Final = (
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/xml",
    "application/json",
    "application/xml",
    "application/ld+json",
)
_SPACES = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")


class UnsupportedContentError(Exception):
    """The page is not HTML or text."""


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False
        self._in_head = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "head":
            self._in_head = True
        elif tag in _BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag == "head":
            self._in_head = False
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
        elif not self._in_head:
            self.parts.append(data)


def _tidy(text: str) -> str:
    lines = (_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> tuple[str, str]:
    """Returns the title and the visible text of an HTML page."""
    parser = _TextParser()
    parser.feed(html)
    parser.close()
    return _tidy(parser.title), _tidy("".join(parser.parts))


def charset_of(content_type: str) -> str:
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            return value.strip("\"' ")
    return "utf-8"


def page_text(content: bytes, content_type: str) -> tuple[str, str]:
    """Returns the title (may be empty) and the text of a page. Raises
    ``UnsupportedContentError`` for types that are not HTML or text."""
    media = content_type.split(";", 1)[0].strip().lower()
    try:
        decoded = content.decode(charset_of(content_type), errors="replace")
    except LookupError:
        decoded = content.decode("utf-8", errors="replace")
    if media in ("text/html", "application/xhtml+xml") or (
        not media and decoded.lstrip()[:15].lower().startswith(("<!doctype html", "<html"))
    ):
        return html_to_text(decoded)
    if media in _TEXT_TYPES or media.endswith(("+json", "+xml")):
        return "", decoded.strip()
    if not media and b"\x00" not in content[:4096]:
        return "", decoded.strip()
    raise UnsupportedContentError(f"the content type {media} is not supported")
