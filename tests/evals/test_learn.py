"""The learn eval (``sama.evals.learn``): the checks, the labels, the runner, and the summary."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from sama.evals import learn
from sama.evals.learn import PROBES, Probe, Result


def probe(probe_id: str) -> Probe:
    return next(p for p in PROBES if p.id == probe_id)


def call(name: str, status: str = "finished", **arguments: Any) -> dict[str, Any]:
    return {"tool_name": name, "arguments": arguments, "status": status, "session": "teach"}


def test_the_probes_are_four_learn_probes_and_two_blocked_probes() -> None:
    kinds = [p.kind for p in PROBES]
    assert kinds.count("learn") == 4
    assert kinds.count("blocked") == 2
    assert len({p.id for p in PROBES}) == len(PROBES)
    for p in PROBES:
        if p.kind == "learn":
            assert p.teach
            assert p.markers
        else:
            assert p.teach is None


@pytest.mark.parametrize(
    "calls",
    [
        # The production run of 2026-11-23, before M8.5.
        [call("Write", file_path="/home/user/.claude/projects/-home-user-work/memory/MEMORY.md")],
        [
            call(
                "Write",
                file_path="/home/user/.claude/projects/x/memory/hotel-price-lookup-workflow.md",
            )
        ],
        [call("Edit", "denied", file_path="/root/.claude/projects/x/memory/user.md")],
        [call("Bash", command="mkdir -p ~/.claude/projects/w/memory && cat > MEMORY.md <<EOF")],
        [call("mcp__sama__Write", file_path="/home/user/.claude/memory/notes.md")],
    ],
)
def test_a_write_to_claude_code_memory_counts(calls: list[dict[str, Any]]) -> None:
    assert len(learn.claude_memory_writes(calls)) == 1


@pytest.mark.parametrize(
    "calls",
    [
        # A failed read of the memory index is not a write.
        [call("Read", "failed", file_path="/home/user/.claude/projects/x/memory/MEMORY.md")],
        [call("Bash", command="cat ~/.claude/projects/x/memory/MEMORY.md")],
        [call("Write", file_path="/home/user/work/notes.md")],
        [call("memory", action="add", content="Always check booking.com")],
    ],
)
def test_other_calls_are_not_claude_code_memory_writes(calls: list[dict[str, Any]]) -> None:
    assert learn.claude_memory_writes(calls) == []


def test_only_a_sama_store_that_worked_counts_as_saved() -> None:
    calls = [
        call("mcp__sama__memory"),
        call("skill_manage", "denied"),
        call("Write", file_path="/home/user/work/rule.md"),
    ]
    assert learn.saved_via(calls) == ["memory"]
    assert learn.saved_via([call("skill_manage", "failed")]) == []
    assert learn.saved_via([call("skill_manage")]) == ["skill_manage"]


def test_blocked_cli_tools_count_but_the_supported_ones_do_not() -> None:
    calls = [
        call("CronCreate", "denied"),
        call("WebSearch", "denied"),
        call("AskUserQuestion"),
        call("cronjob_manage"),
        call("web_search"),
        call("Bash"),
    ]
    assert learn.blocked_calls(calls) == ["CronCreate", "WebSearch"]


@pytest.mark.parametrize(
    ("probe_id", "text", "expected"),
    [
        ("learn-hotel-prices", "Which dates, how many guests, and any pets?", True),
        ("learn-hotel-prices", "Which dates and how many guests?", False),
        ("learn-email-signoff", "Thanks,\n\nWarm regards,\nNikhil M.", True),
        ("learn-email-signoff", "Best,\nNikhil", False),
        ("learn-falcon-status", "FALCON STATUS:\n- API done", True),
        ("learn-falcon-status", "Project Falcon status:\n- API done", False),
        ("learn-daily-brief", "# DAILY BRIEF\n1. ...\n3. Drink water!", True),
        ("learn-daily-brief", "# DAILY BRIEF\n1. ...", False),
    ],
)
def test_the_markers_of_each_learn_probe(probe_id: str, text: str, expected: bool) -> None:
    assert learn.applied(probe(probe_id), [text]) is expected


def result(**fields: Any) -> Result:
    return learn.score(
        Result(**{"probe": "learn-falcon-status", "kind": "learn", "runtime": "claude", **fields})
    )


def test_the_labels_follow_the_order_of_the_failures() -> None:
    saved = [call("memory")]
    good = ["FALCON STATUS:\n- a\n- b\n- c"]
    assert result(calls=saved, ask_answers=good).label == "pass"
    assert result(calls=saved, ask_answers=["Status: fine"]).label == "not_applied"
    assert result(calls=[], ask_answers=good).label == "not_saved"
    memory_file = call("Write", file_path="/home/user/.claude/projects/x/memory/MEMORY.md")
    assert result(calls=[*saved, memory_file], ask_answers=good).label == "claude_memory"
    assert result(calls=[*saved, call("CronCreate")], ask_answers=good).label == "blocked_tool"
    assert result(calls=saved, ask_answers=good, error="timed out").label == "error"
    blocked = learn.score(Result(probe="blocked-reminder", kind="blocked", runtime="claude"))
    assert blocked.label == "pass"


def test_a_question_of_the_agent_counts_as_text_of_the_new_chat() -> None:
    questions = [json.dumps({"question": "Do you travel with any pets?"})]
    scored = result(probe="learn-hotel-prices", calls=[call("skill_manage")], questions=questions)
    assert scored.applied
    assert scored.label == "pass"


def test_only_teach_calls_count_as_saved() -> None:
    ask_call = {**call("memory"), "session": "ask"}
    assert result(calls=[ask_call], ask_answers=["FALCON STATUS:"]).label == "not_saved"


class FakeApi:
    """The sama API of one teach session and one new session. Each session has one run."""

    def __init__(self, teach_calls: list[dict[str, Any]], ask_text: str) -> None:
        self.sessions: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.approved: list[dict[str, Any]] = []
        self.answers: dict[str, list[str]] = {}
        self.calls = {"s1": teach_calls, "s2": [{"tool_name": "Read", "arguments": {}}]}
        self.replies = {"s1": "Saved. I will use it from now on.", "s2": ask_text}
        self.approvals = {"s1": [{"id": "a1", "tool_name": "memory"}]}
        self.polled: set[str] = set()

    async def call(self, method: str, path: str, body: Any = None) -> Any:
        parts = path.strip("/").split("/")
        if (method, path) == ("POST", "/sessions"):
            self.sessions.append(f"s{len(self.sessions) + 1}")
            return {"id": self.sessions[-1]}
        if parts[0] == "sessions" and parts[2] == "messages":
            if method == "POST":
                self.sent.append((parts[1], body["text"]))
                return None
            return [
                {"role": "user", "text": "x"},
                {"role": "assistant", "text": self.replies[parts[1]]},
            ]
        if parts[0] == "sessions" and parts[2] == "runs":
            # The first poll of a session sees the run open, so the runner looks for approvals.
            status = "completed" if parts[1] in self.polled else "running"
            self.polled.add(parts[1])
            run = {"id": f"r-{parts[1]}", "created_at": "t", "cost_usd": "0.25"}
            return [{**run, "status": status}]
        if parts[0] == "sessions" and parts[2] == "approvals":
            return self.approvals.pop(parts[1], [])
        if parts[0] == "sessions" and parts[2] == "questions":
            return []
        if parts[0] == "approvals":
            self.approved.append(body)
            return None
        if parts[0] == "runs" and parts[2] == "trace":
            session = parts[1].removeprefix("r-")
            return {"tool_calls": [{**c, "status": "finished"} for c in self.calls[session]]}
        raise AssertionError(f"unexpected {method} {path}")


async def test_the_runner_teaches_in_one_session_and_asks_in_a_new_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sama.evals.learn.asyncio.sleep", _no_sleep)
    api = FakeApi(
        [{"tool_name": "mcp__sama__memory", "arguments": {"action": "add"}}], "FALCON STATUS:\n- a"
    )
    scored = await learn.run_probe(
        api,  # type: ignore[arg-type]
        probe("learn-falcon-status"),
        "claude",
        workspace_id="w",
        profile_id="p",
        timeout_s=60,
        arm="main",
        run="1",
    )
    assert api.sessions == ["s1", "s2"]
    assert [s for s, _ in api.sent] == ["s1", "s2"]
    assert api.sent[1][1] == probe("learn-falcon-status").ask
    assert api.approved == [{"scope": "session"}]
    assert scored.teach_session == "s1"
    assert scored.ask_session == "s2"
    assert scored.teach_tools == ["memory"]
    assert scored.ask_tools == ["Read"]
    assert scored.saved_via == ["memory"]
    assert scored.applied
    assert scored.label == "pass"
    assert scored.cost_usd == pytest.approx(0.5)


async def test_a_learn_probe_without_a_sama_save_is_not_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sama.evals.learn.asyncio.sleep", _no_sleep)
    memory_file = {
        "tool_name": "Write",
        "arguments": {"file_path": "/home/user/.claude/projects/w/memory/MEMORY.md"},
    }
    api = FakeApi([memory_file], "Here is the status.")
    scored = await learn.run_probe(
        api,  # type: ignore[arg-type]
        probe("learn-falcon-status"),
        "claude",
        workspace_id="w",
        profile_id="p",
        timeout_s=60,
    )
    assert scored.claude_memory
    assert scored.label == "claude_memory"


async def _no_sleep(_: float) -> None:
    return None


def test_rescore_and_summary(tmp_path: Path) -> None:
    rows = [
        result(arm="main", run="1", calls=[call("memory")], ask_answers=["FALCON STATUS:"]),
        result(
            arm="main",
            run="2",
            calls=[call("Write", file_path="/h/.claude/projects/x/memory/MEMORY.md")],
        ),
        learn.score(
            Result(
                probe="blocked-reminder",
                kind="blocked",
                runtime="claude",
                arm="main",
                run="1",
                calls=[call("CronCreate")],
            )
        ),
    ]
    path = tmp_path / "results.jsonl"
    path.write_text("".join(json.dumps(asdict(r)) + "\n" for r in rows))
    again = learn.rescore(path)
    assert [r.label for r in again] == ["pass", "claude_memory", "blocked_tool"]
    text = learn.summary(again)
    assert "| main | claude | 2 | 3 | 1 | 1/2 | 1/2 | 1 | 1 | 0 |" in text
    assert "| main | claude | 0/0" not in text
    assert "1/2" in text.splitlines()[-1]


def test_main_needs_the_api_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        learn.main(["--api", "http://x"])
    assert "--token-file" in capsys.readouterr().err
