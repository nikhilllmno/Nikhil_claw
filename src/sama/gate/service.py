"""The tool gate on the control plane (docs/architecture.md, Section 16.1).

``GateService`` loads the gate context of a run from Postgres, calls ``decide``, and records
each verdict: a ``tool_calls`` row, an ``audit_log`` row, and for ``ask`` an ``approvals`` row.
It also records human decisions and times out open approvals.

A call id is bound to one tool name and one arguments digest: the first check writes them to
the ``tool_calls`` row. A later check of the same call id with another tool or other arguments
gets ``deny`` and does not change the row. A transaction-level advisory lock on the run and the
call id puts concurrent checks of one call id in a sequence.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, cast

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import ColumnElement

from sama.db.models import (
    ENDED_RUN_STATUSES,
    AgentProfile,
    Approval,
    AuditEntry,
    Run,
    Session,
    ToolCall,
    ToolPolicy,
)
from sama.gate.decide import (
    Action,
    GateContext,
    GateStep,
    PolicyRule,
    Recorded,
    RuleLevel,
    Verdict,
    arguments_digest,
    canonical_name,
    decide,
    json_default,
)
from sama.tenancy.access import load_grants
from sama.tenancy.run_token import RunTokenClaims
from sama.vault.redact import default_redactor, redact

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


def _utcnow() -> datetime:
    return datetime.now(UTC)


def stored_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """The arguments to store, with secrets redacted (Section 16.3). Long arguments are cut,
    so one call cannot fill the table. The digest is of the full arguments, not of these."""
    value = default_redactor().json(json.loads(json.dumps(arguments, default=json_default)))
    text = json.dumps(value, ensure_ascii=False)
    if len(text) <= MAX_STORED_ARGUMENT_CHARS:
        return cast(dict[str, Any], value)
    return {"_cut": True, "preview": text[:MAX_STORED_ARGUMENT_CHARS]}


class GateService:
    """The real ``ToolGate`` of sama. One instance serves all orgs: every query is scoped by
    the run token claims."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        approval_ttl: timedelta = APPROVAL_TTL,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if approval_ttl <= timedelta(0):
            raise ValueError("approval_ttl must be more than 0")
        self._engine = engine
        self._ttl = approval_ttl
        self._clock = clock

    # -- verdicts ---------------------------------------------------------------------------

    async def check(
        self,
        claims: RunTokenClaims,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> Verdict:
        """The verdict for one tool call id. Each step is recorded."""
        if not call_id:
            return Verdict("deny", GateStep.TOOLSET, "the tool call has no id")
        name = canonical_name(tool_name)
        digest = arguments_digest(arguments)
        async with self._engine.begin() as connection:
            run = await self._load(connection, claims, call_id)
            if run is None:
                log.warning("gate: unknown run org=%s run=%s", claims.org_id, claims.run_id)
                return Verdict("deny", GateStep.TOOLSET, "the run is unknown or ended")
            if run.call is not None and run.call != _Call(name, digest):
                verdict = Verdict("deny", GateStep.RECORDED, "the call id belongs to another call")
                await self._audit(
                    connection, run.ref, "tool_gate.verdict", call_id, _details(name, verdict)
                )
            else:
                verdict = decide(run.context, name, arguments)
                await self._record(connection, run, call_id, name, arguments, digest, verdict)
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

    async def screen(
        self, claims: RunTokenClaims, tool_name: str, arguments: Mapping[str, Any]
    ) -> Verdict:
        """The verdict without a call id, for the sama MCP server. The server refuses
        ``deny``. A ``deny`` is in the audit log."""
        name = canonical_name(tool_name)
        async with self._engine.begin() as connection:
            run = await self._load(connection, claims, None)
            if run is None:
                return Verdict("deny", GateStep.TOOLSET, "the run is unknown or ended")
            verdict = decide(run.context, name, arguments)
            if verdict.action == "deny":
                await self._audit(
                    connection, run.ref, "tool_gate.screen", None, _details(name, verdict)
                )
        return verdict

    async def _load(
        self,
        connection: AsyncConnection,
        claims: RunTokenClaims,
        call_id: str | None,
    ) -> _Run | None:
        try:
            org_id, workspace_id, session_id, run_id = (
                uuid.UUID(claims.org_id),
                uuid.UUID(claims.workspace_id),
                uuid.UUID(claims.session_id),
                uuid.UUID(claims.run_id),
            )
        except ValueError:
            return None
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
                    AgentProfile.toolsets,
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
                    Run.session_id == session_id,
                    Session.workspace_id == workspace_id,
                    # A runner of an ended run (for example after a worker crash) gets no tool.
                    Run.status.not_in(ENDED_RUN_STATUSES),
                )
            )
        ).one_or_none()
        if row is None:
            return None
        user_id = row.triggered_by_user_id
        if user_id is None and claims.user_id:
            user_id = uuid.UUID(claims.user_id)
        toolsets = row.toolsets if isinstance(row.toolsets, list) else []
        ref = _Ref(org_id, workspace_id, session_id, run_id)
        context = GateContext(
            toolsets=frozenset(str(item) for item in toolsets),
            rules=await self._rules(connection, org_id, row.agent_profile_id, user_id),
            unattended=bool(row.unattended),
            recorded=(await self._recorded(connection, ref, call_id) if call_id else None),
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
                set_={"decision": verdict.action, "status": status},
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

        Only a user who can read the session decides. ``always_allow`` with ``approve`` also
        writes a user policy rule for this tool and the profile of the run.
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
                    )
                    .join(
                        Session,
                        and_(Session.org_id == Approval.org_id, Session.id == Approval.session_id),
                    )
                    .join(Run, and_(Run.org_id == Approval.org_id, Run.id == Approval.run_id))
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
            if not grants.sees_session(row.workspace_id, row.audience, row.owner_user_id):
                raise ApprovalError("this user cannot decide the approval")
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
                if approve and always_allow:
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
