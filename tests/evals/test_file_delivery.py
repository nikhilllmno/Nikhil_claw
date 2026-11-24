"""The file-delivery eval (docs/architecture.md, Section 11.10; roadmap M8.6)."""

# ruff: noqa: S108 - the /tmp paths are paths of the fake sandbox, not of this host.

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from sama.evals.file_delivery import (
    CASES,
    Api,
    Fetched,
    Result,
    check,
    classify,
    load,
    relative_file_links,
    run_case,
    same_origin,
    sandbox_paths,
    summary,
)

# Answers of the baseline on main, 2026-11-24 (shortened).
CLAUDE_PATH = "I made the file at `/home/user/work/top5_countries_by_population.csv`:\n\n```csv"
OPENAI_SANDBOX_LINK = (
    "Your checklist is ready: [Download the PDF](sandbox:/home/user/work/goa.pdf)."
)
OPENAI_LINE_LINK = "Created [`naming.md`](naming.md:1) with a summary of the naming rules."
OPENAI_RELATIVE = "[Download the PNG chart](mumbai_monthly_rainfall.png)"
GOOD = "I made **top5.csv** with the 5 most populous countries (UN 2024 estimates)."

CSV = b"rank,country\n1,India\n"


def ok_file(source: str = "media:1") -> Fetched:
    return Fetched(source, 200, "text/csv", len(CSV), CSV[:8].hex(), ok=True)


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (CLAUDE_PATH, "path_only"),
        (OPENAI_SANDBOX_LINK, "path_only"),
        (OPENAI_LINE_LINK, "dead_link"),
        (OPENAI_RELATIVE, "dead_link"),
        (GOOD, "no_file"),
        ("Source: [PEP 8](https://peps.python.org/pep-0008/)", "no_file"),
    ],
)
def test_an_answer_without_a_delivered_file(answer: str, outcome: str) -> None:
    assert classify(answer, []) == outcome


def test_a_delivered_file_wins_over_a_path_in_the_answer() -> None:
    assert classify(CLAUDE_PATH, [ok_file()]) == "delivered"
    assert classify(GOOD, [ok_file()]) == "delivered"


def test_a_file_that_does_not_open_is_a_dead_link() -> None:
    broken = Fetched("https://sama.test/files/a.csv", 404, "text/html", 9, "", ok=False)
    assert classify(GOOD, [broken]) == "dead_link"
    page = Fetched("https://peps.python.org/", 200, "text/html", 9, "", ok=False, file_link=False)
    assert classify(GOOD, [page]) == "no_file"
    assert classify("", [], failed=True, answered=False) == "error"
    assert classify(CLAUDE_PATH, [], failed=True) == "path_only"


def test_paths_and_relative_links() -> None:
    assert sandbox_paths(CLAUDE_PATH) == ["/home/user/work/top5_countries_by_population.csv"]
    assert sandbox_paths("see /tmp/chart.png.") == ["/tmp/chart.png"]
    assert sandbox_paths("https://example.com/home/user/x") == []
    assert relative_file_links(OPENAI_LINE_LINK) == ["naming.md:1"]
    assert relative_file_links("[docs](https://a.test/x.pdf) [top](#top) [m](mailto:a@b.c)") == []
    assert relative_file_links(OPENAI_SANDBOX_LINK) == []


@pytest.mark.parametrize(
    ("kind", "status", "mime", "body", "ok"),
    [
        ("csv", 200, "text/csv; charset=utf-8", CSV, True),
        ("csv", 200, "text/csv", b"", False),
        ("csv", 404, "text/csv", CSV, False),
        ("csv", 200, "text/html", CSV, False),
        ("pdf", 200, "application/pdf", b"%PDF-1.7 ...", True),
        ("pdf", 200, "application/pdf", b"<html>", False),
        ("image", 200, "image/png", b"\x89PNG\r\n\x1a\n...", True),
        ("zip", 200, "application/zip", b"PK\x03\x04...", True),
        ("markdown", 200, "text/markdown", b"# Naming\n", True),
    ],
)
def test_check_needs_200_the_type_bytes_and_the_start(
    kind: str, status: int, mime: str, body: bytes, ok: bool
) -> None:
    assert check(kind, status, mime, body) is ok


def test_the_cases_cover_each_kind_and_a_second_turn() -> None:
    assert len({case.id for case in CASES}) == len(CASES) == 6
    assert {case.kind for case in CASES} == {"csv", "markdown", "pdf", "image", "zip"}
    assert [case.id for case in CASES if case.again] == ["resend-csv"]


def test_the_summary_counts_the_bar_failures(tmp_path: Path) -> None:
    stored = [
        Result("a", "csv", "claude", answer=CLAUDE_PATH, answers=[CLAUDE_PATH], cost_usd=0.1),
        Result(
            "b",
            "csv",
            "claude",
            answer=GOOD,
            answers=[GOOD],
            fetched=[dataclasses.asdict(ok_file())],
        ),
        Result("a", "csv", "openai", answer=OPENAI_RELATIVE, answers=[OPENAI_RELATIVE]),
    ]
    path = tmp_path / "results.jsonl"
    path.write_text("".join(json.dumps(dataclasses.asdict(r)) + "\n" for r in stored))
    results = load(path)
    assert [(r.case, r.runtime, r.outcome) for r in results] == [
        ("a", "claude", "path_only"),
        ("b", "claude", "delivered"),
        ("a", "openai", "dead_link"),
    ]
    table = summary(results)
    assert "| claude | 2 | 1 | 0 | 1 | 0 | 0 | 1 | 0.10 |" in table
    assert "| openai | 1 | 0 | 1 | 0 | 0 | 0 | 1 | 0.00 |" in table


class FakeSama:
    """A sama API with one session: each message makes one completed run, and the run of the
    second message stores a CSV file."""

    def __init__(self, first: str = CLAUDE_PATH, second: str | None = GOOD) -> None:
        self.first, self.second = first, second
        self.runs: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.media: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "POST" and path == "/sessions":
            return httpx.Response(201, json={"id": "s1"})
        if method == "POST" and path == "/sessions/s1/messages":
            run_id = f"r{len(self.runs) + 1}"
            self.runs.append(
                {
                    "id": run_id,
                    "status": "completed",
                    "created_at": f"2026-11-24T08:0{len(self.runs)}:00Z",
                    "cost_usd": "0.01",
                }
            )
            text = json.loads(request.content)["text"]
            self.messages.append({"role": "user", "text": text})
            if len(self.runs) == 1:
                self.messages.append({"role": "assistant", "text": self.first, "run_id": run_id})
            elif self.second is not None:
                self.media.append({"id": "m1", "run_id": run_id})
                self.messages.append({"role": "assistant", "text": self.second, "run_id": run_id})
            return httpx.Response(202, json={})
        if path == "/sessions/s1/runs":
            return httpx.Response(200, json=self.runs)
        if path in {"/sessions/s1/approvals", "/sessions/s1/questions"}:
            return httpx.Response(200, json=[])
        if path == "/sessions/s1/messages":
            return httpx.Response(200, json={"items": self.messages})
        if path.startswith("/runs/") and path.endswith("/trace"):
            return httpx.Response(200, json={"tool_calls": [{"tool_name": "file_send"}]})
        if path == "/sessions/s1/media":
            return httpx.Response(200, json=self.media)
        if path == "/files/old.csv":
            return httpx.Response(200, content=CSV, headers={"content-type": "text/csv"})
        if path == "/sessions/s1/media/m1":
            assert request.headers["authorization"] == "Bearer t"
            return httpx.Response(200, content=CSV, headers={"content-type": "text/csv"})
        return httpx.Response(404)


async def test_a_resend_case_checks_only_the_second_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    token = tmp_path / "token"
    token.write_text("t")
    api = Api("https://sama.test", token)
    fake = FakeSama()
    api._client = httpx.AsyncClient(
        base_url="https://sama.test", transport=httpx.MockTransport(fake)
    )
    case = next(case for case in CASES if case.id == "resend-csv")
    result = await run_case(api, case, "claude", workspace_id="w", profile_id="p", timeout_s=5)
    await api.aclose()
    assert result.outcome == "delivered", result
    assert result.answer == GOOD
    assert [f["source"] for f in result.fetched] == ["media:m1"]
    assert result.tools == ["file_send", "file_send"]
    assert result.cost_usd == pytest.approx(0.02)


async def _no_sleep(_: float) -> None:
    return None


async def test_a_link_of_the_first_turn_does_not_count_for_the_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    token = tmp_path / "token"
    token.write_text("t")
    api = Api("https://sama.test", token)
    fake = FakeSama(first="Here: https://sama.test/files/old.csv", second=None)
    api._client = httpx.AsyncClient(
        base_url="https://sama.test", transport=httpx.MockTransport(fake)
    )
    case = next(case for case in CASES if case.id == "resend-csv")
    result = await run_case(api, case, "claude", workspace_id="w", profile_id="p", timeout_s=5)
    await api.aclose()
    assert result.answer == ""
    assert result.fetched == []
    assert result.outcome == "error"


@pytest.mark.parametrize(
    ("url", "same"),
    [
        ("https://sama.test/sessions/s1/media/m1", True),
        ("https://sama.test:443/x.csv", True),
        ("http://sama.test/x.csv", False),
        ("https://sama.test:8443/x.csv", False),
        ("https://evil.test/x.csv", False),
    ],
)
def test_the_token_goes_only_to_the_origin_of_the_api(url: str, same: bool) -> None:
    assert same_origin(url, "https://sama.test") is same
