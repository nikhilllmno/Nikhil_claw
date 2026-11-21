"""The ``/plan`` command of the chat channels (Section 15.5)."""

from __future__ import annotations

import pytest

from sama.coding_mode.commands import plan_action, plan_reply
from sama.coding_mode.harness import CLAUDE_CODE, CODEX, plan_mode_for
from sama.coding_mode.service import CodingService
from tests.orchestrator.conftest import Tenant


@pytest.mark.parametrize(
    ("rest", "expected"),
    [
        ("", ("show", "")),
        ("  ", ("show", "")),
        ("on", ("on", "")),
        ("OFF", ("off", "")),
        ("approve", ("approve", "")),
        ("approve the fix", ("task", "approve the fix")),
        ("edit 1. a\n2. b", ("edit", "1. a\n2. b")),
        ("edit", ("task", "edit")),
        ("reject", ("reject", "")),
        ("reject add tests", ("reject", "add tests")),
        ("fix the login bug", ("task", "fix the login bug")),
    ],
)
def test_plan_action(rest: str, expected: tuple[str, str]) -> None:
    assert plan_action(rest) == expected


async def test_plan_reply_flow(coding: CodingService, tenant: Tenant) -> None:
    org, session, mia = tenant.org, tenant.ids["mine"], tenant.ids["mia"]

    async def reply(rest: str, prefix: str = "/") -> tuple[str, bool]:
        answer = await plan_reply(
            coding, org_id=org, user_id=mia, session_id=session, rest=rest, prefix=prefix
        )
        return answer.text, answer.run_id is not None

    assert (await reply(""))[0].startswith("Plan mode is off.")
    assert await reply("approve") == (
        "This session has no open plan. Send /plan TASK to start one.",
        False,
    )
    text, run = await reply("add a health route")
    assert run
    assert "Plan mode is on" in text
    await coding.propose(org_id=org, session_id=session, run_id=None, body="1. add route")
    shown, _ = await reply("", prefix="/sama ")
    assert "1. add route" in shown
    assert "/sama plan approve" in shown
    text, run = await reply("edit 1. add route\n2. add test")
    assert run
    assert "approved" in text
    plans = await coding.plans(org_id=org, session_id=session)
    assert plans[0].edited
    assert plans[0].body.endswith("2. add test")
    await coding.propose(org_id=org, session_id=session, run_id=None, body="1. again")
    text, run = await reply("reject")
    assert not run
    assert "rejected" in text
    await coding.propose(org_id=org, session_id=session, run_id=None, body="1. again")
    text, run = await reply("reject too big")
    assert run
    assert (await reply("off"))[0] == "Plan mode is off."
    assert "Plan mode is on" in (await reply("on"))[0]


async def test_plan_reply_without_a_service_or_a_session(tenant: Tenant) -> None:
    missing = await plan_reply(None, org_id=tenant.org, user_id=None, session_id=None, rest="x")
    assert missing.text == "Coding mode is not available here."


def test_the_harness_plan_modes() -> None:
    assert plan_mode_for(CLAUDE_CODE).cli_args == ("--permission-mode", "plan")
    assert plan_mode_for(CLAUDE_CODE).native_plan_tool == "ExitPlanMode"
    assert plan_mode_for(CODEX).cli_args == ()
    assert plan_mode_for("other").cli_args == ()
    assert "whole answer" in plan_mode_for("other").prompt


def test_the_plan_note_follows_the_harness() -> None:
    from sama.coding_mode.prepare import PLAN_MODE_NOTE, plan_note

    assert plan_note("claude_agent_sdk") == PLAN_MODE_NOTE
    assert plan_note("claude_code").startswith("[sama coding mode] Plan mode is on.")
    assert plan_note("codex") != PLAN_MODE_NOTE
