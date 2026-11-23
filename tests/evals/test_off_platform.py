"""The classifier of the off-platform eval (docs/architecture.md, Section 18.4)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from sama.evals.off_platform import (
    CASES,
    Result,
    asks,
    classify,
    deflections,
    has_price,
    rescore,
    summary,
)

# The two answers of the production session of 2026-11-23 that started this eval (shortened).
WYNDHAM_TURN_1 = """\
I couldn't get exact rates for Nov 23-28. The pages I could read don't show prices for
specific dates.

| Hotel | Recent low rate |
|---|---|
| Reside Navy Yard, a Wyndham Residence | about $124-164 |

**To get a real quote:**
- Enter your dates on wyndhamhotels.com or Kayak.
- Or tell me which hotel and the exact check-in and check-out dates.
"""

WYNDHAM_TURN_2 = """\
I couldn't get a price for Reside Navy Yard for Nov 23-25.

For a reliable same-day quote, you can:
- **Call the hotel:** (908) 223-8082.
- **Check the official site:** wyndhamhotels.com, and look for the Reside Navy Yard page.
- **Let me try again:** I can try the Wyndham site directly.

Want me to try the Wyndham site next?
"""

GOOD_ANSWER = """\
Reside Navy Yard, Dec 4-6, 2 adults, on wyndhamhotels.com: Studio King $189 a night,
$441.20 in total with taxes. Booking.com shows the same room at $185.

Sources: [Wyndham](https://www.wyndhamhotels.com/...), [Booking.com](https://www.booking.com/...)
"""


@pytest.mark.parametrize(
    "text",
    [
        WYNDHAM_TURN_1,
        WYNDHAM_TURN_2,
        "You can check the latest price on amazon.com.",
        "I recommend checking the hotel's website for the exact rate.",
        "I don't have access to real-time prices.",
        "I can't browse live flight fares.",
        "Please visit Google Flights for current fares.",
        "1. Go to booking.com and search for the hotel.",
        "You'll need to contact the hotel directly for this date.",
        "You\u2019ll need to contact the hotel directly for this date.",
        "For an exact quote, check the hotel's own site, tajhotels.com, with your dates.",
        "Prices vary. Or call the hotel at +1 202 555 0100.",
        "To see the exact price, open this link and check the dates:\nhttps://www.hyatt.com/x",
        "I haven't seen the live Amazon price today. Check the Amazon page before you buy.",
        "I can't open it. Check Amazon, Flipkart and Croma directly before buying.",
    ],
)
def test_an_answer_that_sends_the_user_away_is_off_platform(text: str) -> None:
    assert deflections(text)
    assert classify([text]) == "off_platform"


@pytest.mark.parametrize(
    "text",
    [
        GOOD_ANSWER,
        "Do you want me to try the Wyndham site next?",
        "I checked booking.com and expedia.com. The cheapest room is $212.",
        "Prices change often; this is the price at 10:40 UTC. You can book it here: [link](x).",
        "Which dates do you want, and how many guests?",
        "Let me try again: I can try the Wyndham site directly.",
        "- Check-in: Dec 4 from 3 pm at the hotel's front desk ($50 deposit).",
        "I can open the link in the browser and read the price for you.",
        "If you'd like, I can try its search page. I could also try Goibibo or the airline sites.",
        "I can retry Best Buy's page later, or check Amazon's listing directly, if you want.",
        "- **Search link:** https://www.airbnb.com/s/Lisbon--Portugal/homes?adults=2",
    ],
)
def test_a_price_a_source_or_an_offer_is_not_off_platform(text: str) -> None:
    assert deflections(text) == []


def test_the_outcome_uses_every_answer_and_the_price_of_the_last() -> None:
    question = "Which hotel and which dates?"
    assert classify([question, GOOD_ANSWER]) == "answered"
    assert classify([question, "I found no rooms for those dates."]) == "no_price"
    assert classify([WYNDHAM_TURN_1, GOOD_ANSWER]) == "off_platform"
    assert classify([], failed=True) == "error"
    assert classify([GOOD_ANSWER], failed=True) == "error"
    assert classify([WYNDHAM_TURN_2], failed=True) == "off_platform"


@pytest.mark.parametrize(
    ("text", "price"),
    [
        ("$129 a night", True),
        ("INR 4,500", True),
        ("₹ 89,900", True),
        ("about 120 EUR", True),
        ("2 adults, 3 nights", False),
        ("Nov 23-28", False),
    ],
)
def test_a_price_needs_a_currency(text: str, price: bool) -> None:
    assert has_price(text) is price


def test_the_cases_have_unique_ids_and_cover_each_kind() -> None:
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids)) == 18
    assert {case.kind for case in CASES} == {"hotel", "flight", "product", "other"}


def test_the_summary_counts_each_outcome_per_runtime() -> None:
    results = [
        Result("a", "hotel", "claude", outcome="off_platform", cost_usd=0.5),
        Result("b", "hotel", "claude", outcome="answered", tools=["browser_open"], cost_usd=0.25),
        Result("a", "hotel", "openai", outcome="error"),
    ]
    table = summary(results)
    assert "| claude | 2 | 1 | 1 | 0 | 0 | 1 | 0.75 |" in table
    assert "| openai | 1 | 0 | 0 | 0 | 1 | 0 | 0.00 |" in table


def test_rescore_classifies_stored_answers_again(tmp_path: Path) -> None:
    stored = [
        Result("a", "hotel", "claude", answers=[WYNDHAM_TURN_1], outcome="answered"),
        Result("b", "hotel", "claude", answers=[GOOD_ANSWER], outcome="off_platform"),
        Result("c", "hotel", "claude", error="sandbox_error: 429"),
    ]
    path = tmp_path / "results.jsonl"
    path.write_text("".join(json.dumps(dataclasses.asdict(r)) + "\n" for r in stored))
    outcomes = [(r.case, r.outcome) for r in rescore(path)]
    assert outcomes == [("a", "off_platform"), ("b", "answered"), ("c", "error")]


def test_a_question_without_a_price_gets_the_follow_up() -> None:
    assert asks("Which price do you want?\n\n1. The stock\n2. A room\n\nIf the stock, I look now.")
    assert not asks(GOOD_ANSWER + "\nDo you want other dates?")
    assert not asks("I found no rooms for those dates.")


class FakeGuard:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def wait_for_room(self) -> None:
        self.calls.append("wait")

    async def release(self, session_id: str) -> None:
        self.calls.append(f"release {session_id}")


async def test_the_runner_waits_for_room_releases_each_session_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    from sama.evals import off_platform

    async def fake_case(api: object, case: object, runtime: str, **_: object) -> Result:
        name = case.id  # type: ignore[attr-defined]
        return Result(name, "hotel", runtime, session_id=f"s-{name}", answers=[GOOD_ANSWER])

    monkeypatch.setattr(off_platform, "run_case", fake_case)
    done = Result("wyndham-vague", "hotel", "claude", answers=[WYNDHAM_TURN_1])
    (tmp_path / "results.jsonl").write_text(json.dumps(dataclasses.asdict(done)) + "\n")
    token = tmp_path / "token"
    token.write_text("t")
    args = argparse.Namespace(
        profile=["claude=p1"],
        case=["wyndham-vague", "hyatt-sf"],
        api="https://sama.test",
        token_file=str(token),
        out=str(tmp_path),
        resume=True,
        learn=False,
        concurrency=2,
        workspace="w1",
        timeout=1.0,
    )
    guard = FakeGuard()
    results = await off_platform.run(args, guard)
    assert guard.calls == ["wait", "release s-hyatt-sf"]
    assert sorted((r.case, r.outcome) for r in results) == [
        ("hyatt-sf", "answered"),
        ("wyndham-vague", "off_platform"),
    ]
    assert "| claude | 2 | 1 | 1 | 0 | 0 |" in (tmp_path / "summary.md").read_text()


def test_a_learn_case_is_saved_only_through_a_sama_store() -> None:
    from sama.evals.off_platform import LEARN_CASES, learn_summary, learned

    assert "can you learn this" in LEARN_CASES[0].text
    saved = Result("l", "learn", "claude", answers=["Saved."], tools=["skill_manage"])
    file_only = Result(
        "l",
        "learn",
        "openai",
        answers=["Saved."],
        tools=["Read", "Write"],
        writes=["/home/user/.claude/projects/-home-user-work/memory/hotel.md"],
    )
    nothing = Result("l", "learn", "openai", answers=["OK, I will."])
    assert [learned(r) for r in (saved, file_only, nothing)] == ["saved", "file_only", "not_saved"]
    for result in (saved, file_only, nothing):
        result.learned = learned(result)
    table = learn_summary([saved, file_only, nothing])
    assert "| claude | 1 | 1 | 0 | 0 | 0 |" in table
    assert "| openai | 2 | 0 | 1 | 1 | 0 |" in table
    assert summary([saved]) == learn_summary([saved])
