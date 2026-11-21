"""The workflow engine (docs/architecture.md, Section 12.3).

The engine is a loop in the control plane process, next to the scheduler. It has five steps:

- ``tick`` (each ``TICK_S``): one process holds the leader lease and starts the instances of the
  due schedules. An instance is unique on ``(workflow_id, scheduled_for)``.
- ``advance`` (each ``POLL_S``): locks one open instance at a time (``SKIP LOCKED``), walks its
  step tree, writes the rows of the steps that can start, and ends the instance when its last
  step ended.
- ``run_tools`` (each ``POLL_S``): claims the ``pending`` tool steps, writes the receipt
  ``intent``, calls the tool, and writes the result.
- ``reap`` (each ``POLL_S``): a tool step whose lease ended has an unknown result.
- ``reconcile`` (each ``POLL_S``): asks the tool runner if a write with an unknown result
  happened. When the runner cannot tell, the step waits for a human.

Each step reads and writes rows only, so a crash between two transactions loses nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, NamedTuple, Protocol, cast

import httpx
from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.connectors.errors import ConnectorError, ConnectorUnknownError
from sama.connectors.service import ConnectionService
from sama.connectors.service import RunScope as ConnectionScope
from sama.db.models import (
    ENDED_RUN_STATUSES,
    AgentProfile,
    Message,
    Run,
    Session,
    ToolPolicy,
    Workflow,
    WorkflowInstance,
    WorkflowReceipt,
    WorkflowStep,
    WorkflowVersion,
)
from sama.gate.decide import Action, PolicyRule, RuleLevel, arguments_digest, policy_verdict
from sama.orchestrator.queue import QueueError, RunQueue
from sama.tools.output import untrusted
from sama.vault.redact import redact
from sama.workflows.spec import (
    MAX_DEPTH,
    WRITE_ACCESS,
    WorkflowError,
    first_fire,
    holds,
    render,
    render_prompt,
    resolve_inputs,
    wake_time,
)
from sama.workflows.store import (
    OPEN_INSTANCE_STATUSES,
    OWNER_GONE,
    approved,
    cleared,
    close_instance,
    insert_instance,
    load_version,
    owner_sees,
    touch_instance,
)

log = logging.getLogger("sama.workflows")

TICK_S: Final = 60.0
POLL_S: Final = 2.0
BATCH: Final = 50
MAX_TOOL_CALLS: Final = 4
LEASE_KEY: Final = "sama.workflows.tick"
LEASE_S: Final = 120.0
"""The lease of a running tool step. A worker that stops leaves the step ``running``. When the
lease ends, ``reap`` treats the step as an unknown result."""
RETRY_BASE_S: Final = 10.0
MAX_ATTEMPTS: Final = 3
MAX_OUTPUT_CHARS: Final = 20_000


class Reconciled(NamedTuple):
    """The result of a reconcile: the write ``happened`` or not, and its output."""

    happened: bool
    output: Any = None


class StepTools(Protocol):
    """The runner of tool steps. ``ConnectorStepTools`` runs the connector tools."""

    def access(self, name: str) -> str | None: ...

    def idempotent(self, name: str) -> bool: ...

    async def call(
        self,
        scope: ConnectionScope,
        name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str | None,
    ) -> str: ...

    async def reconcile(
        self, scope: ConnectionScope, name: str, arguments: dict[str, Any], *, operation_id: str
    ) -> Reconciled | None: ...


class ConnectorStepTools:
    """Tool steps run connector tools of the catalog (Section 10.3)."""

    def __init__(self, connections: ConnectionService) -> None:
        self._connections = connections

    def access(self, name: str) -> str | None:
        return self._connections.tool_access(name)

    def idempotent(self, name: str) -> bool:
        return self._connections.idempotent(name)

    async def call(
        self,
        scope: ConnectionScope,
        name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str | None,
    ) -> str:
        return await self._connections.call_as(
            scope, name, arguments, idempotency_key=idempotency_key
        )

    async def reconcile(
        self, scope: ConnectionScope, name: str, arguments: dict[str, Any], *, operation_id: str
    ) -> Reconciled | None:
        """A connector tool has no way to look up a call by its operation id in M4.5, so the
        step waits for a human."""
        return None


Outcome = Literal["done", "failed", "unknown"]


@dataclass(frozen=True, slots=True)
class _Claim:
    step_row_id: uuid.UUID
    org_id: uuid.UUID
    instance_id: uuid.UUID
    step_id: str
    tool: str
    arguments: dict[str, Any]
    attempt: int
    operation_id: str
    retry_safe: bool
    idempotency_key: str | None
    scope: ConnectionScope


@dataclass(frozen=True, slots=True)
class _State:
    kind: Literal["done", "blocked", "failed"]
    value: Any = None


_BLOCKED: Final = _State("blocked")


def _parse_output(text: str) -> Any:
    """The output of a tool step. A JSON answer keeps its form, so a template can name its
    fields. The text is redacted and clipped."""
    text = redact(text.strip())
    if len(text) > MAX_OUTPUT_CHARS:
        return text[: MAX_OUTPUT_CHARS - 1] + "…"
    try:
        value = json.loads(text)
    except ValueError:
        return text
    return value if isinstance(value, (dict, list)) else text


def _receipt_text(output: Any) -> str | None:
    """The answer as the receipt keeps it: text, clipped like the step output."""
    if output is None:
        return None
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    return text if len(text) <= MAX_OUTPUT_CHARS else text[: MAX_OUTPUT_CHARS - 1] + "…"


def _short(error: BaseException | str) -> str:
    text = error if isinstance(error, str) else (str(error) or type(error).__name__)
    return redact(text)[:500]


class WorkflowEngine:
    def __init__(
        self,
        engine: AsyncEngine,
        queue: RunQueue,
        tools: StepTools,
        *,
        tick_s: float = TICK_S,
        poll_s: float = POLL_S,
        lease_s: float = LEASE_S,
        retry_base_s: float = RETRY_BASE_S,
    ) -> None:
        self._engine = engine
        self._queue = queue
        self._tools = tools
        self._tick_s = tick_s
        self._poll_s = poll_s
        self._lease_s = lease_s
        self._retry_base_s = retry_base_s

    async def serve(self, stop: asyncio.Event) -> None:
        next_tick = time.monotonic()
        while not stop.is_set():
            if time.monotonic() >= next_tick:
                next_tick = time.monotonic() + self._tick_s
                await self._step("tick", self.tick)
            await self._step("advance", self.advance)
            await self._step("tools", self.run_tools)
            await self._step("reap", self.reap)
            await self._step("reconcile", self.reconcile)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._poll_s)

    async def run_once(self) -> None:
        """One pass of each step, for tests and for a manual drain."""
        await self.tick()
        await self.advance()
        await self.run_tools()
        await self.reap()
        await self.reconcile()
        await self.advance()

    async def _step(self, name: str, step: Any) -> None:
        try:
            await step()
        except Exception:
            log.exception("the workflow step %s failed", name)

    # -- tick -------------------------------------------------------------------------------

    async def tick(self) -> int:
        """Starts the instances of the due schedules when this process holds the leader lease.
        Returns the number of workflows that it handled, or ``-1`` without the lease."""
        async with self._engine.begin() as connection:
            leader = (
                await connection.execute(
                    select(func.pg_try_advisory_xact_lock(func.hashtextextended(LEASE_KEY, 0)))
                )
            ).scalar_one()
            if not leader:
                return -1
            rows = (
                await connection.execute(
                    select(Workflow)
                    .where(Workflow.status == "active", Workflow.next_fire_at <= func.now())
                    .order_by(Workflow.next_fire_at)
                    .limit(BATCH)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for row in rows:
                await self._due(connection, row)
            return len(rows)

    async def _due(self, connection: AsyncConnection, row: Any) -> None:
        scheduled_for = row.next_fire_at
        version = await load_version(connection, row.org_id, row.id, row.version)
        values: dict[str, Any] = {"last_fire_at": scheduled_for}
        if version is None or not cleared(version):
            values.update(status="pending_approval", next_fire_at=None)
        elif not await owner_sees(connection, row.org_id, row.owner_user_id, row.workspace_id):
            values.update(status="paused", next_fire_at=None)
        else:
            try:
                async with connection.begin_nested():
                    await insert_instance(
                        connection,
                        row,
                        inputs=resolve_inputs(version.definition, {}),
                        trigger="schedule",
                        started_by_kind="system",
                        scheduled_for=scheduled_for,
                    )
            except Exception:
                log.exception("the workflow %s did not start", row.id)
            values["next_fire_at"] = first_fire(
                version.definition, datetime.now(UTC), last=scheduled_for
            )
        await connection.execute(
            update(Workflow)
            .where(Workflow.org_id == row.org_id, Workflow.id == row.id)
            .values(**values)
        )

    # -- advance ----------------------------------------------------------------------------

    async def advance(self) -> int:
        """Advances each open instance once. Returns the number of instances."""
        started = datetime.now(UTC)
        count = 0
        for _ in range(BATCH):
            async with self._engine.begin() as connection:
                row = (
                    await connection.execute(
                        select(WorkflowInstance)
                        .where(
                            WorkflowInstance.status.in_(OPEN_INSTANCE_STATUSES),
                            WorkflowInstance.checked_at < started,
                        )
                        .order_by(WorkflowInstance.checked_at)
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                ).one_or_none()
                if row is None:
                    break
                count += 1
                try:
                    async with connection.begin_nested():
                        await _Walk(self, connection, row).run()
                except Exception as error:
                    log.exception("the workflow instance %s did not advance", row.id)
                    await close_instance(
                        connection,
                        row.org_id,
                        row.id,
                        status="failed",
                        error=f"the engine failed: {type(error).__name__}",
                    )
                await connection.execute(
                    update(WorkflowInstance)
                    .where(WorkflowInstance.org_id == row.org_id, WorkflowInstance.id == row.id)
                    .values(checked_at=func.now())
                )
        return count

    # -- tool steps -------------------------------------------------------------------------

    async def run_tools(self) -> int:
        """Claims the due tool steps and runs them. Returns the number of calls."""
        claims = await self._claim()
        if claims:
            await asyncio.gather(*(self._call(claim) for claim in claims))
        return len(claims)

    async def _claim(self) -> list[_Claim]:
        async with self._engine.begin() as connection:
            rows = (
                await connection.execute(
                    select(
                        WorkflowStep.id,
                        WorkflowStep.org_id,
                        WorkflowStep.instance_id,
                        WorkflowStep.step_id,
                        WorkflowStep.input,
                        WorkflowStep.attempts,
                        WorkflowStep.operation_id,
                        WorkflowInstance.workspace_id,
                        WorkflowInstance.owner_user_id,
                        WorkflowInstance.agent_profile_id,
                        WorkflowVersion.hash,
                        WorkflowVersion.definition,
                        WorkflowVersion.approved_hash,
                    )
                    .join(
                        WorkflowInstance,
                        and_(
                            WorkflowInstance.org_id == WorkflowStep.org_id,
                            WorkflowInstance.id == WorkflowStep.instance_id,
                        ),
                    )
                    .join(
                        WorkflowVersion,
                        and_(
                            WorkflowVersion.org_id == WorkflowInstance.org_id,
                            WorkflowVersion.workflow_id == WorkflowInstance.workflow_id,
                            WorkflowVersion.version == WorkflowInstance.version,
                        ),
                    )
                    .where(
                        WorkflowStep.status == "pending",
                        WorkflowStep.kind == "tool",
                        WorkflowStep.next_attempt_at <= func.now(),
                        WorkflowInstance.status.in_(OPEN_INSTANCE_STATUSES),
                    )
                    .order_by(WorkflowStep.next_attempt_at)
                    .limit(MAX_TOOL_CALLS)
                    .with_for_update(of=WorkflowStep, skip_locked=True)
                )
            ).all()
            claims = []
            for row in rows:
                claim = await self._claim_one(connection, row)
                if claim is not None:
                    claims.append(claim)
            return claims

    async def _claim_one(self, connection: AsyncConnection, row: Any) -> _Claim | None:
        tool = str(row.input["tool"])
        arguments = dict(row.input["args"])
        problem = await self._problem(connection, row, tool)
        if problem is not None:
            await connection.execute(
                update(WorkflowStep)
                .where(WorkflowStep.org_id == row.org_id, WorkflowStep.id == row.id)
                .values(status="failed", error=problem, finished_at=func.now())
            )
            await touch_instance(connection, row.org_id, row.instance_id)
            return None
        attempt = row.attempts + 1
        key = await self._key(connection, row, tool)
        await connection.execute(
            update(WorkflowStep)
            .where(WorkflowStep.org_id == row.org_id, WorkflowStep.id == row.id)
            .values(
                status="running",
                attempts=attempt,
                lease_expires_at=func.now() + timedelta(seconds=self._lease_s),
                started_at=func.coalesce(WorkflowStep.started_at, func.now()),
            )
        )
        # The receipt ``intent`` is in the same transaction as the claim, before the call.
        await connection.execute(
            insert(WorkflowReceipt).values(
                org_id=row.org_id,
                instance_id=row.instance_id,
                step_row_id=row.id,
                step_id=row.step_id,
                operation_id=row.operation_id,
                attempt=attempt,
                tool=tool,
                status="intent",
                idempotency_key=key,
                request_digest=arguments_digest({"tool": tool, "args": arguments}),
            )
        )
        return _Claim(
            step_row_id=row.id,
            org_id=row.org_id,
            instance_id=row.instance_id,
            step_id=row.step_id,
            tool=tool,
            arguments=arguments,
            attempt=attempt,
            operation_id=row.operation_id,
            retry_safe=self._retry_safe(tool, key),
            idempotency_key=key,
            scope=ConnectionScope(
                org_id=row.org_id,
                workspace_id=row.workspace_id,
                user_id=row.owner_user_id,
                shared=False,
            ),
        )

    async def _key(self, connection: AsyncConnection, row: Any, tool: str) -> str | None:
        """The idempotency key of the call. Each attempt of a step sends the key of its first
        attempt, or no key when the first attempt sent none: a key that comes later cannot
        stop a second write of an earlier call without a key (Section 12.3)."""
        if row.attempts > 0:
            first = (
                await connection.execute(
                    select(WorkflowReceipt.idempotency_key).where(
                        WorkflowReceipt.org_id == row.org_id,
                        WorkflowReceipt.step_row_id == row.id,
                        WorkflowReceipt.attempt == 1,
                    )
                )
            ).one_or_none()
            if first is not None:
                return cast(str | None, first.idempotency_key)
        return str(row.operation_id) if self._tools.idempotent(tool) else None

    def _retry_safe(self, tool: str, key: str | None) -> bool:
        """A call with an unknown result can run again: the tool reads, or the call sent an
        idempotency key and the tool still takes it. The catalog of now decides the access;
        the receipt decides the key."""
        access = self._tools.access(tool)
        if access is not None and access not in WRITE_ACCESS:
            return True
        return key is not None and self._tools.idempotent(tool)

    async def _problem(self, connection: AsyncConnection, row: Any, tool: str) -> str | None:
        """Why the tool step cannot run now. The engine checks again at each claim: the
        catalog, the approval of the version, the workspace of the owner, and the policy of the
        owner can change."""
        access = self._tools.access(tool)
        if access is None:
            return f"{tool} is not a connector tool of the catalog now"
        if access in WRITE_ACCESS and not approved(row):
            return f"{tool} writes, and the owner did not approve this workflow version"
        if not await owner_sees(connection, row.org_id, row.owner_user_id, row.workspace_id):
            return OWNER_GONE
        profile = (
            await connection.execute(
                select(AgentProfile.toolsets).where(
                    AgentProfile.org_id == row.org_id, AgentProfile.id == row.agent_profile_id
                )
            )
        ).scalar_one_or_none()
        if not isinstance(profile, list) or "integrations" not in profile:
            return "the agent profile of the workflow does not have the integrations toolset"
        rules = (
            await connection.execute(
                select(
                    ToolPolicy.tool,
                    ToolPolicy.action,
                    ToolPolicy.agent_profile_id,
                    ToolPolicy.user_id,
                ).where(
                    ToolPolicy.org_id == row.org_id,
                    or_(
                        ToolPolicy.agent_profile_id.is_(None),
                        ToolPolicy.agent_profile_id == row.agent_profile_id,
                    ),
                    or_(ToolPolicy.user_id.is_(None), ToolPolicy.user_id == row.owner_user_id),
                )
            )
        ).all()
        verdict = policy_verdict(
            [
                PolicyRule(
                    tool=rule.tool,
                    action=cast(Action, rule.action),
                    level=(
                        RuleLevel.USER
                        if rule.user_id is not None
                        else RuleLevel.PROFILE
                        if rule.agent_profile_id is not None
                        else RuleLevel.ORG
                    ),
                )
                for rule in rules
            ],
            tool,
        )
        if verdict.action == "deny":
            return f"a policy rule denies {tool}"
        return None

    async def _call(self, claim: _Claim) -> None:
        output: Any = None
        error: str | None = None
        outcome: Outcome
        try:
            text = await self._tools.call(
                claim.scope, claim.tool, claim.arguments, idempotency_key=claim.idempotency_key
            )
            outcome, output = "done", _parse_output(text)
        except (ConnectorUnknownError, TimeoutError, httpx.HTTPError) as caught:
            outcome, error = "unknown", _short(caught)
        except ConnectorError as caught:
            outcome, error = "failed", _short(caught)
        except Exception as caught:
            # An error that sama does not know can come after the provider did the write.
            log.exception("the tool step %s failed", claim.step_row_id)
            outcome, error = "unknown", _short(f"the call failed: {type(caught).__name__}")
        async with self._engine.begin() as connection:
            await connection.execute(
                update(WorkflowReceipt)
                .where(
                    WorkflowReceipt.org_id == claim.org_id,
                    WorkflowReceipt.step_row_id == claim.step_row_id,
                    WorkflowReceipt.attempt == claim.attempt,
                )
                .values(
                    status=outcome,
                    response=_receipt_text(output) if outcome == "done" else None,
                    error=error,
                    finished_at=func.now(),
                )
            )
            if outcome == "done":
                values: dict[str, Any] = {
                    "status": "completed",
                    "output": output,
                    "error": None,
                    "lease_expires_at": None,
                    "finished_at": func.now(),
                }
            elif outcome == "failed":
                values = self._retry(claim.attempt, error)
            else:
                values = self._unknown(claim.attempt, claim.retry_safe, error)
            # A cancelled step, or a step that ``reap`` took back, does not change.
            await connection.execute(
                update(WorkflowStep)
                .where(
                    WorkflowStep.org_id == claim.org_id,
                    WorkflowStep.id == claim.step_row_id,
                    WorkflowStep.status == "running",
                    WorkflowStep.attempts == claim.attempt,
                )
                .values(**values)
            )
            await touch_instance(connection, claim.org_id, claim.instance_id)

    def _retry(self, attempt: int, error: str | None) -> dict[str, Any]:
        """A retry with backoff, or ``failed`` after ``MAX_ATTEMPTS``."""
        if attempt < MAX_ATTEMPTS:
            delay = timedelta(seconds=self._retry_base_s * 2**attempt)
            return {
                "status": "pending",
                "next_attempt_at": func.now() + delay,
                "lease_expires_at": None,
                "error": error,
            }
        return {
            "status": "failed",
            "lease_expires_at": None,
            "error": error,
            "finished_at": func.now(),
        }

    def _unknown(self, attempt: int, retry_safe: bool, error: str | None) -> dict[str, Any]:
        """An unknown result. A read, or a call with an idempotency key, retries with the same
        operation id. A write without a key does not run again: it waits for a reconcile."""
        if retry_safe:
            return self._retry(attempt, error)
        return {"status": "needs_reconcile", "lease_expires_at": None, "error": error}

    # -- reap -------------------------------------------------------------------------------

    async def reap(self) -> int:
        """A tool step whose lease ended has an unknown result: the worker stopped. Returns
        the number of steps."""
        async with self._engine.begin() as connection:
            rows = (
                await connection.execute(
                    select(
                        WorkflowStep.id,
                        WorkflowStep.org_id,
                        WorkflowStep.instance_id,
                        WorkflowStep.attempts,
                        WorkflowStep.input,
                        WorkflowReceipt.idempotency_key,
                    )
                    .outerjoin(
                        WorkflowReceipt,
                        and_(
                            WorkflowReceipt.org_id == WorkflowStep.org_id,
                            WorkflowReceipt.step_row_id == WorkflowStep.id,
                            WorkflowReceipt.attempt == WorkflowStep.attempts,
                        ),
                    )
                    .where(
                        WorkflowStep.status == "running",
                        WorkflowStep.kind == "tool",
                        WorkflowStep.lease_expires_at < func.now(),
                    )
                    .limit(BATCH)
                    .with_for_update(of=WorkflowStep, skip_locked=True)
                )
            ).all()
            for row in rows:
                error = "the worker stopped before the result came back"
                await connection.execute(
                    update(WorkflowReceipt)
                    .where(
                        WorkflowReceipt.org_id == row.org_id,
                        WorkflowReceipt.step_row_id == row.id,
                        WorkflowReceipt.attempt == row.attempts,
                        WorkflowReceipt.status == "intent",
                    )
                    .values(status="unknown", error=error, finished_at=func.now())
                )
                tool = str((row.input or {}).get("tool", ""))
                await connection.execute(
                    update(WorkflowStep)
                    .where(WorkflowStep.org_id == row.org_id, WorkflowStep.id == row.id)
                    .values(
                        **self._unknown(
                            row.attempts, self._retry_safe(tool, row.idempotency_key), error
                        )
                    )
                )
                await touch_instance(connection, row.org_id, row.instance_id)
            return len(rows)

    # -- reconcile --------------------------------------------------------------------------

    async def reconcile(self) -> int:
        """Checks the writes with an unknown result. Returns the number of steps."""
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(
                        WorkflowStep.id,
                        WorkflowStep.org_id,
                        WorkflowStep.instance_id,
                        WorkflowStep.attempts,
                        WorkflowStep.input,
                        WorkflowStep.operation_id,
                        WorkflowInstance.workspace_id,
                        WorkflowInstance.owner_user_id,
                    )
                    .join(
                        WorkflowInstance,
                        and_(
                            WorkflowInstance.org_id == WorkflowStep.org_id,
                            WorkflowInstance.id == WorkflowStep.instance_id,
                        ),
                    )
                    .where(WorkflowStep.status == "needs_reconcile")
                    .order_by(WorkflowStep.created_at)
                    .limit(BATCH)
                )
            ).all()
        for row in rows:
            scope = ConnectionScope(
                org_id=row.org_id,
                workspace_id=row.workspace_id,
                user_id=row.owner_user_id,
                shared=False,
            )
            try:
                found = await self._tools.reconcile(
                    scope,
                    str(row.input["tool"]),
                    dict(row.input["args"]),
                    operation_id=row.operation_id,
                )
            except Exception:
                log.exception("the reconcile of step %s failed", row.id)
                found = None
            await self._reconciled(row, found)
        return len(rows)

    async def _reconciled(self, row: Any, found: Reconciled | None) -> None:
        """Applies a reconcile result to the attempt that it checked. A result for an older
        attempt changes nothing: two loops can check the same step (Section 12.3)."""
        output: Any = None
        if found is None:
            values: dict[str, Any] = {
                "status": "waiting_human",
                "error": "sama cannot tell if the write happened. Check the system, then "
                "mark the step done, retry, or fail.",
            }
        elif found.happened:
            output = _parse_output(found.output) if isinstance(found.output, str) else found.output
            values = {"status": "completed", "output": output, "finished_at": func.now()}
        else:
            values = {"status": "pending", "next_attempt_at": func.now()}
        async with self._engine.begin() as connection:
            changed = (
                await connection.execute(
                    update(WorkflowStep)
                    .where(
                        WorkflowStep.org_id == row.org_id,
                        WorkflowStep.id == row.id,
                        WorkflowStep.status == "needs_reconcile",
                        WorkflowStep.attempts == row.attempts,
                    )
                    .values(**values)
                    .returning(WorkflowStep.id)
                )
            ).first()
            if changed is None:
                return
            if found is not None and found.happened:
                await connection.execute(
                    update(WorkflowReceipt)
                    .where(
                        WorkflowReceipt.org_id == row.org_id,
                        WorkflowReceipt.step_row_id == row.id,
                        WorkflowReceipt.attempt == row.attempts,
                    )
                    .values(
                        status="done",
                        response=_receipt_text(output),
                        error="found by a reconcile",
                    )
                )
            await touch_instance(connection, row.org_id, row.instance_id)


async def _answer(connection: AsyncConnection, org_id: uuid.UUID, run_id: uuid.UUID) -> str:
    """The last answer of the assistant in the run."""
    content = (
        await connection.execute(
            select(Message.content)
            .where(Message.org_id == org_id, Message.run_id == run_id, Message.role == "assistant")
            .order_by(Message.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return str((content or {}).get("text") or "")


class _Walk:
    """One walk of the step tree of a locked instance."""

    def __init__(self, owner: WorkflowEngine, connection: AsyncConnection, instance: Any) -> None:
        self.owner = owner
        self.connection = connection
        self.instance = instance
        self.rows: dict[str, Any] = {}
        self.context: dict[str, Any] = {"inputs": dict(instance.inputs or {}), "steps": {}}
        self.name = ""

    async def run(self) -> None:
        instance = self.instance
        version = await load_version(
            self.connection, instance.org_id, instance.workflow_id, instance.version
        )
        if version is None:
            return await self._close("failed", "the workflow version does not exist")
        if not await owner_sees(
            self.connection, instance.org_id, instance.owner_user_id, instance.workspace_id
        ):
            return await self._close("failed", OWNER_GONE)
        self.name = (
            await self.connection.execute(
                select(Workflow.name).where(
                    Workflow.org_id == instance.org_id, Workflow.id == instance.workflow_id
                )
            )
        ).scalar_one()
        for row in (
            await self.connection.execute(
                select(WorkflowStep).where(
                    WorkflowStep.org_id == instance.org_id,
                    WorkflowStep.instance_id == instance.id,
                )
            )
        ).all():
            self.rows[row.step_id] = row
            if row.status == "completed":
                self.context["steps"][row.step_id] = row.output
        state = await self.sequence(version.definition["steps"], None)
        if state.kind == "done":
            return await self._close("completed", None, state.value)
        if state.kind == "failed":
            return await self._close("failed", state.value)
        busy = any(row.status in ("pending", "running") for row in self.rows.values())
        await self.connection.execute(
            update(WorkflowInstance)
            .where(WorkflowInstance.org_id == instance.org_id, WorkflowInstance.id == instance.id)
            .values(status="running" if busy else "waiting")
        )

    async def _close(self, status: str, error: str | None, output: Any = None) -> None:
        await close_instance(
            self.connection,
            self.instance.org_id,
            self.instance.id,
            status=status,
            error=error,
            output=output,
        )

    async def sequence(self, steps: list[dict[str, Any]], parent: str | None) -> _State:
        last: Any = None
        for step in steps:
            state = await self.step(step, parent)
            if state.kind != "done":
                return state
            last = state.value
            self.context["steps"][step["id"]] = last
        return _State("done", last)

    async def step(self, step: Mapping[str, Any], parent: str | None) -> _State:
        row = self.rows.get(step["id"])
        if row is not None:
            if row.status in ("completed", "skipped"):
                return _State("done", row.output)
            if row.status in ("failed", "cancelled"):
                return _State("failed", f"step {step['id']} failed: {row.error or row.status}")
        try:
            handler = getattr(self, f"_{step['kind']}")
            state: _State = await handler(step, row, parent)
            return state
        except WorkflowError as error:
            await self._fail(step, row, parent, str(error))
            return _State("failed", f"step {step['id']} failed: {error}")

    async def _fail(
        self, step: Mapping[str, Any], row: Any, parent: str | None, error: str
    ) -> None:
        if row is None:
            await self._new(step, parent, status="failed", error=error, finished_at=func.now())
        else:
            await self._set(row, status="failed", error=error, finished_at=func.now())

    async def _new(self, step: Mapping[str, Any], parent: str | None, **values: Any) -> Any:
        row = (
            await self.connection.execute(
                pg_insert(WorkflowStep)
                .values(
                    org_id=self.instance.org_id,
                    instance_id=self.instance.id,
                    step_id=step["id"],
                    parent_step_id=parent,
                    kind=step["kind"],
                    **values,
                )
                .on_conflict_do_nothing(
                    index_elements=[WorkflowStep.instance_id, WorkflowStep.step_id]
                )
                .returning(*WorkflowStep.__table__.c)
            )
        ).one()
        self.rows[step["id"]] = row
        return row

    async def _set(self, row: Any, **values: Any) -> Any:
        new = (
            await self.connection.execute(
                update(WorkflowStep)
                .where(WorkflowStep.org_id == self.instance.org_id, WorkflowStep.id == row.id)
                .values(**values)
                .returning(*WorkflowStep.__table__.c)
            )
        ).one()
        self.rows[new.step_id] = new
        return new

    # -- step kinds -------------------------------------------------------------------------

    async def _tool(self, step: Mapping[str, Any], row: Any, parent: str | None) -> _State:
        if row is None:
            arguments = render(step["args"], self.context)
            await self._new(
                step,
                parent,
                status="pending",
                input={"tool": step["tool"], "args": arguments},
                operation_id=f"{self.instance.id}:{step['id']}",
            )
        return _BLOCKED

    async def _agent(self, step: Mapping[str, Any], row: Any, parent: str | None) -> _State:
        instance = self.instance
        if row is None:
            prompt = render_prompt(
                step["prompt"], self.context, lambda value, source: untrusted(value, source=source)
            )
            profile_id = await self._profile(step.get("profile"))
            async with self.connection.begin_nested():
                session_id = uuid.uuid4()
                await self.connection.execute(
                    insert(Session).values(
                        id=session_id,
                        org_id=instance.org_id,
                        workspace_id=instance.workspace_id,
                        agent_profile_id=profile_id,
                        audience="private",
                        owner_user_id=instance.owner_user_id,
                        title=f"Workflow: {self.name} / {step['id']}"[:200],
                    )
                )
                try:
                    run_id = await self.owner._queue.enqueue_in(
                        self.connection,
                        org_id=instance.org_id,
                        session_id=session_id,
                        text=self._agent_text(step, prompt),
                        user_id=instance.owner_user_id,
                        unattended=True,
                        model=step.get("model"),
                        check_rate=False,
                        toolsets=instance.toolsets,
                    )
                except QueueError as error:
                    raise WorkflowError(f"the run did not start: {error}") from None
            await self._new(
                step,
                parent,
                status="running",
                input={"prompt": prompt},
                session_id=session_id,
                run_id=run_id,
                started_at=func.now(),
            )
            return _BLOCKED
        ended = (
            await self.connection.execute(
                select(Run.status, Run.failure_message).where(
                    Run.org_id == instance.org_id, Run.id == row.run_id
                )
            )
        ).one_or_none()
        if ended is None or ended.status not in ENDED_RUN_STATUSES:
            return _BLOCKED
        if ended.status == "completed":
            output = redact(await _answer(self.connection, instance.org_id, row.run_id))
            await self._set(row, status="completed", output=output, finished_at=func.now())
            return _State("done", output)
        reason = (
            "the run was cancelled"
            if ended.status == "cancelled"
            else f"the run failed: {ended.failure_message or 'the reason is not known'}"
        )
        raise WorkflowError(reason)

    def _agent_text(self, step: Mapping[str, Any], prompt: str) -> str:
        lines = [
            f'[Workflow "{self.name}", step "{step["id"]}". No person is in this run: do not '
            "ask questions. Your final answer is the output of the step.]"
        ]
        if step.get("skills"):
            lines.append(f"Use these skills (skill_view): {', '.join(step['skills'])}.")
        lines.append(prompt)
        return "\n\n".join(lines)

    async def _profile(self, ref: str | None) -> uuid.UUID:
        instance = self.instance
        if ref is None:
            return uuid.UUID(str(instance.agent_profile_id))
        match = AgentProfile.name == ref
        with contextlib.suppress(ValueError):
            match = or_(match, AgentProfile.id == uuid.UUID(ref))
        found = (
            await self.connection.execute(
                select(AgentProfile.id)
                .where(
                    AgentProfile.org_id == instance.org_id,
                    match,
                    or_(
                        AgentProfile.workspace_id == instance.workspace_id,
                        AgentProfile.workspace_id.is_(None),
                    ),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if found is None:
            raise WorkflowError(f"the agent profile {ref} does not exist in the workspace")
        return uuid.UUID(str(found))

    async def _approval(self, step: Mapping[str, Any], row: Any, parent: str | None) -> _State:
        if row is None:
            prompt = render(step["prompt"], self.context)
            await self._new(step, parent, status="waiting", input={"prompt": prompt})
        return _BLOCKED

    async def _wait(self, step: Mapping[str, Any], row: Any, parent: str | None) -> _State:
        now = datetime.now(UTC)
        if row is None:
            await self._new(
                step,
                parent,
                status="waiting",
                wake_at=wake_time(step, self.context, now),
                event=step.get("event"),
                started_at=func.now(),
            )
            return _BLOCKED
        if row.wake_at is None or now < row.wake_at:
            return _BLOCKED
        if row.event is not None:
            raise WorkflowError(f"the event {row.event} did not come in time")
        output = {"waited_until": row.wake_at.isoformat()}
        await self._set(row, status="completed", output=output, finished_at=func.now())
        return _State("done", output)

    async def _workflow(self, step: Mapping[str, Any], row: Any, parent: str | None) -> _State:
        instance = self.instance
        if row is None:
            child = await self._child_instance(step)
            await self._new(
                step,
                parent,
                status="running",
                input={"name": step["name"]},
                child_instance_id=child,
                started_at=func.now(),
            )
            return _BLOCKED
        child_row = (
            await self.connection.execute(
                select(
                    WorkflowInstance.status, WorkflowInstance.output, WorkflowInstance.error
                ).where(
                    WorkflowInstance.org_id == instance.org_id,
                    WorkflowInstance.id == row.child_instance_id,
                )
            )
        ).one_or_none()
        if child_row is None:
            raise WorkflowError("the sub-workflow instance does not exist")
        if child_row.status in OPEN_INSTANCE_STATUSES:
            return _BLOCKED
        if child_row.status == "completed":
            await self._set(
                row, status="completed", output=child_row.output, finished_at=func.now()
            )
            return _State("done", child_row.output)
        raise WorkflowError(f"the sub-workflow ended {child_row.status}: {child_row.error}")

    async def _child_instance(self, step: Mapping[str, Any]) -> uuid.UUID:
        instance = self.instance
        if instance.depth + 1 > MAX_DEPTH:
            raise WorkflowError(f"sub-workflows go at most {MAX_DEPTH} levels deep")
        child = (
            await self.connection.execute(
                select(Workflow).where(
                    Workflow.org_id == instance.org_id,
                    Workflow.owner_user_id == instance.owner_user_id,
                    Workflow.name == step["name"],
                    Workflow.status != "removed",
                )
            )
        ).one_or_none()
        if child is None:
            raise WorkflowError(f"the workflow {step['name']} does not exist")
        version = await load_version(self.connection, child.org_id, child.id, child.version)
        if child.status == "pending_approval" or version is None or not cleared(version):
            raise WorkflowError(f"the workflow {step['name']} needs the approval of its owner")
        if not await owner_sees(
            self.connection, child.org_id, child.owner_user_id, child.workspace_id
        ):
            raise WorkflowError(f"the workflow {step['name']}: {OWNER_GONE}")
        inputs = resolve_inputs(version.definition, render(step["inputs"], self.context))
        child_id = await insert_instance(
            self.connection,
            child,
            inputs=inputs,
            trigger="workflow",
            started_by_kind="system",
            parent=instance,
            parent_step_id=step["id"],
        )
        if child_id is None:
            raise WorkflowError(f"the workflow {step['name']} did not start")
        return child_id

    async def _branch(self, step: Mapping[str, Any], row: Any, parent: str | None) -> _State:
        if row is None:
            arm = "then" if holds(step["when"], self.context) else "else"
            row = await self._new(
                step, parent, status="running", input={"arm": arm}, started_at=func.now()
            )
        state = await self.sequence(step[row.input["arm"]], step["id"])
        if state.kind == "done":
            await self._set(row, status="completed", output=state.value, finished_at=func.now())
        elif state.kind == "failed":
            await self._set(row, status="failed", error=state.value, finished_at=func.now())
        return state

    async def _parallel(self, step: Mapping[str, Any], row: Any, parent: str | None) -> _State:
        if row is None:
            row = await self._new(step, parent, status="running", started_at=func.now())
        outputs: dict[str, Any] = {}
        blocked = False
        for name, steps in step["branches"].items():
            state = await self.sequence(steps, step["id"])
            if state.kind == "failed":
                error = f"branch {name}: {state.value}"
                await self._set(row, status="failed", error=error, finished_at=func.now())
                return _State("failed", f"step {step['id']} failed: {error}")
            if state.kind == "blocked":
                blocked = True
            else:
                outputs[name] = state.value
        if blocked:
            return _BLOCKED
        await self._set(row, status="completed", output=outputs, finished_at=func.now())
        return _State("done", outputs)


__all__ = [
    "LEASE_KEY",
    "ConnectorStepTools",
    "Reconciled",
    "StepTools",
    "WorkflowEngine",
]
