"""The tool gate service with Postgres (docs/architecture.md, Section 16.1).

Each test makes its own org, so the audit rows and approvals of the test do not change what
the shared seed of the RLS tests reads.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import (
    AgentProfile,
    Approval,
    AuditEntry,
    Membership,
    Org,
    Run,
    Session,
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
    assert verdict.reason == "the run is unknown"
    assert await audit(engine, org) == []


async def test_a_call_needs_an_id(engine: AsyncEngine, org: Org1) -> None:
    assert (await GateService(engine).check(org.claims(), "", "Read", {})).action == "deny"


async def test_the_profile_toolsets_limit_the_tools(engine: AsyncEngine, org: Org1) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == org.ids["profile"])
            .values(toolsets=["web"])
        )
    verdict = await GateService(engine).check(org.claims(), "c1", "Read", {})
    assert (verdict.action, verdict.step) == ("deny", GateStep.TOOLSET)


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
    assert (await gate.screen(org.claims(), "Bash", ARGS)).action == "ask"
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
        user_id=org.ids["max"],
        approve=True,
        reason="fine",
    )
    assert status == "approved"
    row = await approval(engine, org, "c1")
    assert row.decided_by_user_id == org.ids["max"]
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
        ("run", "max", True),
        ("run", "admin", True),
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
        with pytest.raises(ApprovalError, match="cannot decide"):
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
        user_id=org.ids["max"],
        approve=True,
        always_allow=True,
    )
    # The run belongs to mia, and the rule is for max.
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
