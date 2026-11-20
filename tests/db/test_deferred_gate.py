"""A deferred call goes through the same tool gate as a direct call (Sections 10.3, 16.1).

The fixtures and helpers are those of the gate tests with Postgres.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AgentProfile, Run
from sama.gate import GateStep
from sama.gate.service import GateService
from sama.tools.deferred import DeferredCatalog, DeferredTool
from tests.db.test_tool_gate import (
    Org1,
    add_rule,
    approval,
    audit,
    call_row,
)
from tests.db.test_tool_gate import org as gate_org

org = gate_org
"""The fixture of the gate tests: an org with a profile, a shared session, and runs."""

SEND = {"to": "a@b.c"}


async def nothing(claims: Any, arguments: Any) -> str:
    return "done"


def catalog() -> DeferredCatalog:
    return DeferredCatalog(
        [
            DeferredTool("mail_send", "Send mail.", "mail", nothing),
            DeferredTool("mail_read", "Read mail.", "mail", nothing, read_only=True),
            DeferredTool("clock", "Show the time.", "clock", nothing),
        ]
    )


def wrapped(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


async def grant(engine: AsyncEngine, org: Org1, *toolsets: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == org.ids["profile"])
            .values(toolsets=["todo", *toolsets], group_toolsets=["todo", *toolsets])
        )


def gate(engine: AsyncEngine) -> GateService:
    return GateService(engine, extra_toolsets=catalog())


async def test_a_deferred_call_asks_like_a_direct_call(engine: AsyncEngine, org: Org1) -> None:
    await grant(engine, org, "tool_search", "mail")
    service = gate(engine)
    verdict = await service.check(
        org.claims(), "c1", "mcp__sama__tool_call", wrapped("mail_send", **SEND)
    )
    assert (verdict.action, verdict.step) == ("ask", GateStep.POLICY)
    # The card shows the tool that the call runs, not the wrapper.
    row = await approval(engine, org, "c1")
    assert row.tool_name == "mail_send"
    assert row.arguments == SEND
    call = await call_row(engine, org, "c1")
    assert call.tool_name == "mail_send"


async def test_a_deferred_call_needs_the_toolset_of_the_tool(
    engine: AsyncEngine, org: Org1
) -> None:
    await grant(engine, org, "tool_search", "clock")
    verdict = await gate(engine).check(
        org.claims(), "c1", "mcp__sama__tool_call", wrapped("mail_send", **SEND)
    )
    assert (verdict.action, verdict.step) == ("deny", GateStep.TOOLSET)
    assert await approval(engine, org, "c1") is None


async def test_the_search_toolset_alone_is_not_enough_for_tool_call(
    engine: AsyncEngine, org: Org1
) -> None:
    await grant(engine, org, "mail")
    verdict = await gate(engine).check(
        org.claims(), "c1", "mcp__sama__tool_call", wrapped("mail_read")
    )
    assert (verdict.action, verdict.step) == ("deny", GateStep.TOOLSET)


async def test_without_a_catalog_tool_call_reaches_no_tool(engine: AsyncEngine, org: Org1) -> None:
    await grant(engine, org, "tool_search", "mail")
    verdict = await GateService(engine).check(
        org.claims(), "c1", "mcp__sama__tool_call", wrapped("mail_read")
    )
    assert (verdict.action, verdict.step) == ("deny", GateStep.TOOLSET)


async def test_a_policy_rule_for_the_deferred_tool_applies(engine: AsyncEngine, org: Org1) -> None:
    await grant(engine, org, "tool_search", "mail")
    await add_rule(engine, org, "mail_read", "allow")
    await add_rule(engine, org, "mail_send", "deny")
    service = gate(engine)
    allowed = await service.check(org.claims(), "c1", "tool_call", wrapped("mail_read"))
    denied = await service.check(org.claims(), "c2", "tool_call", wrapped("mail_send", **SEND))
    assert (allowed.action, denied.action) == ("allow", "deny")
    assert denied.step is GateStep.POLICY


async def test_an_unattended_run_gets_deny_for_an_ask_tool(engine: AsyncEngine, org: Org1) -> None:
    await grant(engine, org, "tool_search", "mail")
    verdict = await gate(engine).check(
        org.claims("night"), "c1", "tool_call", wrapped("mail_send", **SEND)
    )
    assert verdict.action == "deny"
    assert await approval(engine, org, "c1") is None


async def test_an_approved_deferred_call_gives_one_ticket_for_its_arguments(
    engine: AsyncEngine, org: Org1
) -> None:
    await grant(engine, org, "tool_search", "mail")
    service = gate(engine)
    arguments = wrapped("mail_send", **SEND)
    assert (await service.check(org.claims(), "c1", "tool_call", arguments)).action == "ask"
    await service.decide_approval(
        org_id=org.ids["org"],
        run_id=org.ids["run"],
        call_id="c1",
        user_id=org.ids["mia"],
        approve=True,
    )
    # The tool server screens the call before the reconfirm: there is no ticket yet.
    assert (await service.screen(org.claims(), "tool_call", arguments)).action == "deny"
    verdict = await service.reconfirm(org_id=org.ids["org"], run_id=org.ids["run"], call_id="c1")
    assert verdict.action == "allow"
    # Other arguments, and another tool, do not match the ticket.
    other = await service.screen(org.claims(), "tool_call", wrapped("mail_send", to="x@y.z"))
    assert other.action == "deny"
    other_tool = await service.screen(org.claims(), "tool_call", wrapped("mail_read"))
    assert other_tool.action == "deny"
    used = await service.screen(org.claims(), "tool_call", arguments)
    assert (used.action, used.reason) == ("allow", "the call has an approved ticket")
    assert (await service.screen(org.claims(), "tool_call", arguments)).action == "deny"
    actions = [action for action, _, _ in await audit(engine, org)]
    assert actions.count("tool_gate.ticket") == 1


async def test_a_check_of_a_changed_deferred_call_is_not_the_approved_call(
    engine: AsyncEngine, org: Org1
) -> None:
    await grant(engine, org, "tool_search", "mail")
    service = gate(engine)
    await service.check(org.claims(), "c1", "tool_call", wrapped("mail_send", **SEND))
    await service.decide_approval(
        org_id=org.ids["org"],
        run_id=org.ids["run"],
        call_id="c1",
        user_id=org.ids["mia"],
        approve=True,
    )
    changed = await service.check(org.claims(), "c1", "tool_call", wrapped("mail_send", to="x@y.z"))
    assert changed.action == "deny"


async def test_visible_tools_are_the_tools_of_the_toolsets_without_a_deny_rule(
    engine: AsyncEngine, org: Org1
) -> None:
    await grant(engine, org, "tool_search", "mail")
    await add_rule(engine, org, "mail_send", "deny")
    visible = await gate(engine).visible_tools(org.claims())
    assert {"mail_read", "tool_call", "todo"} <= visible
    assert "mail_send" not in visible
    assert "clock" not in visible


async def test_a_run_that_ended_sees_no_tool(engine: AsyncEngine, org: Org1) -> None:
    await grant(engine, org, "tool_search", "mail")
    async with engine.begin() as connection:
        await connection.execute(
            update(Run).where(Run.id == org.ids["run"]).values(status="completed")
        )
    assert await gate(engine).visible_tools(org.claims()) == frozenset()


async def test_a_shared_run_sees_the_group_toolsets_only(engine: AsyncEngine, org: Org1) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == org.ids["profile"])
            .values(toolsets=["tool_search", "mail"], group_toolsets=["tool_search"])
        )
    visible = await gate(engine).visible_tools(org.claims())
    assert "mail_read" not in visible
    assert "tool_search" in visible
