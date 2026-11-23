"""The tool gate service with Postgres (docs/architecture.md, Section 16.1).

Each test makes its own org, so the audit rows and approvals of the test do not change what
the shared seed of the RLS tests reads.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import (
    AgentProfile,
    Approval,
    AuditEntry,
    Membership,
    ModelCall,
    Org,
    Run,
    Session,
    SessionToolAllow,
    ToolCall,
    ToolPolicy,
    User,
    Workspace,
)
from sama.gate import GateStep, arguments_digest
from sama.gate.service import (
    MAX_STORED_ARGUMENT_CHARS,
    ApprovalError,
    ApprovalExpiredError,
    GateService,
    allowed_scopes,
    stored_arguments,
)
from sama.tenancy import RunTokenClaims

ARGS = {"command": "make test"}


@dataclass
class Org1:
    ids: dict[str, uuid.UUID] = field(default_factory=dict)

    def claims(self, run: str = "run", **changes: Any) -> RunTokenClaims:
        values: dict[str, Any] = {
            "org_id": str(self.ids["org"]),
            "workspace_id": str(self.ids["ws"]),
            "session_id": str(self.ids["shared"]),
            "run_id": str(self.ids[run]),
            "token_id": "t-1",
            "expires_at": 2_000_000_000,
        }
        values.update(changes)
        return RunTokenClaims(**values)


@pytest.fixture
async def org(engine: AsyncEngine) -> AsyncIterator[Org1]:
    """One org: ``admin`` (org admin), ``mia`` and ``max`` (members of ``ws``), ``nia``
    (member with no workspace). ``shared`` is a shared session with the run ``run`` (attended,
    started by mia) and ``night`` (unattended). ``secret`` is mia's private session."""
    world = Org1()
    ids = world.ids
    for label in ("org", "ws", "profile", "admin", "mia", "max", "nia", "shared", "secret"):
        ids[label] = uuid.uuid4()
    for label in ("run", "night", "secret-run"):
        ids[label] = uuid.uuid4()
    org_id = ids["org"]
    rows: list[tuple[Any, dict[str, Any]]] = [
        (Org, {"id": org_id, "name": "G", "slug": f"g-{org_id.hex[:8]}"}),
        (Workspace, {"id": ids["ws"], "org_id": org_id, "name": "ws", "slug": "ws"}),
    ]
    for name in ("admin", "mia", "max", "nia"):
        rows.append(
            (
                User,
                {
                    "id": ids[name],
                    "org_id": org_id,
                    "auth_user_id": uuid.uuid4(),
                    "email": f"{name}@example.test",
                    "display_name": name,
                },
            )
        )
    for name, workspace, role in (
        ("admin", None, "admin"),
        ("mia", None, "member"),
        ("mia", "ws", "member"),
        ("max", None, "member"),
        ("max", "ws", "member"),
        ("nia", None, "member"),
    ):
        rows.append(
            (
                Membership,
                {
                    "org_id": org_id,
                    "user_id": ids[name],
                    "workspace_id": ids[workspace] if workspace else None,
                    "role": role,
                },
            )
        )
    rows.append(
        (
            AgentProfile,
            {
                "id": ids["profile"],
                "org_id": org_id,
                "name": "p",
                "model": "claude-sonnet-5-5",
                "toolsets": ["core", "todo", "web", "clarify"],
                # The tests of the gate steps use the same tools in the shared session. The
                # tests of the group toolset change this value.
                "group_toolsets": ["core", "todo", "web", "clarify"],
            },
        )
    )
    for session, audience, owner in (("shared", "shared", None), ("secret", "private", "mia")):
        rows.append(
            (
                Session,
                {
                    "id": ids[session],
                    "org_id": org_id,
                    "workspace_id": ids["ws"],
                    "agent_profile_id": ids["profile"],
                    "audience": audience,
                    "owner_user_id": ids[owner] if owner else None,
                    "title": session,
                },
            )
        )
    for run, session, unattended in (
        ("run", "shared", False),
        ("night", "shared", True),
        ("secret-run", "secret", False),
    ):
        rows.append(
            (
                Run,
                {
                    "id": ids[run],
                    "org_id": org_id,
                    "session_id": ids[session],
                    "agent_profile_id": ids["profile"],
                    "triggered_by_user_id": ids["mia"],
                    "unattended": unattended,
                    "runtime": "claude_agent_sdk",
                },
            )
        )
    async with engine.begin() as connection:
        for model, values in rows:
            await connection.execute(insert(model).values(**values))
    yield world
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL sama.audit_purge = 'on'"))
        await connection.execute(delete(Org).where(Org.id == org_id))


async def audit(engine: AsyncEngine, org: Org1) -> list[tuple[str, str | None, dict[str, Any]]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(AuditEntry.action, AuditEntry.target_id, AuditEntry.details)
            .where(AuditEntry.org_id == org.ids["org"])
            .order_by(AuditEntry.id)
        )
        return [(action, target, details) for action, target, details in rows]


async def approval(engine: AsyncEngine, org: Org1, call_id: str) -> Any:
    async with engine.connect() as connection:
        return (
            await connection.execute(
                select(Approval).where(
                    Approval.org_id == org.ids["org"], Approval.call_id == call_id
                )
            )
        ).one_or_none()


async def add_rule(
    engine: AsyncEngine, org: Org1, tool: str, action: str, user: str | None = None
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            insert(ToolPolicy).values(
                org_id=org.ids["org"],
                user_id=org.ids[user] if user else None,
                tool=tool,
                action=action,
            )
        )


# -- verdicts ---------------------------------------------------------------------------------


async def test_an_allowed_call_is_recorded(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)
    verdict = await gate.check(org.claims(), "c1", "Read", {"file_path": "a.txt"})
    assert verdict.action == "allow"
    async with engine.connect() as connection:
        call = (
            await connection.execute(
                select(ToolCall.tool_name, ToolCall.decision, ToolCall.status).where(
                    ToolCall.run_id == org.ids["run"], ToolCall.call_id == "c1"
                )
            )
        ).one()
    assert tuple(call) == ("Read", "allow", "started")
    [(action, target, details)] = await audit(engine, org)
    assert (action, target) == ("tool_gate.verdict", "c1")
    assert details["action"] == "allow"
    assert details["step"] == "policy"
    assert details["session_id"] == str(org.ids["shared"])


async def test_ask_creates_one_approval(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine, approval_ttl=timedelta(hours=2))
    first = await gate.check(org.claims(), "c1", "Bash", ARGS)
    again = await gate.check(org.claims(), "c1", "Bash", ARGS)
    assert first.action == again.action == "ask"
    assert again.step is GateStep.RECORDED
    row = await approval(engine, org, "c1")
    assert row.status == "pending"
    assert row.tool_name == "Bash"
    assert row.arguments == ARGS
    assert row.arguments_digest == arguments_digest(ARGS)
    assert row.expires_at - row.created_at == timedelta(hours=2)
    actions = [action for action, _, _ in await audit(engine, org)]
    assert actions == ["tool_gate.verdict", "approval.requested", "tool_gate.verdict"]


async def test_a_call_of_a_subagent_that_would_ask_is_denied_and_has_no_approval(
    engine: AsyncEngine, org: Org1
) -> None:
    """No human can answer a sub-agent call. The gate must not leave a pending approval."""
    gate = GateService(engine)
    verdict = await gate.check(org.claims(), "c1", "Bash", ARGS, child=True)
    assert verdict.action == "deny"
    assert "sub-agent" in verdict.reason
    async with engine.connect() as connection:
        call = (
            await connection.execute(
                select(ToolCall.decision, ToolCall.status).where(
                    ToolCall.run_id == org.ids["run"], ToolCall.call_id == "c1"
                )
            )
        ).one()
    assert tuple(call) == ("deny", "denied")
    assert await approval(engine, org, "c1") is None
    actions = [action for action, _, _ in await audit(engine, org)]
    assert actions == ["tool_gate.verdict"], "no approval.requested"
    # A call of the run itself still asks, and it still gets its approval.
    parent = await gate.check(org.claims(), "c2", "Bash", ARGS)
    assert parent.action == "ask"
    row = await approval(engine, org, "c2")
    assert row is not None
    assert row.status == "pending"


async def test_a_call_of_a_subagent_that_would_allow_is_allowed(
    engine: AsyncEngine, org: Org1
) -> None:
    gate = GateService(engine)
    verdict = await gate.check(org.claims(), "c1", "Read", {"file_path": "a.txt"}, child=True)
    assert verdict.action == "allow"


async def test_a_denied_call_is_recorded_as_denied(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)
    verdict = await gate.check(org.claims(), "c1", "Bash", {"command": "rm -rf /"})
    assert (verdict.action, verdict.step) == ("deny", GateStep.HARDLINE)
    async with engine.connect() as connection:
        status = (
            await connection.execute(
                select(ToolCall.status).where(ToolCall.run_id == org.ids["run"])
            )
        ).scalar_one()
    assert status == "denied"
    assert await approval(engine, org, "c1") is None


async def test_an_unattended_run_gets_deny_and_no_approval(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)
    verdict = await gate.check(org.claims("night"), "c1", "Bash", ARGS)
    assert (verdict.action, verdict.step) == ("deny", GateStep.CONTEXT)
    clarify = await gate.check(org.claims("night"), "c2", "mcp__sama__clarify", {})
    assert clarify.action == "deny"
    assert await approval(engine, org, "c1") is None


@pytest.mark.parametrize(
    "changes",
    [
        {"org_id": str(uuid.uuid4())},
        {"workspace_id": str(uuid.uuid4())},
        {"session_id": str(uuid.uuid4())},
        {"run_id": "not-a-uuid"},
    ],
)
async def test_an_unknown_run_is_denied(
    engine: AsyncEngine, org: Org1, changes: dict[str, str]
) -> None:
    gate = GateService(engine)
    verdict = await gate.check(org.claims(**changes), "c1", "Read", {})
    assert verdict.action == "deny"
    assert verdict.reason == "the run is unknown or ended"
    assert await audit(engine, org) == []


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_an_ended_run_gets_no_tool(engine: AsyncEngine, org: Org1, status: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(update(Run).where(Run.id == org.ids["run"]).values(status=status))
    gate = GateService(engine)
    verdict = await gate.check(org.claims(), "c1", "Read", {})
    assert (verdict.action, verdict.reason) == ("deny", "the run is unknown or ended")
    assert (await gate.screen(org.claims(), "mcp__sama__todo", {})).action == "deny"


async def test_a_call_needs_an_id(engine: AsyncEngine, org: Org1) -> None:
    assert (await GateService(engine).check(org.claims(), "", "Read", {})).action == "deny"


async def test_the_profile_toolsets_limit_the_tools(engine: AsyncEngine, org: Org1) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == org.ids["profile"])
            .values(toolsets=["web"], group_toolsets=["web"])
        )
    for claims in (org.claims(), org.claims("secret-run", session_id=str(org.ids["secret"]))):
        verdict = await GateService(engine).check(claims, "c1", "Read", {})
        assert (verdict.action, verdict.step) == ("deny", GateStep.TOOLSET)


async def test_the_run_toolsets_limit_the_tools(engine: AsyncEngine, org: Org1) -> None:
    # An event trigger limits its runs to its toolsets (Section 12.2): the run cannot use a
    # toolset of the profile outside the limit.
    async with engine.begin() as connection:
        await connection.execute(
            update(Run).where(Run.id == org.ids["run"]).values(toolsets=["web", "clarify"])
        )
    gate = GateService(engine)
    verdict = await gate.check(org.claims(), "c1", "Read", {})
    assert (verdict.action, verdict.step) == ("deny", GateStep.TOOLSET)
    allowed = await gate.check(org.claims(), "c2", "mcp__sama__web_search", {"query": "x"})
    assert allowed.action == "allow"


async def test_rules_come_from_the_org_and_the_run_user(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)
    await add_rule(engine, org, "Bash", "allow", user="max")
    assert (await gate.check(org.claims(), "c1", "Bash", ARGS)).action == "ask"
    await add_rule(engine, org, "Bash", "allow", user="mia")
    assert (await gate.check(org.claims(), "c2", "Bash", ARGS)).action == "allow"
    await add_rule(engine, org, "Bash", "deny")
    assert (await gate.check(org.claims(), "c3", "Bash", ARGS)).action == "deny"


async def test_the_token_user_is_the_fallback_for_user_rules(
    engine: AsyncEngine, org: Org1
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Run).where(Run.id == org.ids["run"]).values(triggered_by_user_id=None)
        )
    await add_rule(engine, org, "Bash", "allow", user="max")
    claims = org.claims(user_id=str(org.ids["max"]))
    assert (await GateService(engine).check(claims, "c1", "Bash", ARGS)).action == "allow"


async def test_screen_audits_only_a_deny(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)
    assert (await gate.screen(org.claims(), "mcp__sama__web_search", {})).action == "allow"
    assert (await gate.screen(org.claims("night"), "clarify", {})).action == "deny"
    [(action, target, details)] = await audit(engine, org)
    assert (action, target, details["tool"]) == ("tool_gate.screen", None, "clarify")
    async with engine.connect() as connection:
        calls = (
            await connection.execute(select(ToolCall).where(ToolCall.org_id == org.ids["org"]))
        ).all()
    assert calls == []


def test_large_arguments_are_cut() -> None:
    small = {"a": "b"}
    assert stored_arguments(small) == small
    cut = stored_arguments({"text": "x" * (MAX_STORED_ARGUMENT_CHARS + 10)})
    assert cut["_cut"] is True
    assert len(cut["preview"]) <= MAX_STORED_ARGUMENT_CHARS


async def call_row(engine: AsyncEngine, org: Org1, call_id: str) -> Any:
    async with engine.connect() as connection:
        return (
            await connection.execute(
                select(ToolCall).where(
                    ToolCall.run_id == org.ids["run"], ToolCall.call_id == call_id
                )
            )
        ).one()


async def test_a_call_id_is_bound_to_one_call(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)
    first = await gate.check(org.claims(), "c1", "Read", {"file_path": "a.txt"})
    again = await gate.check(org.claims(), "c1", "Read", {"file_path": "a.txt"})
    assert first.action == again.action == "allow"
    other = await gate.check(org.claims(), "c1", "Read", {"file_path": "b.txt"})
    assert (other.action, other.step) == ("deny", GateStep.RECORDED)
    tool = await gate.check(org.claims(), "c1", "Glob", {"file_path": "a.txt"})
    assert tool.action == "deny"
    row = await call_row(engine, org, "c1")
    assert (row.tool_name, row.arguments, row.decision, row.status) == (
        "Read",
        {"file_path": "a.txt"},
        "allow",
        "started",
    )
    assert row.arguments_digest == arguments_digest({"file_path": "a.txt"})
    actions = [details["action"] for _, _, details in await audit(engine, org)]
    assert actions == ["allow", "allow", "deny", "deny"]


async def test_concurrent_first_checks_of_one_call_id_are_in_a_sequence(
    engine: AsyncEngine, org: Org1
) -> None:
    gate = GateService(engine)
    deploy = {"command": "make deploy"}
    verdicts = await asyncio.gather(
        *(gate.check(org.claims(), "c1", "Bash", ARGS if i % 2 else deploy) for i in range(6))
    )
    assert sorted(v.action for v in verdicts).count("ask") == 3
    assert sorted(v.action for v in verdicts).count("deny") == 3
    row = await approval(engine, org, "c1")
    winner = ARGS if row.arguments_digest == arguments_digest(ARGS) else deploy
    for i, verdict in enumerate(verdicts):
        expected = "ask" if (ARGS if i % 2 else deploy) == winner else "deny"
        assert verdict.action == expected


# -- approvals --------------------------------------------------------------------------------


async def ask(gate: GateService, org: Org1, call_id: str = "c1", run: str = "run") -> None:
    session = "secret" if run == "secret-run" else "shared"
    claims = org.claims(run, session_id=str(org.ids[session]))
    assert (await gate.check(claims, call_id, "Bash", ARGS)).action == "ask"


async def test_an_approval_allows_the_same_call(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)
    await ask(gate, org)
    status = await gate.decide_approval(
        org_id=org.ids["org"],
        run_id=org.ids["run"],
        call_id="c1",
        user_id=org.ids["admin"],
        approve=True,
        reason="fine",
    )
    assert status == "approved"
    row = await approval(engine, org, "c1")
    assert row.decided_by_user_id == org.ids["admin"]
    assert row.decided_at is not None
    assert row.decision_reason == "fine"
    verdict = await gate.check(org.claims(), "c1", "Bash", ARGS)
    assert (verdict.action, verdict.step) == ("allow", GateStep.RECORDED)
    other = await gate.check(org.claims(), "c1", "Bash", {"command": "make deploy"})
    assert other.action == "deny"
    approved = [e for e in await audit(engine, org) if e[0] == "approval.approved"]
    assert len(approved) == 1


async def test_a_rejection_denies_the_call(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)
    await ask(gate, org)
    status = await gate.decide_approval(
        org_id=org.ids["org"],
        run_id=org.ids["run"],
        call_id="c1",
        user_id=org.ids["admin"],
        approve=False,
    )
    assert status == "rejected"
    assert (await gate.check(org.claims(), "c1", "Bash", ARGS)).action == "deny"
    with pytest.raises(ApprovalError, match="already rejected"):
        await gate.decide_approval(
            org_id=org.ids["org"],
            run_id=org.ids["run"],
            call_id="c1",
            user_id=org.ids["admin"],
            approve=True,
        )


@pytest.mark.parametrize(
    ("run", "user", "allowed"),
    [
        ("run", "mia", True),
        ("run", "admin", True),
        ("run", "max", False),
        ("run", "nia", False),
        ("secret-run", "mia", True),
        ("secret-run", "max", False),
        ("secret-run", "admin", False),
    ],
)
async def test_only_a_user_who_sees_the_session_decides(
    engine: AsyncEngine, org: Org1, run: str, user: str, allowed: bool
) -> None:
    gate = GateService(engine)
    await ask(gate, org, run=run)

    async def decide() -> str:
        return await gate.decide_approval(
            org_id=org.ids["org"],
            run_id=org.ids[run],
            call_id="c1",
            user_id=org.ids[user],
            approve=True,
        )

    if allowed:
        assert await decide() == "approved"
    else:
        with pytest.raises(ApprovalError, match=r"cannot decide|only the person who asked"):
            await decide()


async def test_a_user_of_another_org_cannot_decide(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)
    await ask(gate, org)
    with pytest.raises(ApprovalError, match="does not exist"):
        await gate.decide_approval(
            org_id=uuid.uuid4(),
            run_id=org.ids["run"],
            call_id="c1",
            user_id=org.ids["mia"],
            approve=True,
        )


async def test_always_allow_writes_a_user_rule(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)

    async def always(call_id: str) -> None:
        await gate.decide_approval(
            org_id=org.ids["org"],
            run_id=org.ids["run"],
            call_id=call_id,
            user_id=org.ids["mia"],
            approve=True,
            always_allow=True,
        )

    await ask(gate, org, "c1")
    await always("c1")
    assert (await gate.check(org.claims(), "c2", "Bash", ARGS)).action == "allow"
    # An admin ask rule holds over the user rule. A second "always allow" changes the rule.
    await add_rule(engine, org, "Bash", "ask")
    await ask(gate, org, "c3")
    await always("c3")
    async with engine.connect() as connection:
        rules = (
            await connection.execute(
                select(
                    ToolPolicy.tool,
                    ToolPolicy.action,
                    ToolPolicy.agent_profile_id,
                    ToolPolicy.user_id,
                ).where(ToolPolicy.org_id == org.ids["org"], ToolPolicy.user_id.is_not(None))
            )
        ).all()
    assert [tuple(r) for r in rules] == [("Bash", "allow", org.ids["profile"], org.ids["mia"])]
    sets = [e for e in await audit(engine, org) if e[0] == "tool_policy.set"]
    assert len(sets) == 2


async def test_always_allow_for_one_user_keeps_ask_for_others(
    engine: AsyncEngine, org: Org1
) -> None:
    gate = GateService(engine)
    await ask(gate, org)
    await gate.decide_approval(
        org_id=org.ids["org"],
        run_id=org.ids["run"],
        call_id="c1",
        user_id=org.ids["admin"],
        approve=True,
        always_allow=True,
    )
    # The run belongs to mia, and the rule is for the admin.
    assert (await gate.check(org.claims(), "c2", "Bash", ARGS)).action == "ask"


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


async def test_an_approval_times_out_on_read(engine: AsyncEngine, org: Org1) -> None:
    clock = Clock()
    gate = GateService(engine, approval_ttl=timedelta(hours=1), clock=clock)
    await ask(gate, org)
    clock.now += timedelta(hours=1, seconds=1)
    verdict = await gate.check(org.claims(), "c1", "Bash", ARGS)
    assert (verdict.action, verdict.step) == ("deny", GateStep.RECORDED)
    assert (await approval(engine, org, "c1")).status == "expired"
    assert "approval.expired" in [action for action, _, _ in await audit(engine, org)]


async def test_a_late_decision_raises_and_expires(engine: AsyncEngine, org: Org1) -> None:
    clock = Clock()
    gate = GateService(engine, approval_ttl=timedelta(hours=1), clock=clock)
    await ask(gate, org)
    clock.now += timedelta(hours=2)
    with pytest.raises(ApprovalExpiredError):
        await gate.decide_approval(
            org_id=org.ids["org"],
            run_id=org.ids["run"],
            call_id="c1",
            user_id=org.ids["mia"],
            approve=True,
        )
    assert (await approval(engine, org, "c1")).status == "expired"


async def test_expire_times_out_due_approvals(engine: AsyncEngine, org: Org1) -> None:
    clock = Clock()
    gate = GateService(engine, approval_ttl=timedelta(hours=1), clock=clock)
    await ask(gate, org, "c1")
    clock.now += timedelta(minutes=30)
    await ask(gate, org, "c2")
    clock.now += timedelta(minutes=31)
    assert await gate.expire() >= 1
    assert (await approval(engine, org, "c1")).status == "expired"
    assert (await approval(engine, org, "c2")).status == "pending"
    expired = [t for action, t, _ in await audit(engine, org) if action == "approval.expired"]
    assert expired == ["c1"]


def test_the_ttl_must_be_positive(engine: AsyncEngine) -> None:
    with pytest.raises(ValueError, match="approval_ttl"):
        GateService(engine, approval_ttl=timedelta(0))


# -- live authority (M1.18) ---------------------------------------------------------------------


async def approve(gate: GateService, org: Org1, call_id: str = "c1") -> None:
    await ask(gate, org, call_id)
    await gate.decide_approval(
        org_id=org.ids["org"],
        run_id=org.ids["run"],
        call_id=call_id,
        user_id=org.ids["mia"],
        approve=True,
    )


async def reconfirm(gate: GateService, org: Org1, call_id: str = "c1") -> Any:
    return await gate.reconfirm(org_id=org.ids["org"], run_id=org.ids["run"], call_id=call_id)


async def spend(engine: AsyncEngine, org: Org1, *, tokens: int, cost: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            insert(ModelCall).values(
                call_id=uuid.uuid4(),
                org_id=org.ids["org"],
                workspace_id=org.ids["ws"],
                session_id=org.ids["shared"],
                run_id=org.ids["run"],
                provider="anthropic",
                model="claude-sonnet-5-5",
                endpoint="messages",
                status_code=200,
                input_tokens=tokens,
                cost_usd=cost,
                duration_ms=1,
            )
        )


async def test_screen_without_a_ticket_denies_an_ask_tool(engine: AsyncEngine, org: Org1) -> None:
    verdict = await GateService(engine).screen(org.claims(), "Bash", ARGS)
    assert (verdict.action, verdict.step) == ("deny", GateStep.RECORDED)
    assert "approval" in verdict.reason
    [(action, _, details)] = await audit(engine, org)
    assert (action, details["tool"]) == ("tool_gate.screen", "Bash")


async def test_an_approved_call_gives_one_ticket(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)
    await approve(gate, org)
    assert (await gate.screen(org.claims(), "Bash", ARGS)).action == "deny"  # no reconfirm yet
    verdict = await reconfirm(gate, org)
    assert verdict.action == "allow"
    assert (await gate.screen(org.claims(), "Bash", {"command": "rm -rf ~"})).action == "deny"
    used = await gate.screen(org.claims(), "Bash", ARGS)
    assert (used.action, used.reason) == ("allow", "the call has an approved ticket")
    assert (await gate.screen(org.claims(), "Bash", ARGS)).action == "deny"
    row = await call_row(engine, org, "c1")
    assert row.status == "finished"
    # A check of the same call again does not make a new ticket.
    assert (await gate.check(org.claims(), "c1", "Bash", ARGS)).action == "allow"
    assert (await reconfirm(gate, org)).action == "allow"
    assert (await call_row(engine, org, "c1")).status == "finished"
    assert (await gate.screen(org.claims(), "Bash", ARGS)).action == "deny"
    actions = [action for action, _, _ in await audit(engine, org)]
    assert actions.count("tool_gate.ticket") == 1
    assert actions.count("tool_gate.reconfirm") == 2


async def test_a_failed_call_stays_failed_and_gives_no_ticket(
    engine: AsyncEngine, org: Org1
) -> None:
    """A call that ended with an error (``failed``, M8.3) is not a ticket again after a new
    check of the same call id."""
    gate = GateService(engine)
    await approve(gate, org)
    async with engine.begin() as connection:
        await connection.execute(
            update(ToolCall)
            .where(ToolCall.run_id == org.ids["run"], ToolCall.call_id == "c1")
            .values(status="failed")
        )
    assert (await reconfirm(gate, org)).action == "allow"
    assert (await call_row(engine, org, "c1")).status == "failed"
    assert (await gate.screen(org.claims(), "Bash", ARGS)).action == "deny"


async def test_a_ticket_is_only_for_its_run(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)
    await approve(gate, org)
    assert (await reconfirm(gate, org)).action == "allow"
    other = await gate.screen(org.claims("night"), "Bash", ARGS)
    assert other.action == "deny"
    assert (await call_row(engine, org, "c1")).status == "started"


async def test_reconfirm_makes_the_row_of_a_runtime_approval(
    engine: AsyncEngine, org: Org1
) -> None:
    """The fake runtime and the queue record an approval without a gate check."""
    async with engine.begin() as connection:
        await connection.execute(
            insert(Approval).values(
                org_id=org.ids["org"],
                session_id=org.ids["shared"],
                run_id=org.ids["run"],
                call_id="c9",
                tool_name="mcp__sama__web_search",
                arguments={"query": "x"},
                arguments_digest=arguments_digest({"query": "x"}),
                status="approved",
                decided_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    gate = GateService(engine)
    assert (await reconfirm(gate, org, "c9")).action == "allow"
    row = await call_row(engine, org, "c9")
    assert (row.tool_name, row.arguments, row.decision, row.status) == (
        "web_search",
        {"query": "x"},
        "allow",
        "started",
    )


async def test_reconfirm_needs_an_approved_call(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)
    none = await reconfirm(gate, org, "c-none")
    assert (none.action, none.reason) == ("deny", "the call has no approval")
    await ask(gate, org)
    pending = await reconfirm(gate, org)
    assert (pending.action, pending.reason) == ("deny", "the approval is pending")
    unknown = await gate.reconfirm(org_id=org.ids["org"], run_id=uuid.uuid4(), call_id="c1")
    assert (unknown.action, unknown.step) == ("deny", GateStep.AUTHORITY)


async def test_a_revoked_tool_is_denied_in_the_session(engine: AsyncEngine, org: Org1) -> None:
    """The acceptance test of M1.18: a deny rule during a session applies at the next call."""
    gate = GateService(engine)
    assert (await gate.check(org.claims(), "c1", "WebFetch", {"url": "u"})).action != "deny"
    await add_rule(engine, org, "WebFetch", "deny")
    verdict = await gate.check(org.claims(), "c2", "WebFetch", {"url": "u"})
    assert (verdict.action, verdict.step) == ("deny", GateStep.POLICY)


async def test_a_deny_rule_wins_over_an_approval(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)
    await approve(gate, org)
    await add_rule(engine, org, "Bash", "deny")
    assert (await gate.check(org.claims(), "c1", "Bash", ARGS)).action == "deny"
    verdict = await reconfirm(gate, org)
    assert (verdict.action, verdict.step) == ("deny", GateStep.POLICY)
    row = await call_row(engine, org, "c1")
    assert (row.decision, row.status) == ("deny", "denied")
    assert (await gate.screen(org.claims(), "Bash", ARGS)).action == "deny"


async def test_a_removed_membership_stops_the_run(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)
    await approve(gate, org)
    async with engine.begin() as connection:
        await connection.execute(
            delete(Membership).where(
                Membership.org_id == org.ids["org"],
                Membership.user_id == org.ids["mia"],
                Membership.workspace_id == org.ids["ws"],
            )
        )
    for verdict in (
        await gate.check(org.claims(), "c2", "Read", {}),
        await gate.screen(org.claims(), "mcp__sama__web_search", {}),
        await reconfirm(gate, org),
    ):
        assert (verdict.action, verdict.step) == ("deny", GateStep.AUTHORITY)
        assert verdict.reason == "the user of the run can no longer see the session"


async def test_a_cancelled_run_cannot_resume_an_approved_call(
    engine: AsyncEngine, org: Org1
) -> None:
    gate = GateService(engine)
    await approve(gate, org)
    async with engine.begin() as connection:
        await connection.execute(
            update(Run).where(Run.id == org.ids["run"]).values(status="cancelled")
        )
    verdict = await reconfirm(gate, org)
    assert (verdict.action, verdict.step) == ("deny", GateStep.AUTHORITY)


@pytest.mark.parametrize(
    ("run_budget", "profile_budget", "reason"),
    [
        ({"max_tokens": 100}, {}, "the run reached max_tokens"),
        ({}, {"max_cost_usd": "0.5"}, "the run reached max_cost_usd"),
        ({"max_tokens": 10_000}, {"max_tokens": 100}, "the run reached max_tokens"),
    ],
)
async def test_a_spent_budget_denies_the_next_tool(
    engine: AsyncEngine,
    org: Org1,
    run_budget: dict[str, Any],
    profile_budget: dict[str, Any],
    reason: str,
) -> None:
    gate = GateService(engine)
    async with engine.begin() as connection:
        await connection.execute(
            update(Run).where(Run.id == org.ids["run"]).values(budget=run_budget)
        )
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == org.ids["profile"])
            .values(budget=profile_budget)
        )
    assert (await gate.check(org.claims(), "c1", "Read", {})).action == "allow"
    await spend(engine, org, tokens=100, cost="0.5")
    verdict = await gate.check(org.claims(), "c2", "Read", {})
    assert (verdict.action, verdict.step, verdict.reason) == ("deny", GateStep.AUTHORITY, reason)


async def test_a_private_run_without_a_user_uses_the_owner(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)
    async with engine.begin() as connection:
        await connection.execute(
            update(Run).where(Run.id == org.ids["secret-run"]).values(triggered_by_user_id=None)
        )
    claims = org.claims("secret-run", session_id=str(org.ids["secret"]))
    assert (await gate.check(claims, "c1", "Read", {})).action == "allow"
    async with engine.begin() as connection:
        await connection.execute(
            delete(Membership).where(
                Membership.user_id == org.ids["mia"], Membership.workspace_id == org.ids["ws"]
            )
        )
    assert (await gate.check(claims, "c2", "Read", {})).step is GateStep.AUTHORITY


async def test_reconfirm_loads_the_rules_of_the_private_session_owner(
    engine: AsyncEngine, org: Org1
) -> None:
    """A private run without a starting user has the owner as its user, also in reconfirm."""
    gate = GateService(engine)
    async with engine.begin() as connection:
        await connection.execute(
            update(Run).where(Run.id == org.ids["secret-run"]).values(triggered_by_user_id=None)
        )
    await ask(gate, org, run="secret-run")
    await gate.decide_approval(
        org_id=org.ids["org"],
        run_id=org.ids["secret-run"],
        call_id="c1",
        user_id=org.ids["mia"],
        approve=True,
    )
    await add_rule(engine, org, "Bash", "deny", user="mia")
    verdict = await gate.reconfirm(
        org_id=org.ids["org"], run_id=org.ids["secret-run"], call_id="c1"
    )
    assert (verdict.action, verdict.step) == ("deny", GateStep.POLICY)


# -- the group toolset and the approvers of a shared session (Section 11.3) --------------------


async def set_profile(engine: AsyncEngine, org: Org1, **values: Any) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile).where(AgentProfile.id == org.ids["profile"]).values(**values)
        )


def private_claims(org: Org1) -> RunTokenClaims:
    return org.claims("secret-run", session_id=str(org.ids["secret"]))


async def test_a_shared_run_uses_the_group_toolsets(engine: AsyncEngine, org: Org1) -> None:
    await set_profile(engine, org, group_toolsets=["todo", "web"])
    gate = GateService(engine)
    shared = await gate.check(org.claims(), "c1", "Bash", ARGS)
    assert (shared.action, shared.step) == ("deny", GateStep.TOOLSET)
    assert "not in the toolsets" in shared.reason
    # The same call in a private run is not denied at the toolset step: the profile has core.
    private = await gate.check(private_claims(org), "c1", "Bash", ARGS)
    assert (private.action, private.step) == ("ask", GateStep.POLICY)
    # A group tool and a tool loader still work in the shared run.
    assert (await gate.check(org.claims(), "c2", "web_search", {})).action == "allow"
    assert (await gate.check(org.claims(), "c3", "ToolSearch", {})).action == "allow"


async def test_a_profile_without_group_toolsets_gives_a_group_no_tool(
    engine: AsyncEngine, org: Org1
) -> None:
    await set_profile(engine, org, group_toolsets=[])
    gate = GateService(engine)
    denied = await gate.check(org.claims(), "c1", "web_search", {})
    assert (denied.action, denied.step) == ("deny", GateStep.TOOLSET)
    assert (await gate.check(private_claims(org), "c1", "web_search", {})).action == "allow"


async def test_a_resume_cannot_widen_the_group_toolsets(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)
    await ask(gate, org)
    await gate.decide_approval(
        org_id=org.ids["org"],
        run_id=org.ids["run"],
        call_id="c1",
        user_id=org.ids["mia"],
        approve=True,
    )
    await set_profile(engine, org, group_toolsets=["todo"])
    verdict = await reconfirm(gate, org)
    assert (verdict.action, verdict.step) == ("deny", GateStep.TOOLSET)


async def test_memory_needs_an_approval_in_a_shared_run_only(
    engine: AsyncEngine, org: Org1
) -> None:
    await set_profile(engine, org, toolsets=["core", "memory"], group_toolsets=["memory", "todo"])
    gate = GateService(engine)
    shared = await gate.check(org.claims(), "c1", "mcp__sama__memory", {"target": "agent"})
    assert (shared.action, shared.step) == ("ask", GateStep.POLICY)
    assert (await approval(engine, org, "c1")).status == "pending"
    private = await gate.check(private_claims(org), "c1", "mcp__sama__memory", {"target": "user"})
    assert private.action == "allow"
    # A person approves the write, and the same call goes on.
    await gate.decide_approval(
        org_id=org.ids["org"],
        run_id=org.ids["run"],
        call_id="c1",
        user_id=org.ids["mia"],
        approve=True,
    )
    again = await gate.check(org.claims(), "c1", "mcp__sama__memory", {"target": "agent"})
    assert (again.action, again.step) == ("allow", GateStep.RECORDED)


async def add_review_run(engine: AsyncEngine, org: Org1) -> uuid.UUID:
    run_id = uuid.uuid4()
    org.ids["review"] = run_id
    async with engine.begin() as connection:
        await connection.execute(
            insert(Run).values(
                id=run_id,
                org_id=org.ids["org"],
                session_id=org.ids["shared"],
                agent_profile_id=org.ids["profile"],
                runtime="claude_agent_sdk",
                unattended=True,
                kind="review",
                review_from_seq=0,
                review_to_seq=1,
            )
        )
    return run_id


async def test_a_review_of_a_shared_session_keeps_the_review_clamp(
    engine: AsyncEngine, org: Org1
) -> None:
    """The review is not a chat run: the group toolsets do not apply to it, and it has the
    memory and skill tools of the profile and no other tool (Section 9.4). Its memory call is
    not asked twice: a shared change is a proposal that a person decides."""
    await set_profile(
        engine,
        org,
        toolsets=["core", "memory", "skills"],
        group_toolsets=["todo"],
    )
    await add_review_run(engine, org)
    gate = GateService(engine)
    claims = org.claims("review")
    for call_id, tool in (("r1", "mcp__sama__memory"), ("r2", "mcp__sama__skill_view")):
        verdict = await gate.check(claims, call_id, tool, {})
        assert (verdict.action, verdict.step) == ("allow", GateStep.POLICY), tool
    denied = await gate.check(claims, "r3", "Bash", ARGS)
    assert (denied.action, denied.step) == ("deny", GateStep.TOOLSET)
    assert (await gate.check(claims, "r4", "todo", {})).step is GateStep.TOOLSET


async def decide_as(gate: GateService, org: Org1, user: str, call_id: str = "c1") -> str:
    return await gate.decide_approval(
        org_id=org.ids["org"],
        run_id=org.ids["run"],
        call_id=call_id,
        user_id=org.ids[user],
        approve=True,
    )


async def test_a_member_who_did_not_ask_cannot_decide_in_a_shared_session(
    engine: AsyncEngine, org: Org1
) -> None:
    gate = GateService(engine)
    await ask(gate, org)
    with pytest.raises(ApprovalError, match="only the person who asked or a workspace admin"):
        await decide_as(gate, org, "max")
    assert (await approval(engine, org, "c1")).status == "pending"
    assert await decide_as(gate, org, "mia") == "approved"


async def test_an_org_admin_and_a_workspace_admin_decide_in_a_shared_session(
    engine: AsyncEngine, org: Org1
) -> None:
    gate = GateService(engine)
    await ask(gate, org, "c1")
    await ask(gate, org, "c2")
    assert await decide_as(gate, org, "admin", "c1") == "approved"
    async with engine.begin() as connection:
        await connection.execute(
            update(Membership)
            .where(Membership.user_id == org.ids["max"], Membership.workspace_id == org.ids["ws"])
            .values(role="admin")
        )
    assert await decide_as(gate, org, "max", "c2") == "approved"


async def test_the_profile_approvers_widen_the_set(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)
    await ask(gate, org, "c1")
    await ask(gate, org, "c2")
    await set_profile(engine, org, policy={"approvers": [str(org.ids["max"]), "not-an-id", 7]})
    assert await decide_as(gate, org, "max", "c1") == "approved"
    # Only a listed user is added: nia has no workspace and still cannot see the session.
    with pytest.raises(ApprovalError, match="cannot decide"):
        await decide_as(gate, org, "nia", "c2")


async def test_a_star_approver_restores_the_decision_of_any_member(
    engine: AsyncEngine, org: Org1
) -> None:
    gate = GateService(engine)
    await ask(gate, org, "c1")
    await ask(gate, org, "c2")
    await set_profile(engine, org, policy={"approvers": ["*"]})
    assert await decide_as(gate, org, "max", "c1") == "approved"
    with pytest.raises(ApprovalError, match="cannot decide"):
        await decide_as(gate, org, "nia", "c2")


async def test_a_wrong_approvers_value_adds_nobody(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)
    await ask(gate, org)
    await set_profile(engine, org, policy={"approvers": "*"})
    with pytest.raises(ApprovalError, match="only the person who asked"):
        await decide_as(gate, org, "max")


async def test_the_approvers_do_not_change_a_private_session(
    engine: AsyncEngine, org: Org1
) -> None:
    gate = GateService(engine)
    await set_profile(engine, org, policy={"approvers": ["*"]})
    await ask(gate, org, run="secret-run")
    with pytest.raises(ApprovalError, match="cannot decide"):
        await gate.decide_approval(
            org_id=org.ids["org"],
            run_id=org.ids["secret-run"],
            call_id="c1",
            user_id=org.ids["max"],
            approve=True,
        )


# -- escalation: sensitive actions in the user's Chrome (M5.5) ---------------------------------


async def test_an_escalation_asks_for_an_allowed_tool(engine: AsyncEngine, org: Org1) -> None:
    seen: list[tuple[str, str, str]] = []

    def escalation(org_id: str, run_id: str, tool: str, arguments: Any) -> str | None:
        seen.append((org_id, run_id, tool))
        return "a sensitive action" if arguments.get("file_path") == "secret.txt" else None

    gate = GateService(engine, escalation=escalation)
    assert (await gate.check(org.claims(), "c1", "Read", {"file_path": "a.txt"})).action == "allow"
    verdict = await gate.check(org.claims(), "c2", "Read", {"file_path": "secret.txt"})
    assert (verdict.action, verdict.reason) == ("ask", "a sensitive action")
    assert (await approval(engine, org, "c2")).reason == "a sensitive action"
    assert seen[0] == (str(org.ids["org"]), str(org.ids["run"]), "Read")
    screened = await gate.screen(org.claims(), "Read", {"file_path": "secret.txt"})
    assert screened.action == "deny"  # no approval of a person yet


async def test_a_failing_escalation_check_asks(engine: AsyncEngine, org: Org1) -> None:
    def escalation(org_id: str, run_id: str, tool: str, arguments: Any) -> str | None:
        raise RuntimeError("boom")

    gate = GateService(engine, escalation=escalation)
    verdict = await gate.check(org.claims(), "c1", "Read", {"file_path": "a.txt"})
    assert verdict.action == "ask"


async def test_an_approved_call_under_an_allow_rule_uses_up_its_ticket(
    engine: AsyncEngine, org: Org1
) -> None:
    """A user approves a call with "always allow". The call then runs under the new allow rule.
    Its ticket must not stay for a later call with the same arguments (Section 14.2.2)."""
    gate = GateService(engine, escalation=lambda *_: None)
    await approve(gate, org)
    assert (await reconfirm(gate, org)).action == "allow"
    await add_rule(engine, org, "Bash", "allow")
    first = await gate.screen(org.claims(), "Bash", ARGS)
    assert (first.action, first.step) == ("allow", GateStep.POLICY)
    assert (await call_row(engine, org, "c1")).status == "finished"


# -- steps of a sub-agent tool (M5.6) ---------------------------------------------------------


async def test_inner_judges_a_step_of_browser_task_without_a_record(
    engine: AsyncEngine, org: Org1
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == org.ids["profile"])
            .values(toolsets=["browser"])
        )
    gate = GateService(engine)
    claims = org.claims("secret-run", session_id=str(org.ids["secret"]))
    assert (await gate.inner(claims, "browser_open", {"url": "https://a.test"})).action == "allow"
    assert (await gate.inner(claims, "browser_act", {"action": "click"})).action == "ask"
    assert (await gate.inner(claims, "mcp__sama__browser_task", {"goal": "g"})).action == "ask"
    # Without the toolset, and for an ended run, the step is denied.
    assert (await gate.inner(claims, "web_search", {"query": "q"})).action == "deny"
    gone = org.claims("secret-run", session_id=str(org.ids["secret"]), run_id=str(uuid.uuid4()))
    assert (await gate.inner(gone, "browser_open", {})).action == "deny"
    await add_rule(engine, org, "browser_act", "deny")
    denied = await gate.inner(claims, "browser_act", {"action": "click"})
    assert (denied.action, denied.step) == ("deny", GateStep.POLICY)
    actions = [(action, details["tool"]) for action, _, details in await audit(engine, org)]
    assert actions == [("tool_gate.inner", "web_search"), ("tool_gate.inner", "browser_act")]
    # In an unattended run, the approval of browser_task covers the ask of its steps (the
    # gate judged browser_task with the unattended rule), but a deny rule still wins.
    async with engine.begin() as connection:
        await connection.execute(
            update(Run).where(Run.id == org.ids["secret-run"]).values(unattended=True)
        )
    assert (await gate.inner(claims, "browser_tabs", {"action": "list"})).action == "allow"
    assert (await gate.inner(claims, "browser_task", {"goal": "g"})).action == "ask"
    assert (await gate.inner(claims, "browser_act", {"action": "click"})).action == "deny"
    async with engine.connect() as connection:
        calls = (
            await connection.execute(select(ToolCall).where(ToolCall.org_id == org.ids["org"]))
        ).all()
    assert calls == []


async def test_plan_mode_keeps_an_approved_ticket_and_blocks_other_calls(
    engine: AsyncEngine, org: Org1
) -> None:
    """Coding mode (Section 15.5): plan mode denies a tool that changes files, but a call
    that a person approved before still runs once with its ticket."""
    from sqlalchemy import update as sql_update

    from sama.db.models import Session as SessionRow

    gate = GateService(engine)
    await approve(gate, org)
    async with engine.begin() as connection:
        await connection.execute(
            sql_update(SessionRow).where(SessionRow.id == org.ids["shared"]).values(plan_mode=True)
        )
    blocked = await gate.screen(org.claims(), "Bash", {"command": "touch x"})
    assert (blocked.action, blocked.step) == ("deny", GateStep.PLAN)
    assert (await reconfirm(gate, org)).action == "allow"
    used = await gate.screen(org.claims(), "Bash", ARGS)
    assert (used.action, used.reason) == ("allow", "the call has an approved ticket")
    again = await gate.screen(org.claims(), "Bash", ARGS)
    assert (again.action, again.step) == ("deny", GateStep.PLAN)


async def test_plan_mode_keeps_a_ticket_after_always_allow(engine: AsyncEngine, org: Org1) -> None:
    from sama.db.models import Session as SessionRow
    from sama.db.models import ToolPolicy

    gate = GateService(engine)
    await approve(gate, org)
    async with engine.begin() as connection:
        await connection.execute(
            update(SessionRow).where(SessionRow.id == org.ids["shared"]).values(plan_mode=True)
        )
        await connection.execute(
            insert(ToolPolicy).values(org_id=org.ids["org"], tool="Bash", action="allow")
        )
    assert (await reconfirm(gate, org)).action == "allow"
    used = await gate.screen(org.claims(), "Bash", ARGS)
    assert (used.action, used.reason) == ("allow", "the call has an approved ticket")
    assert (await gate.screen(org.claims(), "Bash", ARGS)).step is GateStep.PLAN


async def test_plan_mode_with_a_ticket_still_obeys_a_new_deny_rule(
    engine: AsyncEngine, org: Org1
) -> None:
    from sama.db.models import Session as SessionRow
    from sama.db.models import ToolPolicy

    gate = GateService(engine)
    await approve(gate, org)
    assert (await reconfirm(gate, org)).action == "allow"
    async with engine.begin() as connection:
        await connection.execute(
            update(SessionRow).where(SessionRow.id == org.ids["shared"]).values(plan_mode=True)
        )
        await connection.execute(
            insert(ToolPolicy).values(org_id=org.ids["org"], tool="Bash", action="deny")
        )
    assert (await gate.screen(org.claims(), "Bash", ARGS)).action == "deny"
    assert (await call_row(engine, org, "c1")).status != "finished"


# -- the approval scope "session" and the direct mode of a coding agent -----------------------


async def decide_scope(
    gate: GateService,
    org: Org1,
    call_id: str = "c1",
    *,
    scope: Any = "session",
    run: str = "run",
    user: str = "mia",
    approve: bool = True,
) -> Any:
    return await gate.decide_with_scope(
        org_id=org.ids["org"],
        run_id=org.ids[run],
        call_id=call_id,
        user_id=org.ids[user],
        approve=approve,
        scope=scope,
    )


async def session_allows(engine: AsyncEngine, org: Org1) -> list[tuple[str, uuid.UUID, uuid.UUID]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(
                SessionToolAllow.tool, SessionToolAllow.user_id, SessionToolAllow.session_id
            ).where(SessionToolAllow.org_id == org.ids["org"])
        )
        return [(tool, user, session) for tool, user, session in rows]


async def test_a_session_approval_lets_the_tool_run_for_the_rest_of_the_session(
    engine: AsyncEngine, org: Org1
) -> None:
    gate = GateService(engine)
    await ask(gate, org)
    decision = await decide_scope(gate, org)
    assert (decision.status, decision.scope) == ("approved", "session")
    assert await session_allows(engine, org) == [("Bash", org.ids["mia"], org.ids["shared"])]
    verdict = await gate.check(org.claims(), "c2", "Bash", {"command": "make lint"})
    assert (verdict.action, verdict.step) == ("allow", GateStep.POLICY)
    assert "session" in verdict.reason
    # A new attended run of the session does not ask either.
    assert (await gate.check(org.claims(), "c3", "Bash", {"command": "make docs"})).action == (
        "allow"
    )
    entries = await audit(engine, org)
    approved = [d for a, _, d in entries if a == "approval.approved"]
    assert [d["scope"] for d in approved] == ["session"]
    assert [d["tool"] for a, _, d in entries if a == "tool_policy.session_allow"] == ["Bash"]
    # No user rule: the grant ends with the session.
    async with engine.connect() as connection:
        rules = (
            await connection.execute(
                select(func.count())
                .select_from(ToolPolicy)
                .where(ToolPolicy.org_id == org.ids["org"])
            )
        ).scalar_one()
    assert rules == 0


async def test_a_session_approval_keeps_the_rest_of_the_call_rules(
    engine: AsyncEngine, org: Org1
) -> None:
    gate = GateService(engine)
    await ask(gate, org)
    await decide_scope(gate, org)
    # Another tool still asks.
    assert (await gate.check(org.claims(), "c2", "Write", {"file_path": "a"})).action == "ask"
    assert len(await session_allows(engine, org)) == 1


async def test_a_session_approval_does_not_reach_another_session_or_user(
    engine: AsyncEngine, org: Org1
) -> None:
    gate = GateService(engine)
    await ask(gate, org)
    await decide_scope(gate, org)
    other_session = org.claims("secret-run", session_id=str(org.ids["secret"]))
    assert (await gate.check(other_session, "c2", "Bash", ARGS)).action == "ask"
    # The grant of the other session is its own rule.
    await ask(gate, org, "c4", run="secret-run")
    await decide_scope(gate, org, "c4", run="secret-run")
    assert (await gate.check(other_session, "c5", "Bash", ARGS)).action == "allow"
    assert {session for _, _, session in await session_allows(engine, org)} == {
        org.ids["shared"],
        org.ids["secret"],
    }


async def test_an_admin_session_approval_is_for_the_admin_only(
    engine: AsyncEngine, org: Org1
) -> None:
    gate = GateService(engine)
    await ask(gate, org)
    decision = await decide_scope(gate, org, user="admin")
    assert decision.scope == "session"
    # The run belongs to mia, and the rule is for the admin.
    assert (await gate.check(org.claims(), "c2", "Bash", ARGS)).action == "ask"


@pytest.mark.parametrize("action", ["deny", "ask"])
async def test_a_policy_rule_wins_over_a_session_approval(
    engine: AsyncEngine, org: Org1, action: str
) -> None:
    gate = GateService(engine)
    await ask(gate, org)
    await decide_scope(gate, org)
    await add_rule(engine, org, "Bash", action)
    assert (await gate.check(org.claims(), "c2", "Bash", ARGS)).action == action


async def test_a_user_deny_rule_wins_over_a_session_approval(
    engine: AsyncEngine, org: Org1
) -> None:
    gate = GateService(engine)
    await ask(gate, org)
    await decide_scope(gate, org)
    await add_rule(engine, org, "Bash", "deny", user="mia")
    assert (await gate.check(org.claims(), "c2", "Bash", ARGS)).action == "deny"


async def test_the_hardline_list_wins_over_a_session_approval(
    engine: AsyncEngine, org: Org1
) -> None:
    gate = GateService(engine)
    await ask(gate, org)
    await decide_scope(gate, org)
    verdict = await gate.check(org.claims(), "c2", "Bash", {"command": "rm -rf /"})
    assert (verdict.action, verdict.step) == ("deny", GateStep.HARDLINE)


async def test_a_rejection_or_once_writes_no_session_rule(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)
    await ask(gate, org)
    denied = await decide_scope(gate, org, approve=False)
    assert (denied.status, denied.scope) == ("rejected", "once")
    await ask(gate, org, "c2")
    once = await decide_scope(gate, org, "c2", scope="once")
    assert (once.status, once.scope) == ("approved", "once")
    assert await session_allows(engine, org) == []


async def test_a_tool_that_always_asks_gets_only_the_scope_once(
    engine: AsyncEngine, org: Org1
) -> None:
    gate = GateService(engine)
    await set_profile(engine, org, toolsets=["core", "github"], group_toolsets=["core", "github"])
    verdict = await gate.check(org.claims(), "c1", "repo_push_approval", {"path": "a"})
    assert verdict.action == "ask"
    decision = await decide_scope(gate, org, scope="session")
    assert decision.scope == "once"
    assert await session_allows(engine, org) == []


async def test_the_old_always_allow_flag_means_the_scope_always(
    engine: AsyncEngine, org: Org1
) -> None:
    gate = GateService(engine)
    await ask(gate, org)
    decision = await gate.decide_with_scope(
        org_id=org.ids["org"],
        run_id=org.ids["run"],
        call_id="c1",
        user_id=org.ids["mia"],
        approve=True,
        always_allow=True,
    )
    assert decision.scope == "always"
    assert await session_allows(engine, org) == []
    assert (await gate.check(org.claims(), "c2", "Bash", ARGS)).action == "allow"
    approved = [d for a, _, d in await audit(engine, org) if a == "approval.approved"]
    assert (approved[0]["scope"], approved[0]["always_allow"]) == ("always", True)


def test_the_scopes_of_a_tool() -> None:
    assert allowed_scopes("Bash", proposal=False) == ("once", "session", "always")
    assert allowed_scopes("Bash", proposal=True) == ("once",)
    assert allowed_scopes("repo_push_approval", proposal=False) == ("once",)
    assert allowed_scopes("db_main_execute", proposal=False) == ("once",)


async def test_a_session_rule_does_not_beat_plan_mode(engine: AsyncEngine, org: Org1) -> None:
    gate = GateService(engine)
    await ask(gate, org)
    await decide_scope(gate, org)
    async with engine.begin() as connection:
        await connection.execute(
            update(Session).where(Session.id == org.ids["shared"]).values(plan_mode=True)
        )
    verdict = await gate.check(org.claims(), "c2", "Bash", {"command": "make build"})
    assert (verdict.action, verdict.step) == ("deny", GateStep.PLAN)


# -- direct mode of a coding agent (Section 15.4) ---------------------------------------------

CODING_ARGS = {"agent": "claude_code", "repo": "", "prompt": "fix it", "open_pr": False}


async def direct_run(
    engine: AsyncEngine,
    org: Org1,
    run: str,
    *,
    runtime: str = "claude_code",
    coding_agent: str | None = None,
    user: str | None = "mia",
    unattended: bool = False,
    human: bool = True,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Run)
            .where(Run.id == org.ids[run])
            .values(
                runtime=runtime,
                coding_agent=coding_agent,
                triggered_by_user_id=org.ids[user] if user else None,
                unattended=unattended,
                human_origin=human,
            )
        )


async def coding_profile(engine: AsyncEngine, org: Org1) -> None:
    await set_profile(
        engine,
        org,
        toolsets=["core", "coding_agents"],
        group_toolsets=["core", "coding_agents"],
    )


async def test_direct_mode_in_a_private_session_of_the_owner_needs_no_approval(
    engine: AsyncEngine, org: Org1
) -> None:
    await coding_profile(engine, org)
    await direct_run(engine, org, "secret-run")
    gate = GateService(engine)
    claims = private_claims(org)
    verdict = await gate.check(claims, "c1", "coding_agent_run", CODING_ARGS)
    assert verdict.action == "allow"
    plan = await gate.check(claims, "c2", "coding_agent_plan", CODING_ARGS)
    assert plan.action == "allow"
    assert await approval(engine, org, "c1") is None
    # The same applies when the run has no starting user: the owner is the user.
    await direct_run(engine, org, "secret-run", user=None)
    assert (await gate.check(claims, "c3", "coding_agent_run", CODING_ARGS)).action == "allow"


async def test_direct_mode_keeps_deny_and_ask_rules(engine: AsyncEngine, org: Org1) -> None:
    await coding_profile(engine, org)
    await direct_run(engine, org, "secret-run")
    gate = GateService(engine)
    claims = private_claims(org)
    await add_rule(engine, org, "coding_agent_run", "ask")
    assert (await gate.check(claims, "c1", "coding_agent_run", CODING_ARGS)).action == "ask"
    await add_rule(engine, org, "coding_agent_run", "deny", user="mia")
    assert (await gate.check(claims, "c2", "coding_agent_run", CODING_ARGS)).action == "deny"


@pytest.mark.parametrize(
    ("runtime", "coding_agent", "unattended", "human"),
    [
        ("claude_agent_sdk", None, False, True),  # an explicit call of the sama agent
        ("claude_code", "claude_code", False, True),  # one message of "/claude <task>"
        ("claude_code", None, True, True),  # a job or a delegated run
        ("claude_code", None, False, False),  # an org API key that waits for approvals
    ],
)
async def test_other_runs_of_a_private_session_keep_the_normal_policy(
    engine: AsyncEngine,
    org: Org1,
    runtime: str,
    coding_agent: str | None,
    unattended: bool,
    human: bool,
) -> None:
    await coding_profile(engine, org)
    await direct_run(
        engine,
        org,
        "secret-run",
        runtime=runtime,
        coding_agent=coding_agent,
        unattended=unattended,
        human=human,
    )
    gate = GateService(engine)
    verdict = await gate.check(private_claims(org), "c1", "coding_agent_run", CODING_ARGS)
    assert verdict.action == ("deny" if unattended else "ask")


async def test_direct_mode_in_a_shared_session_asks_once_then_the_session_rule_allows(
    engine: AsyncEngine, org: Org1
) -> None:
    await coding_profile(engine, org)
    await direct_run(engine, org, "run")
    gate = GateService(engine)
    first = await gate.check(org.claims(), "c1", "coding_agent_run", CODING_ARGS)
    assert first.action == "ask"
    # The person approves "once", and the gate stores it as a session approval.
    decision = await decide_scope(gate, org, scope="once")
    assert (decision.status, decision.scope) == ("approved", "session")
    assert await session_allows(engine, org) == [
        ("coding_agent_run", org.ids["mia"], org.ids["shared"])
    ]
    again = {**CODING_ARGS, "prompt": "and now the tests"}
    assert (await gate.check(org.claims(), "c2", "coding_agent_run", again)).action == "allow"
    # Another member of the shared session asks once for himself.
    async with engine.begin() as connection:
        await connection.execute(
            update(Run).where(Run.id == org.ids["run"]).values(triggered_by_user_id=org.ids["max"])
        )
    assert (await gate.check(org.claims(), "c3", "coding_agent_run", again)).action == "ask"


async def test_an_explicit_call_of_the_sama_agent_keeps_the_scope_once(
    engine: AsyncEngine, org: Org1
) -> None:
    await coding_profile(engine, org)
    gate = GateService(engine)
    assert (await gate.check(org.claims(), "c1", "coding_agent_run", CODING_ARGS)).action == "ask"
    decision = await decide_scope(gate, org, scope="once")
    assert decision.scope == "once"
    assert await session_allows(engine, org) == []
    assert (await gate.check(org.claims(), "c2", "coding_agent_run", CODING_ARGS)).action == "ask"


async def test_a_session_grant_does_not_reach_an_unattended_run(
    engine: AsyncEngine, org: Org1
) -> None:
    """An unattended run (an API key with approvals ``deny``, a job, a delegated run) gets no
    session grant: ``ask`` stays ``deny``, and only a pre-approval of a job version allows."""
    gate = GateService(engine)
    await ask(gate, org)
    await decide_scope(gate, org)
    assert (await gate.check(org.claims(), "c2", "Bash", ARGS)).action == "allow"
    verdict = await gate.check(org.claims("night"), "c3", "Bash", ARGS)
    assert (verdict.action, verdict.step) == ("deny", GateStep.CONTEXT)
    assert await approval(engine, org, "c3") is None


async def test_the_direct_mode_rule_of_a_session_grant_does_not_reach_an_unattended_run(
    engine: AsyncEngine, org: Org1
) -> None:
    await coding_profile(engine, org)
    await direct_run(engine, org, "run")
    gate = GateService(engine)
    assert (await gate.check(org.claims(), "c1", "coding_agent_run", CODING_ARGS)).action == "ask"
    await decide_scope(gate, org, scope="once")
    assert (await gate.check(org.claims(), "c2", "coding_agent_run", CODING_ARGS)).action == "allow"
    await direct_run(engine, org, "run", unattended=True)
    verdict = await gate.check(org.claims(), "c3", "coding_agent_run", CODING_ARGS)
    assert verdict.action == "deny"
