"""The tool gate on the control plane (docs/architecture.md, Section 16.1).

``GateService`` loads the gate context of a run from Postgres, calls ``decide``, and records
each verdict: a ``tool_calls`` row, an ``audit_log`` row, and for ``ask`` an ``approvals`` row.
It also records human decisions and times out open approvals.

A call id is bound to one tool name and one arguments digest: the first check writes them to
the ``tool_calls`` row. A later check of the same call id with another tool or other arguments
gets ``deny`` and does not change the row. A transaction-level advisory lock on the run and the
call id puts concurrent checks of one call id in a sequence.

Authority is live (Section 5.1). Each check reads the run status, the access of the run user,
the run budget, and the policy rules again. ``reconfirm`` checks an approved call again before
the run resumes. An ``allow`` of an approved call makes its ``tool_calls`` row a ticket: when
``screen`` gives ``ask`` for a sama MCP tool call, the tool server runs the call only if it
takes one unused ticket for the same tool and arguments.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, Protocol, cast

from sqlalchemy import and_, case, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import ColumnElement

from sama.db.models import (
    ENDED_RUN_STATUSES,
    AgentProfile,
    Approval,
    AuditEntry,
    ReviewProposal,
    Run,
    Session,
    ToolCall,
    ToolPolicy,
)
from sama.gate.approvers import SHARED_REFUSAL, approvers_of, may_decide_shared
from sama.gate.decide import (
    JOB_TOOLSET,
    WORKFLOW_TOOLSET,
    Action,
    GateContext,
    GateStep,
    PolicyRule,
    Preapproval,
    Recorded,
    RuleLevel,
    ToolAccess,
    ToolsetSource,
    Verdict,
    always_asks,
    arguments_digest,
    canonical_name,
    context_policy,
    decide,
    decide_recorded,
    effective_call,
    effective_toolsets,
    frozen_toolsets,
    json_default,
    merged_catalog,
    run_limit,
    tools_of,
    with_frozen_profile_rules,
)
from sama.review.policy import REVIEW_TOOLSETS
from sama.scheduler.spec import job_of_run
from sama.tenancy.access import load_grants
from sama.tenancy.authority import budget_problem, load_authority, load_run_spend, run_user
from sama.tenancy.run_token import RunTokenClaims
from sama.vault.redact import default_redactor, redact
from sama.workflows.store import workflow_of_run

log = logging.getLogger("sama.gate")

APPROVAL_TTL: Final = timedelta(hours=24)
MAX_STORED_ARGUMENT_CHARS: Final = 8_000
"""Longer arguments are stored cut. The approval keeps the digest of the full arguments."""
EXPIRE_BATCH: Final = 500
_POLICY_KEY: Final = "uq_tool_policies_org_id_agent_profile_id_user_id_tool"


class ApprovalError(Exception):
    """The decision cannot be recorded. The message is safe to show to the user."""


class ApprovalExpiredError(ApprovalError):
    """The approval timed out. It counts as ``deny``."""


@dataclass(frozen=True, slots=True)
class _Ref:
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    session_id: uuid.UUID
    run_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class _Call:
    """The identity of a recorded tool call."""

    tool_name: str
    arguments_digest: str | None


@dataclass(frozen=True, slots=True)
class _Run:
    ref: _Ref
    context: GateContext
    call: _Call | None = None


def _names(value: Any) -> list[str]:
    """The toolset names of a JSON column. A value that is not a list has no name."""
    return [str(item) for item in value] if isinstance(value, list) else []


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _ref_of(claims: RunTokenClaims) -> _Ref | None:
    try:
        return _Ref(
            uuid.UUID(claims.org_id),
            uuid.UUID(claims.workspace_id),
            uuid.UUID(claims.session_id),
            uuid.UUID(claims.run_id),
        )
    except ValueError:
        return None


def stored_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """The arguments to store, with secrets redacted (Section 16.3). Long arguments are cut,
    so one call cannot fill the table. The digest is of the full arguments, not of these."""
    value = default_redactor().json(json.loads(json.dumps(arguments, default=json_default)))
    text = json.dumps(value, ensure_ascii=False)
    if len(text) <= MAX_STORED_ARGUMENT_CHARS:
        return cast(dict[str, Any], value)
    return {"_cut": True, "preview": text[:MAX_STORED_ARGUMENT_CHARS]}


class RunToolSource(Protocol):
    """Tools that depend on the run, for example the tools of the remote MCP servers that the
    run may use (Section 10.3). The gate reads them at each check, in its transaction, so a
    new tool is usable at once."""

    async def run_tools(
        self,
        connection: AsyncConnection,
        *,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID | None,
        shared: bool,
        toolsets: frozenset[str],
    ) -> tuple[Mapping[str, frozenset[str]], Mapping[str, ToolAccess]]: ...


class RunRuleSource(Protocol):
    """Policy rules that depend on the run, for example the policy of each custom tool that
    the run may use (Section 10.8). The gate reads them at each check, in its transaction."""

    async def run_rules(
        self,
        connection: AsyncConnection,
        *,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID | None,
        shared: bool,
        toolsets: frozenset[str],
    ) -> Sequence[PolicyRule]: ...


Escalation = Callable[[str, str, str, Mapping[str, Any]], str | None]
"""``(org_id, run_id, tool name, arguments)`` gives a reason when the call needs a person even
with an ``allow`` rule, or ``None``. It runs in the event loop, so it must be quick."""


class GateService:
    """The real ``ToolGate`` of sama. One instance serves all orgs: every query is scoped by
    the run token claims."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        approval_ttl: timedelta = APPROVAL_TTL,
        clock: Callable[[], datetime] = _utcnow,
        extra_toolsets: ToolsetSource | None = None,
        run_tools: RunToolSource | Sequence[RunToolSource] | None = None,
        run_rules: RunRuleSource | Sequence[RunRuleSource] | None = None,
        escalation: Escalation | None = None,
    ) -> None:
        """``extra_toolsets`` adds toolsets to the fixed table, for example the deferred tools
        of the tool server (Section 10.3). It is read at each check. ``run_tools`` adds the
        tools that depend on the run, and ``run_rules`` adds the policy rules that depend on
        the run (the custom tools, Section 10.8). ``escalation`` gives a reason when a call
        needs a person even with an ``allow`` rule (``decide``, ``escalate``); the control
        plane sets it to the sensitive actions of the browser (Section 14.2.2)."""
        if approval_ttl <= timedelta(0):
            raise ValueError("approval_ttl must be more than 0")
        self._engine = engine
        self._extra_toolsets = extra_toolsets
        self._run_tools: tuple[RunToolSource, ...] = _sources(run_tools)
        self._run_rules: tuple[RunRuleSource, ...] = _sources(run_rules)
        self.escalation = escalation
        self._ttl = approval_ttl
        self._clock = clock

    # -- verdicts ---------------------------------------------------------------------------

    async def check(
        self,
        claims: RunTokenClaims,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        child: bool = False,
    ) -> Verdict:
        """The verdict for one tool call id. Each step is recorded.

        ``child`` is true for a call of a sub-agent. A sub-agent cannot pause the run, so an
        ``ask`` becomes ``deny`` before the gate records anything: no pending approval and no
        ``approval.requested`` entry for a call that no human can answer."""
        if not call_id:
            return Verdict("deny", GateStep.TOOLSET, "the tool call has no id")
        # A ``tool_call`` is recorded as the deferred tool that it names (Section 16.1).
        original = arguments
        name, effective = effective_call(tool_name, original)
        digest = arguments_digest(effective)
        ref = _ref_of(claims)
        async with self._engine.begin() as connection:
            run = None if ref is None else await self._load(connection, ref, claims, call_id)
            if run is None:
                log.warning("gate: unknown run org=%s run=%s", claims.org_id, claims.run_id)
                return Verdict("deny", GateStep.TOOLSET, "the run is unknown or ended")
            if run.call is not None and run.call != _Call(name, digest):
                verdict = Verdict("deny", GateStep.RECORDED, "the call id belongs to another call")
                await self._audit(
                    connection, run.ref, "tool_gate.verdict", call_id, _details(name, verdict)
                )
            else:
                escalate = self._escalate(claims, name, effective)
                verdict = decide(run.context, tool_name, original, escalate=escalate)
                if child and verdict.action == "ask":
                    verdict = Verdict(
                        "deny", GateStep.POLICY, "a sub-agent cannot ask for an approval"
                    )
                await self._record(connection, run, call_id, name, effective, digest, verdict)
        log.info(
            "gate org=%s run=%s call=%s tool=%s action=%s step=%s",
            claims.org_id,
            claims.run_id,
            call_id,
            name,
            verdict.action,
            verdict.step,
        )
        return verdict

    def _escalate(
        self, claims: RunTokenClaims, name: str, arguments: Mapping[str, Any]
    ) -> str | None:
        if self.escalation is None:
            return None
        try:
            return self.escalation(claims.org_id, claims.run_id, name, arguments)
        except Exception:
            # Fail closed: a check that fails asks the user.
            log.exception("gate: the escalation check failed")
            return "the sensitivity check failed"

    async def screen(
        self, claims: RunTokenClaims, tool_name: str, arguments: Mapping[str, Any]
    ) -> Verdict:
        """The verdict without a call id, for the sama MCP server. The server refuses
        ``deny``. A ``deny`` is in the audit log.

        ``ask`` becomes ``allow`` only with a ticket: an approved call of this run with the same
        tool and arguments that ``reconfirm`` allowed and no server call used. The ticket is
        used up. Without a ticket, ``ask`` becomes ``deny``, so a process in the sandbox cannot
        call an ``ask`` tool with the run token alone."""
        name, effective = effective_call(tool_name, arguments)
        ref = _ref_of(claims)
        async with self._engine.begin() as connection:
            run = None if ref is None else await self._load(connection, ref, claims, None)
            if run is None:
                return Verdict("deny", GateStep.TOOLSET, "the run is unknown or ended")
            escalate = self._escalate(claims, name, effective)
            verdict = decide(run.context, tool_name, arguments, escalate=escalate)
            if verdict.action == "allow" and self.escalation is not None:
                # An approved call that now runs under an allow rule (the user chose "always
                # allow") uses up its ticket here, so a later call with the same arguments
                # cannot use the old approval (Section 14.2.2).
                await self._take_ticket(connection, run.ref, name, arguments_digest(effective))
            if verdict.step is GateStep.PLAN:
                # Plan mode does not stop a call that a person approved (Section 15.5): with
                # a ticket of this exact call, the verdict without plan mode applies.
                unplanned = decide(
                    replace(run.context, plan_mode=False), tool_name, arguments, escalate=escalate
                )
                if unplanned.action != "deny" and await self._take_ticket(
                    connection, run.ref, name, arguments_digest(effective)
                ):
                    verdict = Verdict("allow", GateStep.RECORDED, "the call has an approved ticket")
            if verdict.action == "ask":
                digest = arguments_digest(effective)
                if await self._take_ticket(connection, run.ref, name, digest):
                    verdict = Verdict("allow", GateStep.RECORDED, "the call has an approved ticket")
                else:
                    verdict = Verdict(
                        "deny", GateStep.RECORDED, "the call needs an approval of the tool gate"
                    )
            if verdict.action == "deny":
                await self._audit(
                    connection, run.ref, "tool_gate.screen", None, _details(name, verdict)
                )
        return verdict

    async def inner(
        self, claims: RunTokenClaims, tool_name: str, arguments: Mapping[str, Any]
    ) -> Verdict:
        """The verdict for one step of a sama sub-agent tool, for example a click of
        ``browser_task`` that the gate judges as ``browser_act`` (Section 14.1, M5.6). The
        tool server calls it after the gate allowed the call of the sub-agent tool.

        It is ``decide`` with the live context of the run: the authority, the toolsets, the
        hardline list, and the policy rules. It records no tool call and uses no ticket, because
        the step has no call id of the runtime. An ``ask`` stays ``ask``: the caller decides if
        the approval of the sub-agent call covers it. The unattended step of ``decide`` does
        not apply to the step: the sub-agent call passed it already (a pre-approval or an allow
        rule of the job), and that decision covers the ``ask`` of its steps. A ``deny`` is in
        the audit log as ``tool_gate.inner``."""
        name, _effective = effective_call(tool_name, arguments)
        ref = _ref_of(claims)
        async with self._engine.begin() as connection:
            run = None if ref is None else await self._load(connection, ref, claims, None)
            if run is None:
                return Verdict("deny", GateStep.TOOLSET, "the run is unknown or ended")
            context = dataclasses.replace(run.context, unattended=False, preapproved=())
            verdict = decide(context, tool_name, arguments)
            if verdict.action == "deny":
                await self._audit(
                    connection, run.ref, "tool_gate.inner", None, _details(name, verdict)
                )
        return verdict

    async def visible_tools(self, claims: RunTokenClaims) -> frozenset[str]:
        """The tools that this run can use now: the tools of its toolsets that no ``deny`` rule
        blocks. It is empty for a run that has no authority. ``tool_search`` shows only these
        tools, so a run does not learn the names of tools that it cannot call. A tool that is
        visible can still get ``ask`` when the run calls it."""
        ref = _ref_of(claims)
        async with self._engine.begin() as connection:
            run = None if ref is None else await self._load(connection, ref, claims, None)
        if run is None or run.context.revoked is not None:
            return frozenset()
        context = run.context
        return frozenset(
            name
            for name in tools_of(context.toolsets, context.catalog)
            if context_policy(context, name).action != "deny"
        )

    async def reconfirm(self, *, org_id: uuid.UUID, run_id: uuid.UUID, call_id: str) -> Verdict:
        """The verdict again for an approved call, before the run resumes (Section 16.1). The
        worker calls it. A newer deny rule, a lost membership, a spent budget, or an ended run
        gives ``deny``. On ``allow`` the ``tool_calls`` row of the call is the ticket of the call
        for the tool server."""
        async with self._engine.begin() as connection:
            found = (
                await connection.execute(
                    select(Run.session_id, Session.workspace_id)
                    .join(Session, and_(Session.org_id == Run.org_id, Session.id == Run.session_id))
                    .where(Run.org_id == org_id, Run.id == run_id)
                )
            ).one_or_none()
            if found is None:
                return Verdict("deny", GateStep.AUTHORITY, "the run is unknown")
            ref = _Ref(org_id, found.workspace_id, found.session_id, run_id)
            run = await self._load(connection, ref, None, call_id)
            if run is None:
                return Verdict("deny", GateStep.AUTHORITY, "the run is unknown or ended")
            recorded = run.context.recorded
            if recorded is None:
                verdict = Verdict("deny", GateStep.RECORDED, "the call has no approval")
                name = ""
            else:
                name = canonical_name(recorded.tool_name)
                if recorded.status == "approved":
                    verdict = decide_recorded(run.context, name, recorded.arguments_digest)
                else:
                    verdict = Verdict(
                        "deny", GateStep.RECORDED, f"the approval is {recorded.status}"
                    )
                await self._ticket(connection, ref, call_id, name, recorded, verdict)
            await self._audit(
                connection, ref, "tool_gate.reconfirm", call_id, _details(name, verdict)
            )
        log.info(
            "gate reconfirm org=%s run=%s call=%s action=%s step=%s",
            org_id,
            run_id,
            call_id,
            verdict.action,
            verdict.step,
        )
        return verdict

    async def _ticket(
        self,
        connection: AsyncConnection,
        ref: _Ref,
        call_id: str,
        name: str,
        recorded: Recorded,
        verdict: Verdict,
    ) -> None:
        """Writes the verdict to the ``tool_calls`` row of the call. The runtime can report an
        approval that the gate did not check, so the row is made if it is not there."""
        status = "denied" if verdict.action == "deny" else "started"
        stored = (
            await connection.execute(
                select(Approval.arguments).where(
                    Approval.org_id == ref.org_id,
                    Approval.run_id == ref.run_id,
                    Approval.call_id == call_id,
                )
            )
        ).scalar_one()
        statement = pg_insert(ToolCall).values(
            org_id=ref.org_id,
            session_id=ref.session_id,
            run_id=ref.run_id,
            call_id=call_id,
            tool_name=name,
            arguments=stored,
            arguments_digest=recorded.arguments_digest,
            decision=verdict.action,
            status=status,
        )
        await connection.execute(
            statement.on_conflict_do_update(
                index_elements=[ToolCall.run_id, ToolCall.call_id],
                set_={"decision": verdict.action, "status": _keep_used(status)},
            )
        )

    async def _take_ticket(
        self, connection: AsyncConnection, ref: _Ref, name: str, digest: str
    ) -> bool:
        """Uses up one ticket for the call, if the run has one."""
        ticket = (
            select(ToolCall.id)
            .join(
                Approval,
                and_(
                    Approval.org_id == ToolCall.org_id,
                    Approval.run_id == ToolCall.run_id,
                    Approval.call_id == ToolCall.call_id,
                ),
            )
            .where(
                ToolCall.org_id == ref.org_id,
                ToolCall.run_id == ref.run_id,
                ToolCall.tool_name == name,
                ToolCall.arguments_digest == digest,
                ToolCall.decision == "allow",
                ToolCall.status == "started",
                Approval.status == "approved",
            )
            .order_by(ToolCall.created_at)
            .limit(1)
            .with_for_update(of=ToolCall, skip_locked=True)
            .scalar_subquery()
        )
        used = await connection.execute(
            update(ToolCall)
            .where(ToolCall.id == ticket)
            .values(status="finished", finished_at=func.now())
            .returning(ToolCall.call_id)
        )
        call_id = used.scalar_one_or_none()
        if call_id is None:
            return False
        await self._audit(connection, ref, "tool_gate.ticket", call_id, {"tool": name})
        return True

    async def _load(
        self,
        connection: AsyncConnection,
        ref: _Ref,
        claims: RunTokenClaims | None,
        call_id: str | None,
    ) -> _Run | None:
        org_id, run_id = ref.org_id, ref.run_id
        if call_id:
            key = f"sama.gate:{run_id}:{call_id}"
            await connection.execute(
                select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0)))
            )
        row = (
            await connection.execute(
                select(
                    Run.agent_profile_id,
                    Run.triggered_by_user_id,
                    Run.unattended,
                    Run.kind,
                    Run.toolsets.label("run_toolsets"),
                    AgentProfile.toolsets,
                    AgentProfile.group_toolsets,
                    Session.audience,
                    Session.owner_user_id,
                    Session.lineage,
                    Session.profile_snapshot,
                    Session.plan_mode,
                )
                .join(
                    AgentProfile,
                    and_(
                        AgentProfile.org_id == Run.org_id, AgentProfile.id == Run.agent_profile_id
                    ),
                )
                .join(Session, and_(Session.org_id == Run.org_id, Session.id == Run.session_id))
                .where(
                    Run.org_id == org_id,
                    Run.id == run_id,
                    Run.session_id == ref.session_id,
                    Session.workspace_id == ref.workspace_id,
                    # A runner of an ended run (for example after a worker crash) gets no tool.
                    Run.status.not_in(ENDED_RUN_STATUSES),
                )
            )
        ).one_or_none()
        if row is None:
            return None
        # The same run user as the live authority, so `reconfirm` (with no token) loads the
        # same user rules as `check`. The token user is the last fallback.
        user_id = run_user(row.triggered_by_user_id, row.audience, row.owner_user_id)
        if user_id is None and claims is not None and claims.user_id:
            user_id = uuid.UUID(claims.user_id)
        # A session uses only the toolsets that its snapshot also has (Section 11.4).
        snapshot = row.profile_snapshot
        toolsets = effective_toolsets(
            frozen_toolsets(_names(row.toolsets), snapshot, "toolsets"),
            frozen_toolsets(_names(row.group_toolsets), snapshot, "group_toolsets"),
            audience=row.audience,
            kind=row.kind,
            lineage=row.lineage,
            run_toolsets=run_limit(row.run_toolsets),
        )
        if row.kind == "review":
            # A background review has the memory and skill tools and no other tool (Section 9.4).
            toolsets &= REVIEW_TOOLSETS
        preapproved: tuple[Preapproval, ...] = ()
        job = await job_of_run(connection, org_id, run_id)
        if job is not None:
            # A job run cannot create or change jobs (Section 12.1). Its pre-approvals apply
            # only when a human approved this exact version of the job.
            toolsets -= {JOB_TOOLSET}
            preapproved = job.preapproved
        workflow = await workflow_of_run(connection, org_id, run_id)
        if workflow is not None:
            # The run of an agent step cannot create, change, or start workflows or jobs
            # (Section 12.3). Its pre-approvals apply only when a human approved this exact
            # version of the workflow.
            toolsets -= {JOB_TOOLSET, WORKFLOW_TOOLSET}
            preapproved = workflow.preapproved
        extra = dict(self._extra_toolsets.toolsets()) if self._extra_toolsets is not None else {}
        access = dict(self._extra_toolsets.access()) if self._extra_toolsets is not None else {}
        who: dict[str, Any] = {
            "org_id": org_id,
            "workspace_id": ref.workspace_id,
            "user_id": user_id,
            "shared": row.audience != "private",
            "toolsets": toolsets,
        }
        fixed_access = access
        for source in self._run_tools:
            more, more_access = await source.run_tools(connection, **who)
            for name, tools in more.items():
                extra[name] = extra.get(name, frozenset()) | tools
            access = {**more_access, **access}
        # A fixed tool keeps its own access.
        access = {**access, **fixed_access}
        rules = await self._rules(connection, org_id, row.agent_profile_id, user_id)
        for rule_source in self._run_rules:
            rules = (*rules, *await rule_source.run_rules(connection, **who))
        context = GateContext(
            toolsets=toolsets,
            rules=rules,
            frozen_rules=with_frozen_profile_rules(rules, snapshot),
            unattended=bool(row.unattended),
            shared=row.audience == "shared" and row.kind != "review",
            recorded=(await self._recorded(connection, ref, call_id) if call_id else None),
            revoked=await self._revoked(connection, org_id, run_id),
            preapproved=preapproved,
            catalog=merged_catalog(extra),
            access=access,
            # A review run reads the transcript only; plan mode is for the chat runs.
            plan_mode=bool(row.plan_mode) and row.kind != "review",
        )
        call = None
        if call_id:
            found = (
                await connection.execute(
                    select(ToolCall.tool_name, ToolCall.arguments_digest).where(
                        ToolCall.org_id == org_id,
                        ToolCall.run_id == run_id,
                        ToolCall.call_id == call_id,
                    )
                )
            ).one_or_none()
            if found is not None:
                call = _Call(found.tool_name, found.arguments_digest)
        return _Run(ref, context, call)

    async def _revoked(
        self, connection: AsyncConnection, org_id: uuid.UUID, run_id: uuid.UUID
    ) -> str | None:
        """Why the run has no authority now: its user lost access, or it spent its budget."""
        authority = await load_authority(connection, org_id, run_id)
        if authority.problem is not None:
            return authority.problem
        if authority.max_tokens is None and authority.max_cost_usd is None:
            return None
        spend = await load_run_spend(connection, org_id, run_id)
        return budget_problem(
            spend.tokens, spend.cost_usd, authority.max_tokens, authority.max_cost_usd
        )

    async def _rules(
        self,
        connection: AsyncConnection,
        org_id: uuid.UUID,
        profile_id: uuid.UUID,
        user_id: uuid.UUID | None,
    ) -> tuple[PolicyRule, ...]:
        user_match: ColumnElement[bool] = ToolPolicy.user_id.is_(None)
        if user_id is not None:
            user_match = or_(user_match, ToolPolicy.user_id == user_id)
        rows = await connection.execute(
            select(
                ToolPolicy.tool, ToolPolicy.action, ToolPolicy.agent_profile_id, ToolPolicy.user_id
            ).where(
                ToolPolicy.org_id == org_id,
                or_(
                    ToolPolicy.agent_profile_id.is_(None), ToolPolicy.agent_profile_id == profile_id
                ),
                user_match,
            )
        )
        rules: list[PolicyRule] = []
        for tool, action, profile, user in rows:
            level = (
                RuleLevel.USER
                if user is not None
                else RuleLevel.PROFILE
                if profile is not None
                else RuleLevel.ORG
            )
            rules.append(PolicyRule(tool=tool, action=cast(Action, action), level=level))
        return tuple(rules)

    async def _recorded(
        self,
        connection: AsyncConnection,
        ref: _Ref,
        call_id: str,
    ) -> Recorded | None:
        row = (
            await connection.execute(
                select(
                    Approval.id,
                    Approval.status,
                    Approval.tool_name,
                    Approval.arguments_digest,
                    Approval.expires_at,
                )
                .where(
                    Approval.org_id == ref.org_id,
                    Approval.run_id == ref.run_id,
                    Approval.call_id == call_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            return None
        status = row.status
        if status == "pending" and row.expires_at <= self._clock():
            status = "expired"
            await self._expire_one(connection, row.id, ref, call_id)
        return Recorded(
            status=cast(Literal["pending", "approved", "rejected", "expired"], status),
            tool_name=row.tool_name,
            arguments_digest=row.arguments_digest,
        )

    async def _record(
        self,
        connection: AsyncConnection,
        run: _Run,
        call_id: str,
        name: str,
        arguments: Mapping[str, Any],
        digest: str,
        verdict: Verdict,
    ) -> None:
        ref = run.ref
        stored = stored_arguments(arguments)
        status = "denied" if verdict.action == "deny" else "started"
        statement = pg_insert(ToolCall).values(
            org_id=ref.org_id,
            session_id=ref.session_id,
            run_id=ref.run_id,
            call_id=call_id,
            tool_name=name,
            arguments=stored,
            arguments_digest=digest,
            decision=verdict.action,
            status=status,
        )
        await connection.execute(
            statement.on_conflict_do_update(
                index_elements=[ToolCall.run_id, ToolCall.call_id],
                # A used ticket stays used, so a check again cannot make a new ticket.
                set_={"decision": verdict.action, "status": _keep_used(status)},
            )
        )
        await self._audit(connection, ref, "tool_gate.verdict", call_id, _details(name, verdict))
        if verdict.action == "ask" and run.context.recorded is None:
            now = self._clock()
            created = await connection.execute(
                pg_insert(Approval)
                .values(
                    org_id=ref.org_id,
                    session_id=ref.session_id,
                    run_id=ref.run_id,
                    call_id=call_id,
                    tool_name=name,
                    arguments=stored,
                    arguments_digest=digest,
                    reason=verdict.reason,
                    created_at=now,
                    expires_at=now + self._ttl,
                )
                .on_conflict_do_nothing(index_elements=[Approval.run_id, Approval.call_id])
                .returning(Approval.id)
            )
            if created.first() is not None:
                await self._audit(
                    connection,
                    ref,
                    "approval.requested",
                    call_id,
                    {"tool": name, "expires_at": (now + self._ttl).isoformat()},
                )

    # -- approvals --------------------------------------------------------------------------

    async def decide_approval(
        self,
        *,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        call_id: str,
        user_id: uuid.UUID,
        approve: bool,
        always_allow: bool = False,
        reason: str | None = None,
    ) -> str:
        """Records a human decision and returns the new status. Raises ``ApprovalError``.

        Only a user who can read the session decides. In a shared session, only the user who
        started the run, a workspace admin, or an approver of the profile decides.
        ``always_allow`` with ``approve`` also writes a user policy rule for this tool and the
        profile of the run.
        """
        reason = redact(reason) if reason else reason
        status = "expired"
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    select(
                        Approval.id,
                        Approval.session_id,
                        Approval.status,
                        Approval.tool_name,
                        Approval.expires_at,
                        Session.workspace_id,
                        Session.audience,
                        Session.owner_user_id,
                        Run.agent_profile_id,
                        Run.triggered_by_user_id,
                        AgentProfile.policy,
                    )
                    .join(
                        Session,
                        and_(Session.org_id == Approval.org_id, Session.id == Approval.session_id),
                    )
                    .join(Run, and_(Run.org_id == Approval.org_id, Run.id == Approval.run_id))
                    .join(
                        AgentProfile,
                        and_(
                            AgentProfile.org_id == Run.org_id,
                            AgentProfile.id == Run.agent_profile_id,
                        ),
                    )
                    .where(
                        Approval.org_id == org_id,
                        Approval.run_id == run_id,
                        Approval.call_id == call_id,
                    )
                    .with_for_update(of=Approval)
                )
            ).one_or_none()
            if row is None:
                raise ApprovalError("the approval does not exist")
            grants = await load_grants(connection, org_id, user_id)
            proposed = (
                await connection.execute(
                    select(ReviewProposal.scope).where(
                        ReviewProposal.org_id == org_id, ReviewProposal.approval_id == row.id
                    )
                )
            ).scalar_one_or_none()
            if proposed is None:
                if not grants.sees_session(row.workspace_id, row.audience, row.owner_user_id):
                    raise ApprovalError("this user cannot decide the approval")
                # In a shared session, each member sees the session, but only the requester
                # and the admins decide, unless the profile widens the set (Section 11.3).
                if row.audience == "shared" and not may_decide_shared(
                    grants,
                    row.workspace_id,
                    requester=row.triggered_by_user_id,
                    approvers=approvers_of(row.policy),
                ):
                    raise ApprovalError(SHARED_REFUSAL)
            else:
                # A proposal of a background review or a chat run changes shared memory, a
                # shared skill, or a soul (Section 9.4). The role decides, and the access to the
                # session does not: the owner of a private session may lack the role, and an
                # admin may not read the private session. The admin sees the redacted proposal
                # only.
                allowed = (
                    grants.org_owner
                    if proposed == "org"
                    else grants.admins_workspace(row.workspace_id)
                )
                if not allowed:
                    raise ApprovalError(
                        "only the org owner can decide this proposal"
                        if proposed == "org"
                        else "only a workspace admin or the org owner can decide this proposal"
                    )
                always_allow = False
            if row.status != "pending":
                raise ApprovalError(f"the approval is already {row.status}")
            now = self._clock()
            run = _Ref(org_id, row.workspace_id, row.session_id, run_id)
            if row.expires_at <= now:
                await self._expire_one(connection, row.id, run, call_id)
            else:
                status = "approved" if approve else "rejected"
                await connection.execute(
                    update(Approval)
                    .where(Approval.id == row.id)
                    .values(
                        status=status,
                        decided_by_user_id=user_id,
                        decided_at=now,
                        decision_reason=reason,
                    )
                )
                await self._audit(
                    connection,
                    run,
                    f"approval.{status}",
                    call_id,
                    {"tool": row.tool_name, "reason": reason, "always_allow": always_allow},
                    user_id=user_id,
                )
                if approve and always_allow and not always_asks(row.tool_name):
                    await self._always_allow(
                        connection, run, row.agent_profile_id, user_id, row.tool_name, call_id
                    )
        if status == "expired":
            raise ApprovalExpiredError("the approval timed out, so it counts as deny")
        return status

    async def _always_allow(
        self,
        connection: AsyncConnection,
        run: _Ref,
        profile_id: uuid.UUID,
        user_id: uuid.UUID,
        tool: str,
        call_id: str,
    ) -> None:
        statement = pg_insert(ToolPolicy).values(
            org_id=run.org_id,
            agent_profile_id=profile_id,
            user_id=user_id,
            tool=tool,
            action="allow",
            created_by_user_id=user_id,
        )
        await connection.execute(
            statement.on_conflict_do_update(
                constraint=_POLICY_KEY, set_={"action": "allow", "updated_at": func.now()}
            )
        )
        await self._audit(
            connection,
            run,
            "tool_policy.set",
            call_id,
            {"tool": tool, "action": "allow", "level": "user", "profile_id": str(profile_id)},
            user_id=user_id,
        )

    async def expire(self) -> int:
        """Times out open approvals. Returns how many. The worker calls it from time to time;
        the gate also times out an approval when it reads it."""
        now = self._clock()
        async with self._engine.begin() as connection:
            due = (
                select(Approval.id)
                .where(Approval.status == "pending", Approval.expires_at <= now)
                .limit(EXPIRE_BATCH)
                .with_for_update(skip_locked=True)
                .scalar_subquery()
            )
            rows = (
                await connection.execute(
                    update(Approval)
                    .where(
                        Approval.id.in_(due),
                        Session.org_id == Approval.org_id,
                        Session.id == Approval.session_id,
                    )
                    .values(status="expired", decided_at=now)
                    .returning(
                        Approval.org_id,
                        Session.workspace_id,
                        Approval.session_id,
                        Approval.run_id,
                        Approval.call_id,
                    )
                )
            ).all()
            for org_id, workspace_id, session_id, run_id, call_id in rows:
                ref = _Ref(org_id, workspace_id, session_id, run_id)
                await self._audit(connection, ref, "approval.expired", call_id, {})
        return len(rows)

    async def _expire_one(
        self,
        connection: AsyncConnection,
        approval_id: uuid.UUID,
        ref: _Ref,
        call_id: str,
    ) -> None:
        """Times out one open approval. The caller holds the row lock."""
        await connection.execute(
            update(Approval)
            .where(Approval.id == approval_id)
            .values(status="expired", decided_at=self._clock())
        )
        await self._audit(connection, ref, "approval.expired", call_id, {})

    # -- audit ------------------------------------------------------------------------------

    async def _audit(
        self,
        connection: AsyncConnection,
        run: _Ref,
        action: str,
        call_id: str | None,
        details: dict[str, Any],
        *,
        user_id: uuid.UUID | None = None,
    ) -> None:
        await connection.execute(
            insert(AuditEntry).values(
                org_id=run.org_id,
                workspace_id=run.workspace_id,
                actor_kind="user" if user_id is not None else "system",
                actor_user_id=user_id,
                action=action,
                target_type="tool_call" if call_id is not None else None,
                target_id=call_id,
                run_id=run.run_id,
                details=default_redactor().json({"session_id": str(run.session_id), **details}),
            )
        )


def _keep_used(status: str) -> Any:
    """The new status of a ``tool_calls`` row. A used ticket stays used."""
    return case((ToolCall.status == "finished", "finished"), else_=status)


def _details(name: str, verdict: Verdict) -> dict[str, Any]:
    return {
        "tool": name,
        "action": verdict.action,
        "step": str(verdict.step),
        "reason": verdict.reason,
    }


__all__ = [
    "APPROVAL_TTL",
    "ApprovalError",
    "ApprovalExpiredError",
    "GateService",
]


def _sources[T](value: T | Sequence[T] | None) -> tuple[T, ...]:
    if value is None:
        return ()
    if isinstance(value, Sequence):
        return tuple(value)
    return (value,)
