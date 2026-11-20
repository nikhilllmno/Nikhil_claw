"""The workflow engine (docs/architecture.md, Section 12.3): steps, durability, receipts, and
the reconcile of a write with an unknown result."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.connectors.errors import ConnectorError, ConnectorUnknownError
from sama.db.models import (
    Message,
    Run,
    Session,
    ToolPolicy,
    Workflow,
    WorkflowInstance,
    WorkflowReceipt,
    WorkflowStep,
)
from sama.orchestrator.queue import RunQueue
from sama.workflows.engine import Reconciled, WorkflowEngine
from sama.workflows.service import WorkflowService
from tests.workflows.conftest import Crash, FakeTools, Tenant

WRITE = """
name: post-note
inputs:
  text: { default: hello }
steps:
  - id: post
    tool: notes_write
    args: { text: "{{inputs.text}}" }
"""


async def _steps(engine: AsyncEngine, instance_id: str) -> dict[str, Any]:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(WorkflowStep).where(WorkflowStep.instance_id == uuid.UUID(instance_id))
            )
        ).all()
    return {row.step_id: row for row in rows}


async def _receipts(engine: AsyncEngine, instance_id: str) -> list[Any]:
    async with engine.connect() as connection:
        return list(
            (
                await connection.execute(
                    select(WorkflowReceipt)
                    .where(WorkflowReceipt.instance_id == uuid.UUID(instance_id))
                    .order_by(WorkflowReceipt.attempt)
                )
            ).all()
        )


async def _status(engine: AsyncEngine, instance_id: str) -> Any:
    async with engine.connect() as connection:
        return (
            await connection.execute(
                select(WorkflowInstance).where(WorkflowInstance.id == uuid.UUID(instance_id))
            )
        ).one()


async def _approved(service: WorkflowService, tenant: Tenant, source: str) -> dict[str, Any]:
    view = await service.create(tenant.user(), source)
    if view["needs_approval"]:
        view = await service.approve(tenant.user(), uuid.UUID(view["id"]), view["version"])
    return view


async def _start(
    service: WorkflowService, tenant: Tenant, source: str, inputs: dict[str, Any] | None = None
) -> str:
    view = await _approved(service, tenant, source)
    instance = await service.start(tenant.user(), uuid.UUID(view["id"]), inputs or {})
    return str(instance["id"])


async def _drain(runner: WorkflowEngine, rounds: int = 4) -> None:
    for _ in range(rounds):
        await runner.run_once()


# -- tool steps -------------------------------------------------------------------------------


async def test_a_tool_step_runs_once_and_keeps_a_receipt(
    engine: AsyncEngine,
    tenant: Tenant,
    service: WorkflowService,
    runner: WorkflowEngine,
    tools: FakeTools,
) -> None:
    instance_id = await _start(service, tenant, WRITE, {"text": "hi"})
    await _drain(runner)

    assert tools.calls == [("notes_write", {"text": "hi"}, None)]
    instance = await _status(engine, instance_id)
    assert instance.status == "completed"
    assert instance.output == {"tool": "notes_write", "args": {"text": "hi"}}
    step = (await _steps(engine, instance_id))["post"]
    assert step.status == "completed"
    assert step.operation_id == f"{instance_id}:post"
    [receipt] = await _receipts(engine, instance_id)
    assert receipt.status == "done"
    assert receipt.operation_id == f"{instance_id}:post"
    assert receipt.idempotency_key is None
    assert len(receipt.request_digest) == 64


async def test_a_crash_after_a_write_waits_for_a_human_and_does_not_write_again(
    engine: AsyncEngine,
    tenant: Tenant,
    service: WorkflowService,
    runner: WorkflowEngine,
    tools: FakeTools,
) -> None:
    """The acceptance test of M4.5: the worker stops after a write that is not idempotent."""
    tools.script["notes_write"] = ["write-then-crash"]
    instance_id = await _start(service, tenant, WRITE)
    await runner.advance()
    with pytest.raises(Crash):
        await runner.run_tools()
    assert tools.writes == 1
    step = (await _steps(engine, instance_id))["post"]
    assert step.status == "running"
    [receipt] = await _receipts(engine, instance_id)
    assert receipt.status == "intent"

    # A new process starts. The lease of the step has not ended yet: nothing happens.
    restarted = WorkflowEngine(engine, RunQueue(engine), tools, retry_base_s=0)
    await _drain(restarted)
    assert tools.writes == 1
    assert (await _steps(engine, instance_id))["post"].status == "running"

    # The lease ends. The result is unknown, so the step is not retried.
    async with engine.begin() as connection:
        await connection.execute(
            update(WorkflowStep)
            .where(WorkflowStep.id == step.id)
            .values(lease_expires_at=func.now() - timedelta(seconds=1))
        )
    assert await restarted.reap() == 1
    assert (await _steps(engine, instance_id))["post"].status == "needs_reconcile"
    [receipt] = await _receipts(engine, instance_id)
    assert receipt.status == "unknown"

    # The tool runner cannot tell if the write happened: a human decides.
    await _drain(restarted)
    step = (await _steps(engine, instance_id))["post"]
    assert step.status == "waiting_human"
    assert tools.checks == [f"{instance_id}:post"]
    assert tools.writes == 1
    assert len(tools.calls) == 1
    assert (await _status(engine, instance_id)).status == "waiting"

    # The owner checks the system and marks the step done.
    view = await service.resolve(
        tenant.user(), uuid.UUID(instance_id), "post", "done", {"note": "found"}
    )
    assert view["status"] in ("running", "waiting")
    await _drain(restarted)
    instance = await _status(engine, instance_id)
    assert instance.status == "completed"
    assert instance.output == {"note": "found"}
    assert tools.writes == 1


async def test_a_reconcile_that_finds_the_write_completes_the_step(
    engine: AsyncEngine,
    tenant: Tenant,
    service: WorkflowService,
    runner: WorkflowEngine,
    tools: FakeTools,
) -> None:
    tools.script["notes_write"] = [ConnectorUnknownError("the provider timed out")]
    tools.found = Reconciled(happened=True, output='{"id": "n-1"}')
    instance_id = await _start(service, tenant, WRITE)
    await _drain(runner)

    assert len(tools.calls) == 1
    instance = await _status(engine, instance_id)
    assert instance.status == "completed"
    assert instance.output == {"id": "n-1"}
    [receipt] = await _receipts(engine, instance_id)
    assert receipt.status == "done"


async def test_a_reconcile_that_finds_no_write_calls_the_tool_again(
    engine: AsyncEngine,
    tenant: Tenant,
    service: WorkflowService,
    runner: WorkflowEngine,
    tools: FakeTools,
) -> None:
    tools.script["notes_write"] = [ConnectorUnknownError("the provider timed out")]
    tools.found = Reconciled(happened=False)
    instance_id = await _start(service, tenant, WRITE)
    await _drain(runner)

    assert len(tools.calls) == 2
    assert (await _status(engine, instance_id)).status == "completed"
    assert [receipt.status for receipt in await _receipts(engine, instance_id)] == [
        "unknown",
        "done",
    ]


async def test_the_owner_can_retry_or_fail_a_step_that_waits(
    engine: AsyncEngine,
    tenant: Tenant,
    service: WorkflowService,
    runner: WorkflowEngine,
    tools: FakeTools,
) -> None:
    tools.script["notes_write"] = [
        ConnectorUnknownError("lost"),
        ConnectorUnknownError("lost again"),
    ]
    first = await _start(service, tenant, WRITE)
    await _drain(runner)
    assert (await _steps(engine, first))["post"].status == "waiting_human"
    await service.resolve(tenant.user(), uuid.UUID(first), "post", "retry")
    await _drain(runner)
    assert (await _steps(engine, first))["post"].status == "waiting_human"
    assert len(tools.calls) == 2
    await service.resolve(tenant.user(), uuid.UUID(first), "post", "fail")
    await _drain(runner)
    instance = await _status(engine, first)
    assert instance.status == "failed"
    assert "owner marked" in instance.error


async def test_an_agent_cannot_resolve_a_step(
    tenant: Tenant, service: WorkflowService, runner: WorkflowEngine, tools: FakeTools
) -> None:
    tools.script["notes_write"] = [ConnectorUnknownError("lost")]
    instance_id = await _start(service, tenant, WRITE)
    await _drain(runner)
    with pytest.raises(Exception, match="only a person"):
        await service.resolve(tenant.agent(), uuid.UUID(instance_id), "post", "done")


async def test_an_idempotent_write_retries_with_the_same_key(
    engine: AsyncEngine,
    tenant: Tenant,
    service: WorkflowService,
    runner: WorkflowEngine,
    tools: FakeTools,
) -> None:
    tools.script["keyed_write"] = [ConnectorUnknownError("lost")]
    source = WRITE.replace("notes_write", "keyed_write")
    instance_id = await _start(service, tenant, source)
    await _drain(runner)

    key = f"{instance_id}:post"
    assert [call[2] for call in tools.calls] == [key, key]
    assert (await _status(engine, instance_id)).status == "completed"
    receipts = await _receipts(engine, instance_id)
    assert [(item.status, item.idempotency_key) for item in receipts] == [
        ("unknown", key),
        ("done", key),
    ]


async def test_a_definite_failure_retries_and_then_fails_the_instance(
    engine: AsyncEngine,
    tenant: Tenant,
    service: WorkflowService,
    runner: WorkflowEngine,
    tools: FakeTools,
) -> None:
    tools.script["notes_write"] = [ConnectorError("bad request")] * 3
    instance_id = await _start(service, tenant, WRITE)
    await _drain(runner, rounds=5)

    assert len(tools.calls) == 3
    instance = await _status(engine, instance_id)
    assert instance.status == "failed"
    assert "bad request" in instance.error
    assert [item.status for item in await _receipts(engine, instance_id)] == ["failed"] * 3


async def test_a_lost_read_retries_and_the_retry_backs_off(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, tools: FakeTools
) -> None:
    tools.script["notes_read"] = [ConnectorUnknownError("lost")]
    slow = WorkflowEngine(engine, RunQueue(engine), tools, retry_base_s=3600)
    source = WRITE.replace("notes_write", "notes_read")
    instance_id = await _start(service, tenant, source)
    await _drain(slow)
    step = (await _steps(engine, instance_id))["post"]
    assert step.status == "pending"
    assert step.next_attempt_at > datetime.now(UTC) + timedelta(minutes=30)
    assert len(tools.calls) == 1


async def test_a_deny_rule_or_a_missing_toolset_fails_the_tool_step(
    engine: AsyncEngine,
    tenant: Tenant,
    service: WorkflowService,
    runner: WorkflowEngine,
    tools: FakeTools,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            insert(ToolPolicy).values(org_id=tenant.org, tool="notes_write", action="deny")
        )
    instance_id = await _start(service, tenant, WRITE)
    await _drain(runner)
    assert tools.calls == []
    instance = await _status(engine, instance_id)
    assert instance.status == "failed"
    assert "policy rule denies" in instance.error


async def test_a_write_step_does_not_run_when_the_version_lost_its_approval(
    engine: AsyncEngine,
    tenant: Tenant,
    service: WorkflowService,
    runner: WorkflowEngine,
    tools: FakeTools,
) -> None:
    """The engine checks the approval again at each claim."""
    from sama.db.models import WorkflowVersion

    instance_id = await _start(service, tenant, WRITE)
    async with engine.begin() as connection:
        await connection.execute(
            update(WorkflowVersion)
            .where(WorkflowVersion.org_id == tenant.org)
            .values(approved_hash=None, approved_at=None, approved_by_user_id=None)
        )
    await _drain(runner)
    assert tools.calls == []
    assert "did not approve" in (await _status(engine, instance_id)).error


# -- other step kinds -------------------------------------------------------------------------


async def test_branch_parallel_and_templates(
    engine: AsyncEngine,
    tenant: Tenant,
    service: WorkflowService,
    runner: WorkflowEngine,
    tools: FakeTools,
) -> None:
    source = """
name: fan-out
inputs:
  mode: { default: wide }
steps:
  - id: pick
    branch:
      when: { value: "{{inputs.mode}}", equals: wide }
      then:
        - id: both
          parallel:
            branches:
              left:
                - id: left_read
                  tool: notes_read
                  args: { side: left }
              right:
                - id: right_read
                  tool: notes_read
                  args: { side: right }
      else:
        - id: one
          tool: notes_read
          args: { side: one }
  - id: last
    tool: notes_read
    args: { got: "{{steps.pick.output.left.args.side}}" }
"""
    instance_id = await _start(service, tenant, source)
    await _drain(runner)
    instance = await _status(engine, instance_id)
    assert instance.status == "completed", instance.error
    assert instance.output == {"tool": "notes_read", "args": {"got": "left"}}
    steps = await _steps(engine, instance_id)
    assert "one" not in steps
    assert steps["pick"].input == {"arm": "then"}
    assert steps["left_read"].parent_step_id == "both"
    assert sorted(call[1].get("side", "") for call in tools.calls) == ["", "left", "right"]


async def test_an_approval_step_waits_for_the_owner(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    source = """
name: gated
steps:
  - id: ask
    approval: { prompt: "Go?" }
  - id: read
    tool: notes_read
    args: {}
"""
    instance_id = await _start(service, tenant, source)
    await _drain(runner)
    assert (await _status(engine, instance_id)).status == "waiting"
    with pytest.raises(Exception, match="only a person"):
        await service.decide(tenant.agent(), uuid.UUID(instance_id), "ask", approve=True)
    await service.decide(tenant.user(), uuid.UUID(instance_id), "ask", approve=True)
    await _drain(runner)
    assert (await _status(engine, instance_id)).status == "completed"

    rejected = await _start(service, tenant, source.replace("name: gated", "name: gated-2"))
    await _drain(runner)
    await service.decide(tenant.user(), uuid.UUID(rejected), "ask", approve=False)
    await _drain(runner)
    instance = await _status(engine, rejected)
    assert instance.status == "failed"
    assert "rejected" in instance.error


async def test_wait_steps_for_time_and_for_an_event(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    source = """
name: waits
steps:
  - id: pause
    wait: { seconds: 60 }
  - id: hook
    wait: { event: paid, timeout_seconds: 3600 }
"""
    instance_id = await _start(service, tenant, source)
    await _drain(runner)
    steps = await _steps(engine, instance_id)
    assert steps["pause"].status == "waiting"
    assert "hook" not in steps
    async with engine.begin() as connection:
        await connection.execute(
            update(WorkflowStep)
            .where(WorkflowStep.id == steps["pause"].id)
            .values(wake_at=func.now() - timedelta(seconds=1))
        )
    await runner.advance()
    await _drain(runner)
    steps = await _steps(engine, instance_id)
    assert steps["pause"].status == "completed"
    assert steps["hook"].event == "paid"
    assert await service.signal(tenant.user(), uuid.UUID(instance_id), "other") == 0
    assert await service.signal(tenant.user(), uuid.UUID(instance_id), "paid", {"n": 1}) == 1
    await _drain(runner)
    instance = await _status(engine, instance_id)
    assert instance.status == "completed"
    assert instance.output == {"n": 1}


async def test_an_event_wait_that_times_out_fails(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    source = """
name: late
steps:
  - id: hook
    wait: { event: paid, timeout_seconds: 60 }
"""
    instance_id = await _start(service, tenant, source)
    await _drain(runner)
    async with engine.begin() as connection:
        await connection.execute(
            update(WorkflowStep)
            .where(WorkflowStep.instance_id == uuid.UUID(instance_id))
            .values(wake_at=func.now() - timedelta(seconds=1))
        )
        await connection.execute(
            update(WorkflowInstance)
            .where(WorkflowInstance.id == uuid.UUID(instance_id))
            .values(checked_at=datetime(2000, 1, 1, tzinfo=UTC))
        )
    await _drain(runner)
    instance = await _status(engine, instance_id)
    assert instance.status == "failed"
    assert "did not come in time" in instance.error


async def test_an_agent_step_runs_in_a_private_session_and_its_answer_is_the_output(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    source = """
name: think
inputs:
  topic: { default: tea }
steps:
  - id: write
    agent: { prompt: "Write about {{inputs.topic}}." }
"""
    instance_id = await _start(service, tenant, source)
    await _drain(runner)
    step = (await _steps(engine, instance_id))["write"]
    assert step.status == "running"
    async with engine.begin() as connection:
        run = (await connection.execute(select(Run).where(Run.id == step.run_id))).one()
        session = (
            await connection.execute(select(Session).where(Session.id == step.session_id))
        ).one()
        assert run.unattended is True
        assert session.audience == "private"
        assert session.owner_user_id == tenant.ids["ana"]
        text = (
            await connection.execute(
                select(Message.content).where(Message.run_id == run.id, Message.role == "user")
            )
        ).scalar_one()["text"]
        assert "untrusted_content" in text
        assert "tea" in text
        await connection.execute(
            insert(Message).values(
                org_id=tenant.org,
                session_id=session.id,
                run_id=run.id,
                role="assistant",
                content={"text": "Tea is good."},
            )
        )
        await connection.execute(
            update(Run).where(Run.id == run.id).values(status="completed", finished_at=func.now())
        )
        await connection.execute(
            update(WorkflowInstance)
            .where(WorkflowInstance.id == uuid.UUID(instance_id))
            .values(checked_at=datetime(2000, 1, 1, tzinfo=UTC))
        )
    await _drain(runner)
    instance = await _status(engine, instance_id)
    assert instance.status == "completed"
    assert instance.output == "Tea is good."


async def test_a_sub_workflow_runs_and_gives_its_output(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    await _approved(
        service,
        tenant,
        """
name: child
inputs:
  side: { required: true }
steps:
  - id: read
    tool: notes_read
    args: { side: "{{inputs.side}}" }
""",
    )
    instance_id = await _start(
        service,
        tenant,
        """
name: parent
steps:
  - id: call
    workflow: { name: child, inputs: { side: deep } }
""",
    )
    await _drain(runner, rounds=6)
    instance = await _status(engine, instance_id)
    assert instance.status == "completed", instance.error
    assert instance.output == {"tool": "notes_read", "args": {"side": "deep"}}
    step = (await _steps(engine, instance_id))["call"]
    child = await _status(engine, str(step.child_instance_id))
    assert child.parent_instance_id == uuid.UUID(instance_id)
    assert child.depth == 1
    assert child.trigger == "workflow"


async def test_a_cancel_ends_the_open_steps_and_the_late_answer_does_not_change_them(
    engine: AsyncEngine,
    tenant: Tenant,
    service: WorkflowService,
    runner: WorkflowEngine,
    tools: FakeTools,
) -> None:
    instance_id = await _start(service, tenant, WRITE)
    await runner.advance()
    claims = await runner._claim()
    await service.cancel(tenant.user(), uuid.UUID(instance_id))
    await runner._call(claims[0])
    step = (await _steps(engine, instance_id))["post"]
    assert step.status == "cancelled"
    [receipt] = await _receipts(engine, instance_id)
    assert receipt.status == "done"
    assert (await _status(engine, instance_id)).status == "cancelled"


# -- schedules --------------------------------------------------------------------------------


async def test_a_due_schedule_starts_one_instance_per_fire(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    source = """
name: hourly
triggers:
  - cron: "0 * * * *"
steps:
  - id: read
    tool: notes_read
    args: {}
"""
    view = await service.create(tenant.user(), source)
    workflow_id = uuid.UUID(view["id"])
    due = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
    async with engine.begin() as connection:
        await connection.execute(
            update(Workflow).where(Workflow.id == workflow_id).values(next_fire_at=due)
        )
    assert await runner.tick() == 1
    assert await runner.tick() == 0
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(WorkflowInstance).where(WorkflowInstance.workflow_id == workflow_id)
            )
        ).all()
        workflow = (
            await connection.execute(select(Workflow).where(Workflow.id == workflow_id))
        ).one()
    assert len(rows) == 1
    assert rows[0].trigger == "schedule"
    assert rows[0].scheduled_for == due
    assert workflow.last_fire_at == due
    assert workflow.next_fire_at > datetime.now(UTC)

    # The same fire again (for example a second process) starts nothing.
    async with engine.begin() as connection:
        await connection.execute(
            update(Workflow).where(Workflow.id == workflow_id).values(next_fire_at=due)
        )
    await runner.tick()
    async with engine.connect() as connection:
        count = (
            await connection.execute(
                select(func.count()).where(WorkflowInstance.workflow_id == workflow_id)
            )
        ).scalar_one()
    assert count == 1


async def test_a_schedule_of_a_version_without_approval_does_not_start(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    source = WRITE + 'triggers:\n  - cron: "0 * * * *"\n'
    view = await service.create(tenant.user(), source)
    assert view["status"] == "pending_approval"
    assert view["next_fire_at"] is None
    with pytest.raises(Exception, match="approv"):
        await service.start(tenant.user(), uuid.UUID(view["id"]), {})


# -- review fixes -----------------------------------------------------------------------------


async def _expire_lease(engine: AsyncEngine, step_row_id: uuid.UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(WorkflowStep)
            .where(WorkflowStep.id == step_row_id)
            .values(lease_expires_at=func.now() - timedelta(seconds=1))
        )


async def test_a_cancel_ends_the_agent_run_in_the_same_transaction(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    """The run of an agent step ends with the cancel, so a crash after the commit cannot leave
    the run alive."""
    source = "name: think\nsteps:\n  - id: write\n    agent: { prompt: Hi. }\n"
    instance_id = await _start(service, tenant, source)
    await _drain(runner)
    step = (await _steps(engine, instance_id))["write"]
    assert step.status == "running"
    await service.cancel(tenant.user(), uuid.UUID(instance_id))
    async with engine.connect() as connection:
        run = (await connection.execute(select(Run).where(Run.id == step.run_id))).one()
    assert run.status == "cancelled"
    assert run.cancel_requested_at is not None


async def test_a_reap_uses_the_key_of_the_receipt_not_the_catalog_of_now(
    engine: AsyncEngine,
    tenant: Tenant,
    service: WorkflowService,
    runner: WorkflowEngine,
    tools: FakeTools,
) -> None:
    """The first call sent no key. The tool takes a key later, but a key cannot stop a second
    write of a call without a key: the step waits for a reconcile, and a retry sends no key."""
    tools.script["notes_write"] = ["write-then-crash"]
    instance_id = await _start(service, tenant, WRITE)
    await runner.advance()
    with pytest.raises(Crash):
        await runner.run_tools()
    tools.keyed.add("notes_write")
    step = (await _steps(engine, instance_id))["post"]
    await _expire_lease(engine, step.id)
    assert await runner.reap() == 1
    assert (await _steps(engine, instance_id))["post"].status == "needs_reconcile"

    tools.found = Reconciled(happened=False)
    await _drain(runner)
    assert [call[2] for call in tools.calls] == [None, None]
    assert (await _status(engine, instance_id)).status == "completed"


async def test_a_stale_reconcile_result_does_not_change_a_newer_attempt(
    engine: AsyncEngine,
    tenant: Tenant,
    service: WorkflowService,
    runner: WorkflowEngine,
    tools: FakeTools,
) -> None:
    tools.script["notes_write"] = [ConnectorUnknownError("lost")]
    instance_id = await _start(service, tenant, WRITE)
    await runner.advance()
    await runner.run_tools()
    step = (await _steps(engine, instance_id))["post"]
    assert step.status == "needs_reconcile"
    stale = step  # the reconcile loop read attempt 1
    async with engine.begin() as connection:
        await connection.execute(
            update(WorkflowStep)
            .where(WorkflowStep.id == step.id)
            .values(attempts=step.attempts + 1)
        )
    await runner._reconciled(stale, Reconciled(happened=True, output='{"id": "n-1"}'))
    after = (await _steps(engine, instance_id))["post"]
    assert after.status == "needs_reconcile"
    assert after.output is None
    [receipt] = await _receipts(engine, instance_id)
    assert receipt.status == "unknown"


async def test_a_write_step_fails_when_the_definition_changed_after_the_approval(
    engine: AsyncEngine,
    tenant: Tenant,
    service: WorkflowService,
    runner: WorkflowEngine,
    tools: FakeTools,
) -> None:
    """The stored hash still equals the approved hash, but the definition does not match the
    hash: the engine hashes the definition again at each claim."""
    from sama.db.models import WorkflowVersion

    instance_id = await _start(service, tenant, WRITE)
    async with engine.begin() as connection:
        version = (
            await connection.execute(
                select(WorkflowVersion).where(WorkflowVersion.org_id == tenant.org)
            )
        ).one()
        changed = dict(version.definition)
        changed["steps"] = [{**changed["steps"][0], "args": {"text": "other"}}]
        await connection.execute(
            update(WorkflowVersion)
            .where(WorkflowVersion.id == version.id)
            .values(definition=changed)
        )
    await _drain(runner)
    assert tools.calls == []
    assert "did not approve" in (await _status(engine, instance_id)).error


async def test_an_owner_who_left_the_workspace_fails_the_instance(
    engine: AsyncEngine,
    tenant: Tenant,
    service: WorkflowService,
    runner: WorkflowEngine,
    tools: FakeTools,
) -> None:
    from sama.db.models import Membership
    from sama.workflows.store import OWNER_GONE

    instance_id = await _start(service, tenant, WRITE)
    async with engine.begin() as connection:
        await connection.execute(
            delete(Membership).where(
                Membership.org_id == tenant.org, Membership.workspace_id == tenant.ids["ws"]
            )
        )
    await _drain(runner)
    assert tools.calls == []
    instance = await _status(engine, instance_id)
    assert instance.status == "failed"
    assert instance.error == OWNER_GONE


async def test_a_cancel_locks_the_runs_so_a_requeue_cannot_slip_between_its_updates(
    engine: AsyncEngine, tenant: Tenant, service: WorkflowService, runner: WorkflowEngine
) -> None:
    """The queue can requeue a running run when its lease ends. The cancel locks the runs
    before its two updates, so a requeue between the updates waits, and the run does not
    escape the cancel."""
    from sqlalchemy.exc import DBAPIError

    from sama.workflows.store import close_instance

    source = "name: think\nsteps:\n  - id: write\n    agent: { prompt: Hi. }\n"
    instance_id = await _start(service, tenant, source)
    await _drain(runner)
    run_id = (await _steps(engine, instance_id))["write"].run_id
    async with engine.begin() as connection:
        await connection.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(status="running", lease_owner="w1", lease_expires_at=func.now())
        )
    requeue: list[str] = []

    async def requeue_now() -> None:
        """The reap of the run queue, with a short lock wait."""
        try:
            async with engine.begin() as other:
                await other.execute(text("SET LOCAL lock_timeout = '200ms'"))
                await other.execute(
                    update(Run)
                    .where(Run.id == run_id, Run.status == "running")
                    .values(status="queued", lease_owner=None, lease_expires_at=None)
                )
            requeue.append("done")
        except DBAPIError:
            requeue.append("blocked")

    class Between:
        """The connection of the cancel. It requeues the run just before the update of the
        running runs, after the update of the queued runs."""

        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

        async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            if "UPDATE runs" in str(statement) and "cancel_requested_at IS NULL" in str(statement):
                await requeue_now()
            return await self.inner.execute(statement, *args, **kwargs)

    async with engine.begin() as connection:
        await close_instance(
            cast(Any, Between(connection)), tenant.org, uuid.UUID(instance_id), status="cancelled"
        )
    assert requeue == ["blocked"]
    async with engine.connect() as connection:
        run = (await connection.execute(select(Run).where(Run.id == run_id))).one()
    assert run.status == "running"
    assert run.cancel_requested_at is not None
