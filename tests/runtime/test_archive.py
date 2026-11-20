"""Unit tests for the transcript archive of a compaction (Section 6.3)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from sama.runtime.archive import (
    MAX_ARCHIVE_CHARS,
    MAX_ENTRIES,
    MAX_ENTRY_CHARS,
    ArchiveEntry,
    RemoteArchiver,
    TranscriptArchive,
    fit,
)
from sama.runtime.claude_transcript import parse_transcript, read_transcript
from tests.support import make_scope


def entry(text: str, role: str = "user") -> ArchiveEntry:
    return ArchiveEntry(role=role, text=text)  # type: ignore[arg-type]


def test_a_short_transcript_is_not_cut() -> None:
    entries = [entry("a"), entry("b", "assistant")]
    assert fit(entries) == (entries, False)


def test_a_long_entry_keeps_its_start_and_its_end() -> None:
    text = "S" * 20_000 + "M" * 20_000 + "E" * 20_000
    (cut,), truncated = fit([entry(text)])
    assert truncated
    assert len(cut.text) <= MAX_ENTRY_CHARS
    assert cut.text.startswith("SSS")
    assert cut.text.endswith("EEE")
    assert "characters cut" in cut.text


def test_a_long_transcript_keeps_its_first_and_last_entries_with_a_marker() -> None:
    entries = [entry(f"entry {n}") for n in range(MAX_ENTRIES + 500)]
    kept, truncated = fit(entries)
    assert truncated
    assert len(kept) <= MAX_ENTRIES
    assert kept[0].text == "entry 0"
    assert kept[-1].text == f"entry {MAX_ENTRIES + 499}"
    markers = [e for e in kept if e.role == "system"]
    assert len(markers) == 1
    omitted = len(entries) - (len(kept) - 1)
    assert markers[0].text == f"[{omitted} entries are not in this archive]"


def test_a_transcript_over_the_text_limit_is_cut_by_characters() -> None:
    entries = [entry("x" * 15_000) for _ in range(300)]
    kept, truncated = fit(entries)
    assert truncated
    assert sum(len(e.text) for e in kept) <= MAX_ARCHIVE_CHARS + 100


async def test_the_remote_archiver_posts_the_transcript_with_the_run_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"entries": 1})

    archiver = RemoteArchiver(
        "https://tools.test/archive", "run-token-1", transport=httpx.MockTransport(handler)
    )
    await archiver.archive(
        make_scope(), TranscriptArchive(trigger="manual", entries=[entry("hello")])
    )
    (request,) = seen
    assert request.headers["authorization"] == "Bearer run-token-1"
    assert json.loads(request.content) == {
        "trigger": "manual",
        "entries": [{"role": "user", "text": "hello"}],
    }
    assert "run-token-1" not in repr(archiver)


async def test_the_remote_archiver_raises_for_an_error_answer() -> None:
    archiver = RemoteArchiver(
        "https://tools.test/archive",
        "t",
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await archiver.archive(make_scope(), TranscriptArchive(trigger="auto", entries=[]))


# -- the transcript file of the CLI ------------------------------------------------------------


def lines(*items: object) -> str:
    return "\n".join(json.dumps(item) for item in items)


def test_the_parser_reads_text_tool_calls_and_tool_results() -> None:
    raw = lines(
        {"type": "user", "message": {"role": "user", "content": "build it"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "private"},
                    {"type": "text", "text": "ok"},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"cmd": "ls"}},
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "a.py"}, {"type": "image"}],
                    }
                ]
            },
        },
        {"type": "summary", "summary": "skipped"},
    )
    assert [(e.role, e.text) for e in parse_transcript(raw)] == [
        ("user", "build it"),
        ("assistant", "ok"),
        ("assistant", '[tool call Bash] {"cmd": "ls"}'),
        ("tool", "a.py\n[image]"),
    ]


def test_the_parser_skips_lines_that_it_does_not_understand() -> None:
    raw = "not json\n[1, 2]\n" + lines(
        {"type": "user", "message": "no content"},
        {"type": "user", "message": {"content": 5}},
        {"type": "user", "message": {"content": [7, {"type": "text", "text": "  "}]}},
        {"type": "assistant", "message": {"content": "fine"}},
    )
    assert [(e.role, e.text) for e in parse_transcript(raw)] == [("assistant", "fine")]


def test_a_long_tool_input_is_cut() -> None:
    raw = lines(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "W", "input": {"d": "x" * 9000}}]},
        }
    )
    (only,) = parse_transcript(raw)
    assert only.text.endswith("[cut]")
    assert len(only.text) < 4_100


def test_an_unreadable_file_gives_no_entries(tmp_path: Path) -> None:
    assert read_transcript(str(tmp_path / "missing.jsonl")) == []


def test_read_transcript_reads_a_file(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text(lines({"type": "user", "message": {"content": "hi"}}), encoding="utf-8")
    assert [e.text for e in read_transcript(str(path))] == ["hi"]
