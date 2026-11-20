"""Unit tests for the transcript archive of a compaction (Section 6.3)."""

from __future__ import annotations

import json
import tracemalloc
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from sama.runtime.archive import (
    MAX_ARCHIVE_CHARS,
    MAX_ENTRIES,
    MAX_ENTRY_CHARS,
    MAX_TOOL_NAME_CHARS,
    MAX_WIRE_BYTES,
    MAX_WIRE_ENTRIES,
    MAX_WIRE_ENTRY_BYTES,
    ArchiveEntry,
    RemoteArchiver,
    TranscriptArchive,
    bound_for_wire,
    entry_wire_bytes,
    fit,
    parse_archive,
)
from sama.runtime.claude_transcript import cli_session_exists, parse_transcript, read_transcript
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
    # The runner does not send fields that have no value.
    assert b"tool_input" not in request.content
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
    # A tool call keeps its input as a JSON value: the control plane redacts the value.
    entries, omitted = parse_transcript(raw)
    assert omitted == 0
    assert [(e.role, e.text, e.tool_name, e.tool_input) for e in entries] == [
        ("user", "build it", None, None),
        ("assistant", "ok", None, None),
        ("assistant", "", "Bash", {"cmd": "ls"}),
        ("tool", "a.py\n[image]", None, None),
    ]


def test_the_parser_skips_lines_that_it_does_not_understand() -> None:
    raw = "not json\n[1, 2]\n" + lines(
        {"type": "user", "message": "no content"},
        {"type": "user", "message": {"content": 5}},
        {"type": "user", "message": {"content": [7, {"type": "text", "text": "  "}]}},
        {"type": "assistant", "message": {"content": "fine"}},
    )
    entries, omitted = parse_transcript(raw)
    assert [(e.role, e.text) for e in entries] == [("assistant", "fine")]
    assert omitted == 0


def test_the_parser_does_not_cut_a_tool_input() -> None:
    """A cut before the redaction could split a secret. The control plane cuts later."""
    big = {"d": "x" * 9000, "secret": 'pa"ss\\word\nline'}
    raw = lines(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "W", "input": big}]},
        }
    )
    (only,), omitted = parse_transcript(raw)
    assert omitted == 0
    assert only.tool_input == big
    assert only.text == ""


def test_an_unreadable_file_gives_no_entries(tmp_path: Path) -> None:
    assert read_transcript(str(tmp_path / "missing.jsonl")) == ([], 0)


def test_read_transcript_reads_a_file(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text(lines({"type": "user", "message": {"content": "hi"}}), encoding="utf-8")
    entries, omitted = read_transcript(str(path))
    assert [e.text for e in entries] == ["hi"]
    assert omitted == 0


def test_a_large_entry_of_a_file_stays_out_whole_with_a_marker() -> None:
    raw = lines(
        {"type": "user", "message": {"content": "start"}},
        {"type": "user", "message": {"content": "z" * (MAX_WIRE_ENTRY_BYTES + 1)}},
        {"type": "assistant", "message": {"content": "end"}},
    )
    entries, omitted = parse_transcript(raw)
    assert omitted == 1
    assert [e.role for e in entries] == ["user", "system", "assistant"]
    assert "zzz" not in entries[1].text


def test_a_tool_name_that_is_too_long_stays_out_whole_and_is_not_cut() -> None:
    """The old parser cut the name at 200 characters in the runner, before the redaction: a
    secret that crossed the limit lost its end, and the redactor did not know the rest."""
    secret = "SECRET-VALUE-0123456789"
    name = "n" * (MAX_TOOL_NAME_CHARS - 5) + secret
    raw = lines(
        {"type": "user", "message": {"content": "start"}},
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name, "input": {"a": 1}}]},
        },
        {"type": "assistant", "message": {"content": "end"}},
    )
    entries, omitted = parse_transcript(raw)
    assert omitted == 1
    assert [e.role for e in entries] == ["user", "system", "assistant"]
    dumped = "".join(e.model_dump_json() for e in entries)
    assert secret not in dumped
    assert secret[:6] not in dumped
    assert entries[1].tool_name is None


def test_an_oversized_block_does_not_drop_the_blocks_around_it() -> None:
    """One line (one message) can have many blocks. The size check is for each block, so the
    valid blocks of the same message stay in, and each large block counts as one."""
    huge = "z" * (MAX_WIRE_ENTRY_BYTES + 1)
    raw = lines(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "tool_use", "name": "Write", "input": {"data": huge}},
                    {"type": "tool_use", "name": "Read", "input": {"path": "a.txt"}},
                    {"type": "text", "text": huge},
                    {"type": "text", "text": "last"},
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "1", "content": huge},
                    {"type": "tool_result", "tool_use_id": "2", "content": "small result"},
                ]
            },
        },
    )
    entries, omitted = parse_transcript(raw)
    assert omitted == 3
    assert [(e.role, e.text or e.tool_name) for e in entries] == [
        ("assistant", "first"),
        ("system", "[a large entry is not in this archive]"),
        ("assistant", "Read"),
        ("system", "[a large entry is not in this archive]"),
        ("assistant", "last"),
        ("system", "[a large entry is not in this archive]"),
        ("tool", "small result"),
    ]
    assert "zzz" not in "".join(e.model_dump_json() for e in entries)


def test_a_file_over_the_read_limit_keeps_its_end_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sama.runtime import claude_transcript

    monkeypatch.setattr(claude_transcript, "MAX_TRANSCRIPT_BYTES", 200)
    path = tmp_path / "s.jsonl"
    rows = [{"type": "user", "message": {"content": f"message number {n:03d}"}} for n in range(30)]
    path.write_text(lines(*rows), encoding="utf-8")
    entries, omitted = read_transcript(str(path))
    assert omitted == 1
    assert entries[0].role == "system"
    assert entries[-1].text == "message number 029"


# -- the limits of the wire --------------------------------------------------------------------


def test_a_transcript_in_the_limits_goes_out_whole() -> None:
    entries = [entry("a"), entry("b", "assistant")]
    assert bound_for_wire(entries) == (entries, 0)


def test_the_runner_does_not_cut_inside_an_entry() -> None:
    """A text that is long but in the limit goes out whole, so the control plane can redact
    it whole. The old code cut at 20,000 characters, before the redaction."""
    text = "A" * 30_000 + "SECRET-VALUE" + "B" * 30_000
    (kept,), omitted = bound_for_wire([entry(text)])
    assert kept.text == text
    assert omitted == 0
    big_input = ArchiveEntry(role="assistant", tool_name="W", tool_input={"d": "y" * 50_000})
    ((kept_call,), omitted) = bound_for_wire([big_input])
    assert kept_call == big_input
    assert omitted == 0


def test_an_entry_over_the_limit_stays_out_whole_with_a_marker() -> None:
    # The model refuses a large entry, so the entry is built without the check. This is what
    # ``bound_for_wire`` does for an entry that came from another source.
    huge = ArchiveEntry.model_construct(
        role="user",
        text="\n" * (MAX_WIRE_ENTRY_BYTES // 2 + 1),  # the JSON text escapes each \n
    )
    kept, omitted = bound_for_wire([entry("first"), huge, entry("last")], 2)
    assert omitted == 3
    assert [e.text for e in kept][0::2] == ["first", "last"]
    marker = kept[1]
    assert marker.role == "system"
    assert "is not in this archive" in marker.text
    assert "\n" not in marker.text


def test_too_many_entries_leave_the_middle_out_whole() -> None:
    entries = [entry(f"entry {n}") for n in range(MAX_WIRE_ENTRIES + 700)]
    kept, omitted = bound_for_wire(entries)
    assert len(kept) <= MAX_WIRE_ENTRIES
    assert kept[0].text == "entry 0"
    assert kept[-1].text == f"entry {MAX_WIRE_ENTRIES + 699}"
    marker = next(e for e in kept if e.role == "system")
    assert omitted == len(entries) - (len(kept) - 1)
    assert marker.text == f"[{omitted} entries are not in this archive]"


def test_too_many_bytes_leave_whole_entries_out() -> None:
    each = 400_000
    entries = [entry(f"{n:04d}" + "q" * each) for n in range(40)]
    kept, omitted = bound_for_wire(entries)
    assert omitted > 0
    assert sum(len(e.model_dump_json()) for e in kept) <= MAX_WIRE_BYTES + 1_000
    # Each entry that stays is whole.
    assert all(len(e.text) == each + 4 for e in kept if e.role != "system")


async def test_the_remote_archiver_sends_the_omitted_count_as_it_is() -> None:
    """The runner cut is not lost: the control plane gets the number (``archive_truncated``)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"entries": 1})

    archiver = RemoteArchiver(
        "https://t.test/archive", "tok", transport=httpx.MockTransport(handler)
    )
    await archiver.archive(
        make_scope(), TranscriptArchive(trigger="auto", entries=[entry("a")], omitted=4)
    )
    assert json.loads(seen[0].content)["omitted"] == 4


def test_the_model_refuses_too_many_entries_and_large_values() -> None:
    body = {"trigger": "auto", "entries": [{"role": "user", "text": "a"}] * (MAX_WIRE_ENTRIES + 1)}
    with pytest.raises(ValueError, match="too many entries"):
        parse_archive(json.dumps(body).encode())
    with pytest.raises(ValueError, match="too large"):
        ArchiveEntry(role="assistant", tool_name="W", tool_input={"d": "x" * MAX_WIRE_ENTRY_BYTES})
    with pytest.raises(ValueError, match="too large"):
        ArchiveEntry(role="user", text="x" * (MAX_WIRE_ENTRY_BYTES + 1))


def many_entries(entry_json: bytes, count: int, *, entries_key: bytes = b'"entries"') -> bytes:
    return b'{"trigger":"auto",' + entries_key + b":[" + b",".join([entry_json] * count) + b"]}"


def test_a_body_of_many_tiny_entries_is_refused_before_pydantic_builds_them() -> None:
    """200,000 small entries in 5 MB took 400 MiB of memory with the old model. The count
    refuses the body, and it builds no entry."""
    raw = json.dumps({"trigger": "auto", "entries": [{"role": "user"}] * 200_000}).encode()
    assert len(raw) < 8_000_000
    with pytest.raises(ValueError, match="too many entries"):
        parse_archive(raw)
    # A real body is not refused, also when a tool input has a "role" key of its own.
    entries = [
        {"role": "assistant", "tool_name": "W", "tool_input": {"messages": [{"role": "user"}] * 50}}
    ]
    assert (
        len(parse_archive(json.dumps({"trigger": "auto", "entries": entries}).encode()).entries)
        == 1
    )


@pytest.mark.parametrize(
    ("entry_json", "entries_key"),
    [
        (b'{"\\u0072ole":"user"}', b'"entries"'),  # the key "role" in escapes
        (b'{"\\u0072\\u006fle":"user"}', b'"entries"'),
        (b'{"r\\u006fle":"user","text":""}', b'"entries"'),
        (b'{"role":"user"}', b'"\\u0065ntries"'),  # the key "entries" in escapes
        (b'{"\\u0072ole":"user"}', b'"\\u0065\\u006etries"'),
    ],
)
def test_the_entry_count_does_not_depend_on_how_the_body_spells_its_keys(
    entry_json: bytes, entries_key: bytes
) -> None:
    """The old check counted the bytes ``"role"``. JSON can write the key with escapes, so a
    body without those bytes got to the model and made 200,000 objects."""
    raw = many_entries(entry_json, 200_000, entries_key=entries_key)
    assert b'"role"' not in raw or entries_key != b'"entries"'
    tracemalloc.start()
    try:
        with pytest.raises(ValueError, match="too many entries"):
            parse_archive(raw)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # The model needs more than 100 MiB for 200,000 entries. The count needs a pointer each.
    assert peak < 30_000_000


def test_a_body_with_escaped_keys_in_the_limits_is_accepted() -> None:
    raw = many_entries(b'{"\\u0072ole":"user","t\\u0065xt":"hi"}', MAX_WIRE_ENTRIES)
    parsed = parse_archive(raw)
    assert len(parsed.entries) == MAX_WIRE_ENTRIES
    assert parsed.entries[0].text == "hi"
    with pytest.raises(ValueError, match="too many entries"):
        parse_archive(many_entries(b'{"\\u0072ole":"user"}', MAX_WIRE_ENTRIES + 1))


def test_a_body_that_is_nested_too_deeply_or_not_json_is_refused() -> None:
    with pytest.raises(ValueError, match="nested too deeply"):
        parse_archive(b"[" * 1_000_000)
    with pytest.raises(json.JSONDecodeError):
        parse_archive(b'{"trigger": "auto", "entries": [')
    with pytest.raises(ValidationError):
        parse_archive(b"[1, 2]")


# -- the size of an entry, in bytes of its JSON text -------------------------------------------


def test_the_size_of_an_entry_is_the_bytes_of_its_json_text() -> None:
    base = entry_wire_bytes(entry("a")) - 1  # the empty text is a default: it has no bytes
    assert entry_wire_bytes(entry("é")) == base + 2
    assert entry_wire_bytes(entry("\n")) == base + 2  # an escape takes two bytes
    # An entry at the limit is accepted, and one byte more is not.
    at_limit = entry("a" * (MAX_WIRE_ENTRY_BYTES - base))
    assert entry_wire_bytes(at_limit) == MAX_WIRE_ENTRY_BYTES
    assert bound_for_wire([at_limit]) == ([at_limit], 0)
    with pytest.raises(ValueError, match="too large"):
        entry("a" * (MAX_WIRE_ENTRY_BYTES - base + 1))


@pytest.mark.parametrize(
    "fields",
    [
        {"text": "é" * 600_000},  # 600,000 characters, 1.2 MB in UTF-8
        {"text": "\n" * 500_001},  # 500,001 characters, 1,000,002 bytes with escapes
        {"tool_name": "W", "tool_input": {"d": "é" * 600_000}},
        {"tool_name": "W", "tool_input": {"d": "\\" * 500_001}},
        # Each field is in a limit for characters, but together they are over the limit.
        {"text": "a" * 600_000, "tool_name": "W", "tool_input": {"d": "b" * 600_000}},
        {"text": "é" * 300_000, "tool_name": "W", "tool_input": {"d": "é" * 300_000}},
    ],
)
def test_the_model_counts_the_bytes_of_the_whole_entry(fields: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="too large"):
        ArchiveEntry(role="assistant", **fields)  # type: ignore[arg-type]
    # The body is refused with the same rule.
    raw = json.dumps(
        {"trigger": "auto", "entries": [{"role": "assistant", **fields}]}, ensure_ascii=False
    ).encode()
    assert len(raw) < MAX_WIRE_BYTES * 2
    with pytest.raises(ValueError, match="too large"):
        parse_archive(raw)


def test_the_model_refuses_a_tool_name_that_is_too_long() -> None:
    with pytest.raises(ValueError, match="at most"):
        ArchiveEntry(role="assistant", tool_name="n" * (MAX_TOOL_NAME_CHARS + 1))
    assert ArchiveEntry(role="assistant", tool_name="n" * MAX_TOOL_NAME_CHARS)


def test_a_cli_session_exists_only_with_its_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "projects" / "-workspace"
    project.mkdir(parents=True)
    (project / "abc-123.jsonl").write_text("{}\n")
    assert cli_session_exists("abc-123", str(tmp_path))
    assert not cli_session_exists("other", str(tmp_path))
    assert not cli_session_exists("*", str(tmp_path)), "a glob is not a session"
    assert not cli_session_exists("../projects/-workspace/abc-123", str(tmp_path))
    assert not cli_session_exists("", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert cli_session_exists("abc-123")
