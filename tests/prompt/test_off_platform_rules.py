"""The M8.4 rules in the M8.5 system text: finish the task in sama, get live values, load a
fitting skill, and save "learn this" in a sama store (docs/architecture.md, Sections 6.1 and
18.4). The off-platform eval measures them; this test keeps the text from losing them."""

from __future__ import annotations

from pathlib import Path

import pytest

from sama.instructions import SYSTEM_PROMPT
from sama.skills.builtin import BuiltinSkills

SKILLS = Path(__file__).resolve().parents[2] / "skills"


@pytest.mark.parametrize(
    "rule",
    [
        # Finish the task in sama.
        "Own the outcome",
        "do not give the user a list of things to try that you could try yourself",
        # Live values: get them now, and never send the user off to look them up.
        "Live values (prices, fares, rates, availability, opening hours): get the real value now",
        "Try the official site first, then other sites",
        "Do not send the user off to look up a value that your tools can read",
        "a link to your source is good, a task for the user is not",
        # Load a fitting skill.
        "load it with skill_view before you start",
        # "Learn this" goes to a sama store, not to a sandbox file.
        "save it with the memory tool, not in a file",
        "save it with skill_manage",
    ],
)
def test_the_system_text_keeps_the_off_platform_rule(rule: str) -> None:
    assert " ".join(rule.split()) in " ".join(SYSTEM_PROMPT.split())


def test_the_price_check_skill_is_a_builtin_skill() -> None:
    skill = BuiltinSkills(SKILLS).all()["price-check"]
    assert skill.description == "Find the live price of a room, a fare, or a product"
    assert "Never send the user to check a website" in skill.skill_md
